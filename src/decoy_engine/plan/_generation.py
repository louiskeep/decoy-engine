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
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from decoy_engine import fileaccess, reasons
from decoy_engine.fileaccess import FileRole
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


# Set by pinned compilation to inspect each snapshot's decoded JSON before any schema or spec
# validation sees it, without a second read of the file.
snapshot_content_guard: ContextVar[Callable[[Any], None] | None] = ContextVar(
    "snapshot_content_guard", default=None
)


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


def column_pins_snapshot(col: dict[str, Any]) -> bool:
    """True when a generate column's `snapshot_file` is read once and pinned into the Plan:
    a `statistical` column, or a `reference` column with `distribution: fitted`. The one
    predicate for the read-once pass and the load-time pin check."""
    col_type = col.get("type")
    return col_type == "statistical" or (
        col_type == "reference" and col.get("distribution") == "fitted"
    )


def _iter_statistical_snapshot_paths(config: dict[str, Any]) -> list[str]:
    """Every distinct `snapshot_file` path referenced by a pinning generate
    column (`column_pins_snapshot`), in declared order (tables then columns),
    de-duplicated by path string while preserving first-seen order."""
    return list(_snapshot_field_keys(config))


def _snapshot_field_keys(config: dict[str, Any]) -> dict[str, str]:
    """`{path: config position}` for the same paths, first-seen position kept, so a
    denial names where the path was written and never the path."""
    seen: dict[str, str] = {}
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        for col_entry in table_entry.get("generate_columns", []) or []:
            if not isinstance(col_entry, dict) or not column_pins_snapshot(col_entry):
                continue
            path = col_entry.get("snapshot_file")
            if path:
                key = f"tables.{table_entry.get('name')}.generate_columns.{col_entry.get('name')}.snapshot_file"
                seen.setdefault(str(path), key)
    return seen


