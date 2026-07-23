"""Read-once snapshot pinning + `GenerationPlan` construction (DPS Scope B).

`plan/_compile.py` is already at its ~600 LOC orchestration cap (guide
section 1, "Current module sizes"), so this module owns everything the
guide's section 4.7 requires beyond a call site: reading every distinct
`snapshot_file` path exactly once per compile, embedding the bytes and
digest into a `PinnedSnapshot`, and freezing the generate-side
configuration plus parsed statistical specs into a `GenerationPlan`.

Why "once per path, before any check" matters (guide section 4.7): the
parked code reached `_load_snapshot(path)` from three separate compile-
time callers, each re-opening the file; a file swapped between two of
those reads could yield two different byte strings inside ONE
compilation. Doing the read pass first and handing every subsequent
check the SAME pinned bytes closes that window structurally, the same
argument that makes generation itself safe from a post-compile file
swap (guide section 7.4's TOCTOU test).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import (
    DpVerification,
    GenerationPlan,
    PinnedSnapshot,
    PinnedStatisticalSpec,
    freeze_json,
)
from decoy_engine.quality.snapshot import DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION

# HC-5 precedent (`quality/snapshot.py::_HIGH_CARDINALITY_MAX_LABEL_BYTES`):
# a fail-closed size fence on an embedded artifact is a typed compile
# error, never a silent truncation or a fallback to a runtime path read.
EMBEDDED_SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024  # 16 MiB


@dataclass(frozen=True)
class ReadSnapshot:
    """One snapshot path's content, read exactly once. Internal to the
    compile-time pinning pass; `plan._checks_dp` and `plan._checks`
    consume this instead of ever calling `open()` themselves."""

    path: str
    sha256: str
    parsed: dict[str, Any]
    raw: bytes

    def to_pinned(self) -> PinnedSnapshot:
        dp_block = self.parsed.get("dp")
        release_id = dp_block.get("release_id") if isinstance(dp_block, dict) else None
        return PinnedSnapshot(
            source_path=self.path,
            sha256=self.sha256,
            payload_b64=base64.b64encode(self.raw).decode("ascii"),
            release_id=release_id if isinstance(release_id, str) else None,
        )


def _iter_statistical_snapshot_paths(config: dict[str, Any]) -> list[str]:
    """Every distinct `snapshot_file` path referenced by a `type:
    statistical` generate column, in declared order (tables then columns),
    de-duplicated by path string while preserving first-seen order."""
    seen: dict[str, None] = {}
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        for col_entry in table_entry.get("generate_columns", []) or []:
            if not isinstance(col_entry, dict) or col_entry.get("type") != "statistical":
                continue
            path = col_entry.get("snapshot_file")
            if path:
                seen.setdefault(str(path), None)
    return list(seen)


def read_and_pin_snapshots(
    config: dict[str, Any],
) -> tuple[dict[str, ReadSnapshot], dict[str, Any]]:
    """The single read pass (guide section 4.7 item 1): open every distinct
    referenced snapshot path exactly once, hash it, parse it, and return
    `({path: ReadSnapshot}, {path: StatisticalSpecError})`. A file this
    pass cannot open or parse is absent from the first mapping and present
    in the second, carrying the SAME classified failure `check_statistical_
    columns` would otherwise re-derive by reopening it.

    C-M1 (round-3 remediation): the parked version of this pass silently
    dropped an unreadable/malformed path with no record of WHY, relying on
    `check_statistical_columns` to call `generation.statistical._spec.
    _load_snapshot(path)` a SECOND time to re-derive the same verdict --
    a real second `open()` of a path that could have been swapped between
    the two reads, violating the single-read invariant `CHANGELOG.md`
    claims. Classifying the failure HERE, during the one read this pass
    ever performs, and handing the classification (not the path) to the
    caller closes that reopen structurally rather than by convention.
    """
    from decoy_engine.generation.statistical._spec import StatisticalSpecError

    pinned: dict[str, ReadSnapshot] = {}
    failures: dict[str, Any] = {}
    for path in _iter_statistical_snapshot_paths(config):
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            failures[path] = StatisticalSpecError(
                code="statistical_snapshot_unreadable",
                message=f"snapshot_file {path!r} could not be read: {exc}",
            )
            continue
        if len(raw) > EMBEDDED_SNAPSHOT_MAX_BYTES:
            raise PlanCompileError(
                code="dp_snapshot_embedded_artifact_too_large",
                path=f"<snapshot_file={path}>",
                message=(
                    f"snapshot_file {path!r} is {len(raw)} bytes, exceeding the "
                    f"{EMBEDDED_SNAPSHOT_MAX_BYTES} byte (16 MiB) embedded-artifact cap "
                    "(HC-5 high-cardinality precedent, quality/snapshot.py). Pinning the "
                    "full artifact bytes into the Plan is required for generation to never "
                    "reopen a snapshot path; a bounded format change is a separate, reviewed "
                    "escalation, not a silent truncation or a runtime path-read fallback."
                ),
            )
        digest = hashlib.sha256(raw).hexdigest()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            failures[path] = StatisticalSpecError(
                code="statistical_snapshot_unreadable",
                message=f"snapshot_file {path!r} could not be read: {exc}",
            )
            continue
        version = parsed.get("schema_version") if isinstance(parsed, dict) else None
        if not isinstance(parsed, dict) or version != DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION:
            failures[path] = StatisticalSpecError(
                code="statistical_snapshot_schema_mismatch",
                message=(
                    f"snapshot_file {path!r} declares schema {version!r}; this engine "
                    f"consumes {DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION!r}."
                ),
            )
            continue
        pinned[path] = ReadSnapshot(path=path, sha256=digest, parsed=parsed, raw=raw)
    return pinned, failures


def resolve_pinned_snapshot(
    snapshot_file: str,
    pinned: dict[str, ReadSnapshot],
    failures: dict[str, Any] | None,
) -> dict[str, Any]:
    """Guide section 4.7 / C-M1 (round-3 remediation): the single place
    that decides what `check_statistical_columns` sees for one
    `snapshot_file`, and the only place left that may still call
    `_load_snapshot` -- and only when NO read-once pass ran at all.

    `failures` non-`None` means the caller DID run `read_and_pin_snapshots`
    for this compile: every referenced path was already attempted exactly
    once, so a path absent from BOTH `pinned` and `failures` here is a
    compiler defect, not a license to reopen it -- raise a generic refusal
    rather than falling back to disk. `failures=None` means no read-once
    pass ran (a direct/legacy caller building a spec by hand, e.g. a unit
    test); the read below is that caller's ONLY read of `snapshot_file`,
    not a second read of anything a compile pass already pinned.
    """
    from decoy_engine.generation.statistical._spec import StatisticalSpecError, _load_snapshot

    read = pinned.get(snapshot_file)
    if read is not None:
        return read.parsed
    if failures is not None:
        raise failures.get(
            snapshot_file,
            StatisticalSpecError(
                code="statistical_snapshot_unreadable",
                message=(
                    f"snapshot_file {snapshot_file!r} has no pinned content and no "
                    "recorded read failure from this compile's read-once pass; "
                    "refusing rather than reopening the path."
                ),
            ),
        )
    return _load_snapshot(snapshot_file)[1]


def build_generation_plan(
    config: dict[str, Any],
    pinned: dict[str, ReadSnapshot],
    *,
    column_specs: list[tuple[str, str, str, dict[str, Any]]],
    dp_verification: DpVerification | None,
) -> GenerationPlan | None:
    """Assemble the frozen `GenerationPlan` from the read-once pass's
    output plus the already-validated statistical specs.

    Args:
        column_specs: `(table_name, column_name, snapshot_path,
            parsed_spec_dict)` per `type: statistical` generate column
            that compiled successfully (in declared order). The parsed
            spec dict is the `StatisticalSpec` shape `load_spec` already
            validated; it is frozen here, not re-validated.

    Returns:
        `None` when the config has no `generate_columns` at all (a pure
        mask Plan) -- a Plan with nothing to generate needs no
        GenerationPlan. A config with generate tables but NO `type:
        statistical` column still gets one (guide section 4.7's "None for
        a Plan compiled without any statistical generate columns" is
        read here as "nothing generate-shaped to pin at all": every
        generate-capable Plan must carry `config_json` for `generate_
        tables` to be Plan-only end to end, guide step 8/F3; only the
        snapshot-pinning fields are specifically statistical-gated).
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    has_generate_tables = any(isinstance(t, dict) and t.get("generate_columns") for t in tables)
    if not has_generate_tables:
        return None

    ordered_paths = _iter_statistical_snapshot_paths(config)
    snapshots: list[PinnedSnapshot] = []
    path_to_index: dict[str, int] = {}
    for path in ordered_paths:
        read = pinned.get(path)
        if read is None:
            continue  # unreadable; check_statistical_columns already raised
        path_to_index[path] = len(snapshots)
        snapshots.append(read.to_pinned())

    statistical_specs = tuple(
        PinnedStatisticalSpec(
            table_name=table_name,
            column_name=column_name,
            snapshot_index=path_to_index[path],
            spec=freeze_json(spec_dict),
        )
        for table_name, column_name, path, spec_dict in column_specs
        if path in path_to_index
    )
    return GenerationPlan(
        config_json=json.dumps(config, sort_keys=True, default=str),
        snapshots=tuple(snapshots),
        statistical_specs=statistical_specs,
        dp_verification=dp_verification,
    )
