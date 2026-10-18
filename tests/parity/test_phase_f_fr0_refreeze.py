"""Phase F FR0: the re-freeze is a reviewed, semantic, non-vacuous step (plan sections 5 and 6).

What this pins. The F0 golden was re-based once, for the unified generation core. Four things
guard that step: the manifest ties each golden to the engine minor version and the pinned input
envelope; the semantic gate (`_fr0_semantic_gate.py`) passes for every fixture, so a re-frozen
table is checked against RELATIONS and the configured distribution, not against its own bytes; each
mutant that keeps the marginals but breaks one relation makes the gate fail; and the reproducibility
closure leaves no excluded column inside the golden.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine.generation._reproducibility import engine_minor_version
from tests.parity import _f0_fixtures as fx
from tests.parity import _fr0_refreeze as rf
from tests.parity import _fr0_semantic_gate as gate
from tests.parity._f0_canonical import table_fingerprint
from tests.parity._f0_expected import EXPECTED

MANIFEST = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "phase_f_fr0" / "manifest.json").read_text()
)
GATED = sorted(rf.GATES)


# ---- manifest ---------------------------------------------------------------------------------


def test_manifest_pins_the_running_engine_minor_version() -> None:
    assert MANIFEST["engine_minor"] == engine_minor_version(), (
        "the golden is pinned to one engine minor version; re-freeze deliberately when it moves"
    )


def test_manifest_covers_every_generate_tables_fixture() -> None:
    expected = {f.fid for f in fx.FIXTURES if f.entry == fx.ENTRY_GENERATE_TABLES}
    assert set(MANIFEST["fixtures"]) == expected


@pytest.mark.parametrize("fid", sorted(MANIFEST["fixtures"]))
def test_manifest_envelope_and_digests_match_the_checked_in_golden(fid: str) -> None:
    entry = MANIFEST["fixtures"][fid]
    fixture = fx.FIXTURES_BY_ID[fid]
    assert rf.fixture_envelope_digest(fixture) == entry["envelope_digest"]
    assert entry["tables"] == {n: fp["table"] for n, fp in EXPECTED[fid].items()}
    actual = {name: table_fingerprint(t)["table"] for name, t in fixture.run().items()}
    assert actual == entry["tables"]


def test_refrozen_fixtures_record_their_previous_digest_and_reason() -> None:
    assert set(MANIFEST["refrozen"]) == set(rf.REFROZEN)
    for fid, info in MANIFEST["refrozen"].items():
        assert info["reason"]
        assert MANIFEST["fixtures"][fid]["status"] == "refrozen"
        for table, previous in info["previous_tables"].items():
            assert previous != MANIFEST["fixtures"][fid]["tables"][table]
    unchanged = [f for f, e in MANIFEST["fixtures"].items() if e["status"] == "unchanged"]
    assert set(unchanged) == set(MANIFEST["fixtures"]) - set(rf.REFROZEN) - set(rf.ADDED)
    assert set(MANIFEST["added"]) == set(rf.ADDED)
    for fid in rf.ADDED:
        assert MANIFEST["fixtures"][fid]["status"] == "added"


def test_every_slice_digest_is_a_real_earlier_digest_of_a_moved_table() -> None:
    """`by_slice` records the digest each slice replaced; none may equal today's golden."""
    for fid, info in MANIFEST["refrozen"].items():
        for slice_name, previous in info.get("by_slice", {}).items():
            for table, digest in previous.items():
                assert len(digest) == 64, (fid, slice_name, table)
                assert digest != MANIFEST["fixtures"][fid]["tables"][table], (fid, slice_name)
    for fid, previous in rf.REFROZEN_PIPELINE.items():
        for table, digest in previous.items():
            assert digest != EXPECTED[fid][table]["table"], (fid, table)
    assert set(MANIFEST["refrozen_outside_manifest"]) == set(rf.REFROZEN_PIPELINE)


