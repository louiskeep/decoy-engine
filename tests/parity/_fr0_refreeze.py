"""Re-freeze procedure for the Phase F generation re-baseline (test-side only).

Maps every F0 `generate_tables` fixture to (a) the semantic checks its output must satisfy and
(b) its pinned input envelope, and builds the manifest that records the engine minor version, the
envelope digest and the re-freeze date next to the table digests.

    ~/.cache/decoy-d1a-abi3-venv/bin/python -m tests.parity._fr0_refreeze > \\
        tests/fixtures/phase_f_fr0/manifest.json

Print-only: it never writes a golden. The golden stays in `_f0_expected.py`, edited by hand after
reviewing the diff against the semantic gate, so a test run can never launder a change into a new
baseline.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import pyarrow as pa

from decoy_engine.generation._reproducibility import build_envelope, reproducibility_exclusions
from tests.parity import _f0_fixtures as fx
from tests.parity import _fr0_semantic_gate as gate
from tests.parity._f0_canonical import table_fingerprint

RE_FROZEN_ON = "2026-10-18"
# Fixtures whose checked-in digest moved in this re-freeze, and why.
REFROZEN: dict[str, dict[str, Any]] = {
    "nulls": {
        "reason": (
            "schema only: the all-null categorical column and the zero-row table now carry their "
            "declared string type instead of Arrow's inferred null type; every value digest is "
            "unchanged"
        ),
        "previous_tables": {
            "nulls": "85588c66a32d807e060337af1abc3c12a9369902c91a6528943b81c02ceb0855",
            "empty": "6fb55c2d1210f8fb8e4ac79c3ff6fca06158df62f07e768834f439eaec685322",
        },
    },
}
LARGE_ROWS = 20_000


def large(builder: Callable[[], dict[str, Any]], rows: int = LARGE_ROWS) -> dict[str, pa.Table]:
    """The same configuration at a sample size where a marginal check has statistical power."""
    cfg = builder()
    for table in cfg["tables"]:
        if table["name"] in ("cat", "nulls"):
            table["row_count"] = rows
    return fx.run_generate_tables(cfg)


def _col(tables: dict[str, pa.Table], table: str, column: str) -> list[Any]:
    return tables[table].column(column).to_pylist()


# ---- per-fixture relation checks ---------------------------------------------------------


def g_sequence(t: dict[str, pa.Table]) -> None:
    gate.check_sequence(_col(t, "seq", "plain"), start=1, step=1)
    gate.check_sequence(
        _col(t, "seq", "padded"), start=100, step=7, prefix="INV-", suffix="/x", pad=6
    )
    gate.check_sequence(_col(t, "seq", "neg"), start=50, step=-4)


def g_categorical(t: dict[str, pa.Table]) -> None:
    assert set(_col(t, "cat", "weighted")) <= {"red", "green", "blue", "amber"}
    assert set(_col(t, "cat", "ints")) <= {1, 2, 3, 4, 5}
    assert pa.types.is_integer(t["cat"].schema.field("ints").type)


def g_categorical_marginal(t: dict[str, pa.Table]) -> None:
    gate.check_marginal(_col(t, "cat", "weighted"), ["red", "green", "blue", "amber"], [5, 3, 2, 1])
    gate.check_marginal(_col(t, "cat", "uniform"), ["a", "b", "c"])
    gate.check_marginal(_col(t, "cat", "ints"), [1, 2, 3, 4, 5])


def g_pooled(t: dict[str, pa.Table]) -> None:
    for name in ("city", "last", "street"):
        values = _col(t, "pooled", name)
        assert all(isinstance(v, str) and v for v in values), f"{name}: empty or non-string value"
        assert len(set(values)) <= 10_000, f"{name}: more distinct values than the 10_000 pool"


def g_live_faker(t: dict[str, pa.Table]) -> None:
    for name in ("city", "email", "de_name", "fallback", "opt_out"):
        assert all(isinstance(v, str) and v for v in _col(t, "live", name))
    assert all("@" in v for v in _col(t, "live", "email"))
    assert all(5 <= int(v) <= 9 for v in _col(t, "live", "num"))


def g_live_default_locale(t: dict[str, pa.Table]) -> None:
    for name in ("city", "own"):
        assert all(isinstance(v, str) and v for v in _col(t, "live_loc", name))


def g_keyed(t: dict[str, pa.Table]) -> None:
    assert set(_col(t, "keyed", "cat")) <= {"a", "b", "c", "d"}
    assert set(_col(t, "keyed", "maybe")) <= {"u", "v", None}


def g_nulls(t: dict[str, pa.Table]) -> None:
    for name in t["nulls"].column_names:
        gate.check_declared_string_column(t["nulls"], name)
    gate.check_declared_string_column(t["empty"], "id")
    assert t["empty"].num_rows == 0
    assert all(v is None for v in _col(t, "nulls", "cat_all_null"))
    for i, v in enumerate(_col(t, "nulls", "seq_partial")):
        assert v is None or v == str(1 + i), "a non-null sequence value moved off its ordinal"
    assert set(_col(t, "nulls", "cat_partial")) <= {"u", "v", None}


def g_nulls_large(t: dict[str, pa.Table]) -> None:
    for name in ("seq_partial", "cat_partial", "faker_partial"):
        gate.check_null_fraction(_col(t, "nulls", name), 0.4)
    gate.check_marginal(_col(t, "nulls", "cat_partial"), ["u", "v"])


def g_reference(t: dict[str, pa.Table]) -> None:
    child, parent = t["ref_child"], t["ref_parent"]
    for col in ("r_random", "r_seq", "r_weighted", "r_bounded"):
        gate.check_fk_membership(child, col, parent, "pkey")
    gate.check_sequential_reference(child, "r_seq", parent, "pkey")
    gate.check_parent_child_cardinality(child, "r_bounded", parent, "pkey", low=2, high=4)


def g_referenced_formula(t: dict[str, pa.Table]) -> None:
    rows = t["rf"].to_pylist()
    for row in rows:
        assert row["rf_a"].startswith(f"{row['grp']}-{row['pid']}-")
        assert row["rf_c"] == "[]"


def g_derived(t: dict[str, pa.Table]) -> None:
    gate.check_null_propagation(
        _col(t, "derived", "from_nullable"), _col(t, "derived", "maybe"), label="from_nullable"
    )
    assert all(
        p == b + 1
        for p, b in zip(_col(t, "derived", "plus"), _col(t, "derived", "base"), strict=True)
    )


def g_mixed_fk(t: dict[str, pa.Table]) -> None:
    assert set(_col(t, "people", "id")) <= {"x", "y", "z", "w", "v"}


def g_composite(table: str) -> Callable[[dict[str, pa.Table]], None]:
    return lambda t: gate.check_composite_coherence(t[table])


GATES: dict[str, Callable[[dict[str, pa.Table]], None]] = {
    "sequence": g_sequence,
    "categorical": g_categorical,
    "pooled_faker": g_pooled,
    "live_faker": g_live_faker,
    "live_faker_default_locale": g_live_default_locale,
    "keyed_derive": g_keyed,
    "nulls": g_nulls,
    "reference": g_reference,
    "referenced_formula": g_referenced_formula,
    "derived": g_derived,
    "mixed_fk_generated_parent": g_mixed_fk,
    "composite_person_deterministic": g_composite("person_det"),
    "composite_person_seeded_pool": g_composite("person_pool"),
    "composite_name_email_deterministic": g_composite("name_email_det"),
    "composite_name_email_seeded_pool": g_composite("name_email_pool"),
}
# (sample builder, check) pairs that need statistical power; run on a large regeneration.
LARGE_GATES: dict[
    str, tuple[Callable[[], dict[str, pa.Table]], Callable[[dict[str, pa.Table]], None]]
] = {
    "categorical": (lambda: large(fx.cfg_categorical), g_categorical_marginal),
    "nulls": (lambda: large(fx.cfg_nulls), g_nulls_large),
}
# Legacy-family fixtures whose golden is unchanged and which carry no relation this gate checks.
NO_RELATION_CHECKS: dict[str, str] = {
    "formula": "legacy path, golden unchanged, row-local pandas formulas",
    "derived_aggregate": "legacy path, golden unchanged, whole-column reductions",
    "statistical": "legacy path, golden unchanged, pinned-snapshot sampler",
    "conditional": "legacy path, golden unchanged, pinned-snapshot sampler",
    "grouped_windowed": "legacy path, golden unchanged, grouped families deferred",
}


def run_gate(fid: str, tables: dict[str, pa.Table]) -> None:
    GATES[fid](tables)
    if fid in LARGE_GATES:
        builder, check = LARGE_GATES[fid]
        check(builder())


# ---- envelope + manifest -------------------------------------------------------------------


def fixture_envelope_inputs(fixture: fx.Fixture) -> dict[str, Any] | None:
    builder = getattr(fixture.run, "builder", None)
    if builder is None:
        return None
    kwargs = getattr(fixture.run, "kwargs", {})
    config = builder()
    # A snapshot path is a checkout location, not an input: the envelope names the snapshot by the
    # digest of its content (the same rule the seed fingerprint uses).
    pinned: list[str] = []
    for table in config["tables"]:
        for col in table.get("generate_columns") or []:
            if "snapshot_file" in col:
                with open(col["snapshot_file"], "rb") as fh:
                    digest = "sha256:" + hashlib.sha256(fh.read()).hexdigest()
                col["snapshot_file"] = digest
                pinned.append(digest)
    derive_digest = None
    if kwargs.get("derive_key") is not None:
        derive_digest = hashlib.sha256(kwargs["derive_key"]("envelope-probe")).hexdigest()
    return {
        "config": config,
        "pinned_input_digests": pinned,
        "derive_key_digest": derive_digest,
        "instance_default_locale": kwargs.get("instance_default_locale"),
        "controlled_clock": fx.ORACLE_CLOCK.isoformat(),
    }


def fixture_envelope_digest(fixture: fx.Fixture) -> str | None:
    inputs = fixture_envelope_inputs(fixture)
    if inputs is None:
        return None
    return build_envelope(
        inputs["config"],
        derive_key_digest=inputs["derive_key_digest"],
        instance_default_locale=inputs["instance_default_locale"],
        pinned_input_digests=inputs["pinned_input_digests"],
        controlled_clock=inputs["controlled_clock"],
    ).digest()


def fixture_exclusions(fixture: fx.Fixture) -> dict[tuple[str, str], str]:
    inputs = fixture_envelope_inputs(fixture)
    if inputs is None:
        return {}
    return reproducibility_exclusions(inputs["config"], controlled_clock=True)


def build_manifest() -> dict[str, Any]:
    from decoy_engine.generation._reproducibility import engine_minor_version
    from tests.parity._f0_expected import EXPECTED

    fixtures: dict[str, Any] = {}
    for fixture in fx.FIXTURES:
        digest = fixture_envelope_digest(fixture)
        if digest is None:
            continue
        with fx.frozen_faker_clock():
            tables = fixture.run()
        fixtures[fixture.fid] = {
            "envelope_digest": digest,
            "tables": {name: table_fingerprint(tbl)["table"] for name, tbl in tables.items()},
            "status": "refrozen" if fixture.fid in REFROZEN else "unchanged",
            "excluded_columns": sorted(f"{t}.{c}" for t, c in fixture_exclusions(fixture)),
        }
        assert fixtures[fixture.fid]["tables"] == {
            n: fp["table"] for n, fp in EXPECTED[fixture.fid].items()
        }, f"{fixture.fid}: _f0_expected.py disagrees with the regenerated golden"
    return {
        "engine_minor": engine_minor_version(),
        "re_frozen_on": RE_FROZEN_ON,
        "refrozen": REFROZEN,
        "fixtures": fixtures,
    }


if __name__ == "__main__":
    print(json.dumps(build_manifest(), indent=2, sort_keys=True))
