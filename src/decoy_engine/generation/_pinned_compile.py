"""Compile a generation job into a ``PinnedPlan``.

Admits only constructs whose output is a pure function of (plan, seed, runtime identity) and
refuses the rest with a coded error, before and independent of ordinary plan compilation.
Statistical snapshots are read and digested exactly once, by the same read-once pass
``compile_plan`` runs, so the digest in the plan is the digest of the bytes the specs were built
from.

Methodology: lockfile-style content pinning (inputs addressed by SHA-256 digest, as in pip/uv
hash pinning) on top of the engine's existing read-once snapshot pass; canonical JSON for the
resolved column config (see ``_pinned_plan``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from decoy_engine import fileaccess, reasons
from decoy_engine._reference_date import normalize_reference_date
from decoy_engine.fileaccess import FileAccess
from decoy_engine.generation._pinned_plan import (
    PINNED_PLAN_SCHEMA,
    PinnedColumn,
    PinnedModeError,
    PinnedPlan,
    PinnedStatistical,
    PinnedTable,
    runtime_identity,
)

# Fail-closed allowlist: a generator type enters pinned mode only by being listed here after its
# purity is reviewed. The sentry test keeps this and REJECTED_TYPES in step with the config Literal.
PINNABLE_TYPES = frozenset(
    {
        "sequence",
        "categorical",
        "reference",
        "derived",
        "derived_aggregate",
        "grouped_series",
        "group_key",
        "windowed_date",
        "statistical",
    }
)

REJECTED_TYPES = {
    "faker": (
        "pinned_faker_unsupported",
        "faker columns are not available in pinned mode",
    ),
    "formula": (
        "pinned_formula_unsupported",
        "formula columns are not available in pinned mode",
    ),
}

# Matches the only string grammar `windowed_date` accepts (numeric ISO date or datetime, no
# zone), plus the two tokens pinned mode resolves against the reference date.
ANCHOR_TOKENS = frozenset({"now", "today"})
ISO_ANCHOR = re.compile(
    r"\A\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?)?\Z",
    re.ASCII,
)


def anchor_is_absolute(text: str) -> bool:
    return text.strip().lower() in ANCHOR_TOKENS or ISO_ANCHOR.match(text) is not None


# Any ${...} form: the platform resolver substitutes these before the engine sees the job, so one
# surviving here is a live input the plan cannot pin.
LIVE_TOKEN = re.compile(r"\$\{[^}]*\}")


def find_live_token(value: Any, path: str = "") -> str | None:
    """Field path of the first ``${...}`` token in ``value``, scanning every key and value.

    The path never contains the token: a key that holds one is shown as ``<key>``.
    """
    if isinstance(value, str):
        return path if LIVE_TOKEN.search(value) else None
    if isinstance(value, dict):
        for k, v in value.items():
            key = str(k)
            seg = "<key>" if LIVE_TOKEN.search(key) else key
            sub = f"{path}.{seg}" if path else seg
            if LIVE_TOKEN.search(key):
                return sub
            hit = find_live_token(v, sub)
            if hit is not None:
                return hit
        return None
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            hit = find_live_token(v, f"{path}[{i}]")
            if hit is not None:
                return hit
    return None


def refuse_live_tokens(value: Any, root: str) -> None:
    """The one token guard: run first, over the whole input, before any validator sees it."""
    hit = find_live_token(value)
    if hit is not None:
        where = f"{root}.{hit}" if hit and not hit.startswith("[") else f"{root}{hit}"
        raise PinnedModeError(
            code=reasons.plan.PINNED_LIVE_TOKEN,
            message=f"{where or root} holds an unresolved variable token",
        )


def _where(table: str, column: str) -> str:
    return f"table {table!r} column {column!r}"


def reject_unpinnable(config: dict[str, Any]) -> None:
    """Raise ``PinnedModeError`` for any construct pinned mode refuses. Reads config only."""
    tables = config.get("tables")
    if not isinstance(tables, list) or not tables:
        raise PinnedModeError(
            code=reasons.plan.PINNED_NOT_GENERATION_ONLY, message="the job declares no tables"
        )
    for table in tables:
        tname = str(table.get("name"))
        gcols = table.get("generate_columns")
        if table.get("source") or table.get("sources"):
            raise PinnedModeError(
                code=reasons.plan.PINNED_NOT_GENERATION_ONLY,
                message=f"table {tname!r} declares a source; pinned mode is generation-only",
            )
        if not gcols:
            raise PinnedModeError(
                code=reasons.plan.PINNED_NOT_GENERATION_ONLY,
                message=f"table {tname!r} has no generate_columns; pinned mode is generation-only",
            )
        by_name = {c.get("name"): c for c in gcols}
        for col in gcols:
            _reject_column(tname, col, by_name)


def _reject_column(tname: str, col: dict[str, Any], siblings: dict[Any, dict[str, Any]]) -> None:
    cname = str(col.get("name"))
    kind = col.get("type")
    if kind in REJECTED_TYPES:
        code, why = REJECTED_TYPES[kind]
        raise PinnedModeError(code=code, message=f"{_where(tname, cname)}: {why}")
    if kind not in PINNABLE_TYPES:
        raise PinnedModeError(
            code=reasons.plan.PINNED_TYPE_NOT_ALLOWLISTED,
            message=f"{_where(tname, cname)}: generator type is not pinnable",
        )
    if col.get("determinism") == "fresh":
        raise PinnedModeError(
            code=reasons.plan.PINNED_DETERMINISM_FRESH,
            message=f"{_where(tname, cname)}: determinism 'fresh' draws OS entropy",
        )
    if kind != "statistical" and col.get("snapshot_file"):
        raise PinnedModeError(
            code=reasons.plan.PINNED_SNAPSHOT_FILE_NOT_STATISTICAL,
            message=f"{_where(tname, cname)}: snapshot_file is only pinnable on statistical columns",
        )
    if kind == "windowed_date":
        anchor = siblings.get(col.get("anchor"))
        if anchor is not None and anchor.get("type") == "categorical":
            for category in anchor.get("categories") or []:
                if isinstance(category, str) and not anchor_is_absolute(category):
                    raise PinnedModeError(
                        code=reasons.plan.PINNED_WINDOWED_ANCHOR_NOT_ABSOLUTE,
                        message=(
                            f"{_where(tname, cname)}: anchor column {anchor.get('name')!r} "
                            "declares a category that is not an absolute date"
                        ),
                    )


def _empty_profile(reference_date: str) -> Any:
    from decoy_engine.profile import Profile

    return Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime.fromisoformat(reference_date.replace("Z", "+00:00")),
        decoy_engine_version="pinned",
    )


@dataclass(frozen=True)
class PinnedCompilation:
    """The ``PinnedPlan`` and the ``GenerationPlan`` it was built from, from one compile.

    An in-process pair, not a wire type: fingerprint and serialize ``pinned``. ``generation`` is
    the identical object ``pinned`` was projected from, so window admission and execution cannot
    disagree about tables, columns or row counts. It is not seed-independent the way the
    fingerprint is.
    """

    pinned: PinnedPlan
    generation: Any  # GenerationPlan; plan types are imported lazily in this module


@fileaccess.entry_point
def compile_pinned_plan(
    config: dict[str, Any],
    reference_date: datetime | str | None = None,
    *,
    file_access: FileAccess | None = None,
) -> PinnedPlan:
    """Resolve ``config`` into a ``PinnedPlan`` bound to ``reference_date``.

    Without a positional ``reference_date`` the config's ``global_settings.reference_date`` is
    used, so an approved config carries its own clock anchor. The positional argument wins.

    The config seed is deliberately dropped: the caller supplies the seed at execution, and the
    fingerprint must be the same for every seed.
    """
    return compile_pinned_plan_with_generation(config, reference_date).pinned


@fileaccess.entry_point
def compile_pinned_plan_with_generation(
    config: dict[str, Any],
    reference_date: datetime | str | None = None,
    *,
    file_access: FileAccess | None = None,
) -> PinnedCompilation:
    """``compile_pinned_plan`` that also returns the ``GenerationPlan`` of the same compile.

    The step order is load-bearing for which error wins: token scan, reference date, pre-compile
    rejection, guarded compile, resolved-config recheck.
    """
    import decoy_engine
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._types import unfreeze_json

    refuse_live_tokens(config, "config")
    if reference_date is None:
        reference_date = (config.get("global_settings") or {}).get("reference_date")
    if reference_date is None:
        raise PinnedModeError(
            code=reasons.plan.PINNED_REFERENCE_DATE_INVALID,
            message="reference_date is required: pass it or set global_settings.reference_date",
        )
    reference = normalize_reference_date(reference_date)
    reject_unpinnable(config)
    from decoy_engine.plan._generation import snapshot_content_guard

    reset = snapshot_content_guard.set(lambda parsed: refuse_live_tokens(parsed, "snapshot"))
    try:
        plan = compile_plan(
            config, _empty_profile(reference), decoy_engine_version=str(decoy_engine.__version__)
        )
    finally:
        snapshot_content_guard.reset(reset)
    gen = plan.generation
    if gen is None:
        raise PinnedModeError(
            code=reasons.plan.PINNED_NOT_GENERATION_ONLY,
            message="the job compiled with nothing to generate",
        )
    resolved = json.loads(gen.config_json)
    # Re-run on the resolved config: compilation may fill defaults, and the pinned plan is
    # built from that form.
    reject_unpinnable(resolved)

    statistical: list[PinnedStatistical] = []
    digest_for: dict[tuple[str, str], str] = {}
    for spec in gen.statistical_specs:
        digest = gen.snapshots[spec.snapshot_index].sha256
        digest_for[(spec.table_name, spec.column_name)] = digest
        statistical.append(
            PinnedStatistical(
                table=spec.table_name,
                column=spec.column_name,
                snapshot_digest=digest,
                spec_json=json.dumps(unfreeze_json(spec.spec), sort_keys=True),
            )
        )

    tables: list[PinnedTable] = []
    for table in sorted(resolved["tables"], key=lambda t: str(t["name"])):
        tname = str(table["name"])
        columns: list[PinnedColumn] = []
        for col in table["generate_columns"]:
            cfg = dict(col)
            if cfg.get("type") == "statistical":
                pinned_digest = digest_for.get((tname, str(cfg["name"])))
                if pinned_digest is None:
                    raise PinnedModeError(
                        code=reasons.plan.PINNED_STATISTICAL_DIGEST_MISSING,
                        message=f"{_where(tname, str(cfg['name']))}: no pinned snapshot digest",
                    )
                # The path is a deployment detail; the digest is the identity.
                cfg["snapshot_file"] = f"sha256:{pinned_digest}"
            columns.append(
                PinnedColumn(name=str(cfg["name"]), config_json=json.dumps(cfg, sort_keys=True))
            )
        depends = sorted(
            {
                str(c["reference_table"])
                for c in table["generate_columns"]
                if c.get("type") == "reference"
            }
        )
        tables.append(
            PinnedTable(
                name=tname,
                row_count=int(table.get("row_count") or 0),
                depends_on=tuple(depends),
                columns=tuple(columns),
            )
        )

    pinned = PinnedPlan(
        schema_version=PINNED_PLAN_SCHEMA,
        reference_date=reference,
        runtime_identity=runtime_identity(),
        tables=tuple(tables),
        statistical=tuple(sorted(statistical, key=lambda s: (s.table, s.column))),
    )
    return PinnedCompilation(pinned=pinned, generation=gen)