def test_envelope_digest_moves_with_each_pinned_input() -> None:
    from decoy_engine.generation._reproducibility import build_envelope

    cfg = fx.cfg_live_faker_default_locale()
    base = build_envelope(cfg, instance_default_locale="fr_FR", controlled_clock="c").digest()
    assert (
        base == build_envelope(cfg, instance_default_locale="fr_FR", controlled_clock="c").digest()
    )
    changed = [
        build_envelope(cfg, instance_default_locale="de_DE", controlled_clock="c"),
        build_envelope(cfg, instance_default_locale="fr_FR", controlled_clock="d"),
        build_envelope(
            cfg, instance_default_locale="fr_FR", controlled_clock="c", derive_key_digest="k"
        ),
        build_envelope(
            cfg, instance_default_locale="fr_FR", controlled_clock="c", pinned_input_digests=["p"]
        ),
        build_envelope(
            {**cfg, "global_settings": {"seed": 1}},
            instance_default_locale="fr_FR",
            controlled_clock="c",
        ),
    ]
    assert len({base, *(e.digest() for e in changed)}) == len(changed) + 1


def test_the_golden_contains_no_reproducibility_excluded_column() -> None:
    for fid, entry in MANIFEST["fixtures"].items():
        assert entry["excluded_columns"] == [], f"{fid}: an excluded column was re-frozen"


# ---- the semantic gate ------------------------------------------------------------------------


def test_every_fixture_is_gated_or_explained() -> None:
    assert set(rf.GATES) | set(rf.NO_RELATION_CHECKS) | {"statistical"} >= {
        f.fid for f in fx.FIXTURES
    }
    assert not set(rf.GATES) & set(rf.NO_RELATION_CHECKS)


@pytest.mark.parametrize("fid", GATED)
def test_semantic_gate_passes_for_the_real_output(fid: str) -> None:
    rf.run_gate(fid, fx.FIXTURES_BY_ID[fid].run())


# ---- mutants: each preserves marginals, breaks one relation, and must fail the gate ----------


def _replace_column(table: pa.Table, name: str, values: list[Any]) -> pa.Table:
    idx = table.column_names.index(name)
    return table.set_column(idx, name, pa.array(values, type=table.schema.field(name).type))


def _mutated(fid: str, mutate: Any) -> dict[str, pa.Table]:
    return mutate(dict(fx.FIXTURES_BY_ID[fid].run()))


def _roll_nonnull(values: list[Any], shift: int = 1) -> list[Any]:
    idx = [i for i, v in enumerate(values) if v is not None]
    moved = [values[idx[(k + shift) % len(idx)]] for k in range(len(idx))]
    out = list(values)
    for i, v in zip(idx, moved, strict=True):
        out[i] = v
    return out


def mutant_permuted_composite_field(t: dict[str, pa.Table]) -> dict[str, pa.Table]:
    bundle = t["name_email_pool"]
    return {
        "name_email_pool": _replace_column(
            bundle, "email", _roll_nonnull(bundle.column("email").to_pylist())
        )
    }


def mutant_dropped_fk_member(t: dict[str, pa.Table]) -> dict[str, pa.Table]:
    parent = t["ref_parent"]
    victim = next(v for v in parent.column("pkey").to_pylist() if v is not None)
    keep = [v != victim for v in parent.column("pkey").to_pylist()]
    return {**t, "ref_parent": parent.filter(pa.array(keep))}


def mutant_permuted_sequence(t: dict[str, pa.Table]) -> dict[str, pa.Table]:
    seq = t["seq"]
    return {"seq": _replace_column(seq, "plain", list(reversed(seq.column("plain").to_pylist())))}


def mutant_permuted_sequential_reference(t: dict[str, pa.Table]) -> dict[str, pa.Table]:
    child = t["ref_child"]
    return {
        **t,
        "ref_child": _replace_column(
            child, "r_seq", _roll_nonnull(child.column("r_seq").to_pylist())
        ),
    }


def mutant_broken_cardinality(t: dict[str, pa.Table]) -> dict[str, pa.Table]:
    child = t["ref_child"]
    vals = child.column("r_bounded").to_pylist()
    return {**t, "ref_child": _replace_column(child, "r_bounded", [vals[0]] * len(vals))}