def snapshot_schema_failure(parsed: Any, shown: str) -> Any:
    """The one snapshot schema-version check, for compile (`read_and_pin_snapshots`) and plan
    load. Returns a `StatisticalSpecError` or `None`. The message names the field and the
    expected version only: the stored text is untrusted and never echoed."""
    from decoy_engine.generation.statistical._spec import StatisticalSpecError

    if (
        isinstance(parsed, dict)
        and parsed.get("schema_version") == DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION
    ):
        return None
    return StatisticalSpecError(
        code=reasons.plan.STATISTICAL_SNAPSHOT_SCHEMA_MISMATCH,
        message=(
            f"snapshot_file {shown!r} does not declare 'schema_version' "
            f"{DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION!r}; this engine consumes only that version."
        ),
    )


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
    for path, field_key in _snapshot_field_keys(config).items():
        shown = fileaccess.shown(path, field_key)
        failure: str | None = None
        try:
            raw = fileaccess.read_all(FileRole.SNAPSHOT, path, field_key)
        except OSError as exc:
            if fileaccess.keeps_class(exc):
                raise
            if fileaccess.is_ambient():
                failures[path] = StatisticalSpecError(
                    code=reasons.plan.STATISTICAL_SNAPSHOT_UNREADABLE,
                    message=f"snapshot_file {shown!r} could not be read: {exc}",
                )
                continue
            failure = fileaccess.redact_locators(str(exc), [(path, shown)])
        if failure is not None:
            # Strict: the failure is recorded after the handler closes, with no library error
            # attached (its text can name the file).
            failures[path] = StatisticalSpecError(
                code=reasons.plan.STATISTICAL_SNAPSHOT_UNREADABLE,
                message=f"snapshot_file {shown!r} could not be read: {failure}",
            )
            continue
        if len(raw) > EMBEDDED_SNAPSHOT_MAX_BYTES:
            raise PlanCompileError(
                code=reasons.plan.DP_SNAPSHOT_EMBEDDED_ARTIFACT_TOO_LARGE,
                path=f"<snapshot_file={shown}>",
                message=(
                    f"snapshot_file {shown!r} is {len(raw)} bytes, exceeding the "
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
                code=reasons.plan.STATISTICAL_SNAPSHOT_UNREADABLE,
                message=f"snapshot_file {shown!r} could not be read: {exc}",
            )
            continue
        guard = snapshot_content_guard.get()
        if guard is not None:
            guard(parsed)
        mismatch = snapshot_schema_failure(parsed, shown)
        if mismatch is not None:
            failures[path] = mismatch
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
        failure = failures.get(snapshot_file)
        if failure is None:
            failure = StatisticalSpecError(
                code=reasons.plan.STATISTICAL_SNAPSHOT_UNREADABLE,
                message=(
                    f"snapshot_file {fileaccess.shown(snapshot_file, 'snapshot_file')!r} has no "
                    "pinned content and no "
                    "recorded read failure from this compile's read-once pass; "
                    "refusing rather than reopening the path."
                ),
            )
        raise failure
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


_FITTED_CONFLICT_KEYS = ("weights", "min_per_parent", "max_per_parent")


def check_fitted_references(
    config: dict[str, Any],
    pinned: dict[str, ReadSnapshot],
    failures: dict[str, Any] | None,
) -> None:
    """Refuse a `reference` column with `distribution: fitted` whose pinned snapshot cannot
    drive it: no `snapshot_file`, a failed read, a conflicting knob, or a missing or invalid
    fan-out block. Reads only the read-once pass's bytes (`resolve_pinned_snapshot`, never
    the path) and decodes with the same reader generation uses. Messages carry structure
    names only, and the decoder error is not chained."""
    from decoy_engine.generation.statistical import StatisticalSpecError, fanout_spec_from_snapshot

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col in table_entry.get("generate_columns", []) or []:
            if not isinstance(col, dict) or col.get("type") != "reference":
                continue
            if col.get("distribution") != "fitted":
                continue
            name = col.get("name", "?")
            where = f"tables.{table_name}.generate_columns.{name}"
            snapshot_file = col.get("snapshot_file")
            if not snapshot_file:
                raise PlanCompileError(
                    code=reasons.plan.REFERENCE_FANOUT_MISSING,
                    path=f"{where}.snapshot_file",
                    message=(
                        f"reference column {name!r} uses distribution: fitted and needs "
                        "`snapshot_file`. Run `decoy fit <source> --fanout <column>`, or use "
                        "distribution: random."
                    ),
                )
            snapshot_file = str(snapshot_file)
            outcome: tuple[str, str] | None = None
            try:
                # `{}` rather than None: a fitted check never falls back to reading the path.
                snap = resolve_pinned_snapshot(
                    snapshot_file, pinned, failures if failures is not None else {}
                )
                conflict = [k for k in _FITTED_CONFLICT_KEYS if col.get(k) is not None]
                if conflict:
                    outcome = (
                        reasons.plan.REFERENCE_FANOUT_CONFLICT,
                        f"reference column {name!r} sets {conflict[0]!r} with distribution: "
                        "fitted, which already fixes the per-parent counts. Remove it, or use "
                        "another distribution.",
                    )
                else:
                    source = str(col.get("source_column") or name)
                    fanout_spec_from_snapshot(snap, source, digest=pinned[snapshot_file].sha256)
            except StatisticalSpecError as exc:
                outcome = (exc.code, exc.message)
            if outcome is not None:
                raise PlanCompileError(code=outcome[0], path=where, message=outcome[1])


def check_generate_snapshot_columns(
    config: dict[str, Any],
    pinned: dict[str, ReadSnapshot] | None,
    dp_verified_columns: frozenset[tuple[str, str]],
    failures: dict[str, Any] | None = None,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """The one snapshot-column check for compile, config-only checks and plan load: the
    statistical checks, then the fitted-reference checks. Returns the statistical
    column-spec list unchanged for `build_generation_plan`."""
    from decoy_engine.plan._checks import check_statistical_columns

    specs = check_statistical_columns(config, pinned, dp_verified_columns, failures=failures)
    check_fitted_references(config, pinned or {}, failures)
    return specs