def mutant_broken_null_propagation(t: dict[str, pa.Table]) -> dict[str, pa.Table]:
    tbl = t["derived"]
    vals = tbl.column("from_nullable").to_pylist()
    return {"derived": _replace_column(tbl, "from_nullable", [0 if v is None else v for v in vals])}


def mutant_lost_declared_type(t: dict[str, pa.Table]) -> dict[str, pa.Table]:
    tbl = t["nulls"]
    idx = tbl.column_names.index("cat_all_null")
    return {**t, "nulls": tbl.set_column(idx, "cat_all_null", pa.nulls(tbl.num_rows))}


MUTANTS = [
    (
        "permuted_composite_field",
        "composite_name_email_seeded_pool",
        mutant_permuted_composite_field,
        "email does not match",
    ),
    ("dropped_fk_member", "reference", mutant_dropped_fk_member, "not in the parent key set"),
    ("permuted_sequence", "sequence", mutant_permuted_sequence, "sequence order"),
    (
        "permuted_sequential_reference",
        "reference",
        mutant_permuted_sequential_reference,
        "sequential reference order",
    ),
    ("broken_cardinality", "reference", mutant_broken_cardinality, "per-parent counts"),
    ("broken_null_propagation", "derived", mutant_broken_null_propagation, "null state differs"),
    ("lost_declared_type", "nulls", mutant_lost_declared_type, "declared string type"),
]


@pytest.mark.parametrize(("name", "fid", "mutate", "match"), MUTANTS, ids=[m[0] for m in MUTANTS])
def test_relation_breaking_mutant_fails_the_gate(
    name: str, fid: str, mutate: Any, match: str
) -> None:
    rf.GATES[fid](fx.FIXTURES_BY_ID[fid].run())  # the unmutated output passes
    with pytest.raises(AssertionError, match=match):
        rf.GATES[fid](_mutated(fid, mutate))


def test_permutation_mutants_keep_every_marginal() -> None:
    """The point of the relation checks: these mutants are invisible to a histogram."""
    seq = fx.FIXTURES_BY_ID["sequence"].run()["seq"].column("plain").to_pylist()
    assert sorted(seq) == sorted(
        mutant_permuted_sequence({"seq": fx.FIXTURES_BY_ID["sequence"].run()["seq"]})["seq"]
        .column("plain")
        .to_pylist()
    )
    child = fx.FIXTURES_BY_ID["reference"].run()["ref_child"].column("r_seq").to_pylist()
    rolled = _roll_nonnull(child)
    assert sorted(map(str, child)) == sorted(map(str, rolled)) and child != rolled


def test_frequency_bias_mutant_fails_the_marginal_gate() -> None:
    builder, check = rf.LARGE_GATES["categorical"]
    sample = builder()
    check(sample)  # the real sample passes
    weighted = sample["cat"].column("weighted").to_pylist()
    rng = np.random.default_rng(0)
    for i in rng.choice(len(weighted), size=int(0.15 * len(weighted)), replace=False):
        weighted[int(i)] = "red"
    biased = {"cat": _replace_column(sample["cat"], "weighted", weighted)}
    with pytest.raises(AssertionError, match="frequency"):
        check(biased)


def test_null_rate_bias_mutant_fails_the_null_gate() -> None:
    builder, check = rf.LARGE_GATES["nulls"]
    sample = builder()
    check(sample)
    vals = sample["nulls"].column("cat_partial").to_pylist()
    biased = [None if i % 5 == 0 else v for i, v in enumerate(vals)]
    with pytest.raises(AssertionError, match="null fraction"):
        check({"nulls": _replace_column(sample["nulls"], "cat_partial", biased)})


def test_gate_helpers_have_teeth_in_isolation() -> None:
    with pytest.raises(AssertionError):
        gate.check_marginal(["a"] * 100, ["a", "b"])
    with pytest.raises(AssertionError):
        gate.check_marginal(["z", "a", "b"], ["a", "b"])
    with pytest.raises(AssertionError):
        gate.check_sequence(["1", "1", "1"], start=1, step=1)
