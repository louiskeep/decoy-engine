"""SC3 parity: out-of-core Group (b) strategies vs the pandas oracle.

`test_out_of_core_fk_parity.py` pins SC1's `hash/redact/truncate/passthrough`.
This file pins the SC3 widening: the per-value / source-conditioned Group (b)
strategies ported onto the out-of-core kernel (`fpe`, `text_redact`,
`categorical`) are byte-identical to `PandasExecutionAdapter.run` (the oracle)
when masking payload columns of an FK job, and the deliberately-deferred ones
(`faker`, `bucketize`, `date_shift`) plus the unsupported shapes (Group (b) as
an FK parent key, non-deterministic categorical) are a fail-closed gate MISS
(the route never runs, so it never emits divergent output; the job falls back
to full-frame).

The oracle is the adapter (same boundary the SC1 parity harness uses): the
out-of-core route has no row-error/quarantine channel, and neither does the
adapter surface it in `.outputs`, so output-parity is the right invariant here.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_JOB_SEED = b"\x11" * 8


def _seed(
    strategy: str,
    *,
    namespace: str | None = None,
    provider_config: tuple[tuple[str, Any], ...] = (),
    deterministic: bool | None = None,
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=(namespace is not None) if deterministic is None else deterministic,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(per_table: tuple[tuple[str, TableSeed], ...]) -> Any:
    return SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=_JOB_SEED, per_table=per_table))


def _fold(v: object) -> object:
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _comparable(table: pa.Table) -> dict[str, list[object]]:
    return {name: [_fold(v) for v in col] for name, col in table.to_pydict().items()}


def _assert_value_equal(oracle: pa.Table, ooc: pa.Table, label: str) -> None:
    got, want = _comparable(ooc), _comparable(oracle)
    assert set(got) == set(want), f"{label}: column mismatch {set(got)} vs {set(want)}"
    for name in want:
        assert got[name] == want[name], (
            f"{label}: column {name!r} diverges\n oracle={want[name]}\n    ooc={got[name]}"
        )


def _gate_admits(plan: Any, graph: RelationshipGraph) -> bool:
    work = order_work(build_work_list(plan, _REG), graph)
    return check_out_of_core_compatibility(plan, work, graph).accepted


def _gate_codes(plan: Any, graph: RelationshipGraph) -> set[str]:
    work = order_work(build_work_list(plan, _REG), graph)
    return {r.code for r in check_out_of_core_compatibility(plan, work, graph).rejections}


# ---------------------------------------------------------------------------
# Payload-column parity: parent + child each carry a Group (b) payload column,
# and the FK key stays hash (an SC1-supported key). Orphans + nulls included.
# ---------------------------------------------------------------------------

# A source column shaped for the strategy under test. Values are chosen so the
# transform actually does visible work (fpe permutes, text_redact splices a
# span, categorical remaps) rather than trivially passing through.
_PAYLOADS: dict[str, tuple[ColumnSeed, list[str | None]]] = {
    "fpe": (
        _seed("fpe", namespace="pii", provider_config=(("charset", "digits"),)),
        ["123456789", "000111222", None, "999", "42", "7654321"],
    ),
    "fpe_preserve_sep": (
        _seed(
            "fpe",
            namespace="pii2",
            provider_config=(("charset", "digits"), ("preserve_separators", True)),
        ),
        ["123-45-6789", "111-22-3333", None, "555-55-5555", "000-00-0000", "321-54-9876"],
    ),
    "text_redact": (
        _seed("text_redact", provider_config=(("token", "[X]"),)),
        [
            "call me at 415-555-1234 today",
            "ssn 123-45-6789 on file",
            None,
            "no pii here at all",
            "email a@b.com please",
            "plain text",
        ],
    ),
    "text_redact_label": (
        _seed("text_redact", provider_config=(("label_token", True),)),
        [
            "ssn 123-45-6789",
            "phone 415-555-1234",
            None,
            "nothing",
            "two 111-22-3333 and 415-555-9999",
            "x",
        ],
    ),
    "categorical": (
        _seed("categorical", namespace="cat", provider_config=(("categories", ("A", "B", "C")),)),
        ["alpha", "beta", None, "gamma", "delta", "epsilon"],
    ),
    "categorical_weighted": (
        _seed(
            "categorical",
            namespace="catw",
            provider_config=(("categories", ("R", "S", "T")), ("weights", (0.6, 0.3, 0.1))),
        ),
        ["one", "two", None, "three", "four", "five"],
    ),
    # SC4 carry-forward (SC3 MEDIUM): exercise the live kernel branches the
    # SC3 suite did not pin -- fpe validate_luhn / checksum / non-digit charset /
    # fpe_join_group tweak, text_redact detector-subset, categorical from_profile.
    # Each reuses the same primitive the full-frame handler cites, so parity holds;
    # pinning them stops a future edit from diverging one branch unnoticed.
    "fpe_validate_luhn": (
        _seed(
            "fpe",
            namespace="pvl",
            provider_config=(("charset", "digits"), ("validate_luhn", True)),
        ),
        [
            "4111111111111111",
            "5555555555554444",
            None,
            "4012888888881881",
            "6011111111111117",
            "38520000023237",
        ],
    ),
    "fpe_checksum_luhn": (
        _seed(
            "fpe",
            namespace="pcl",
            provider_config=(("charset", "digits"), ("checksum", "luhn")),
        ),
        [
            "4111111111111111",
            "5555555555554444",
            None,
            "4012888888881881",
            "6011111111111117",
            "38520000023237",
        ],
    ),
    "fpe_alphanum": (
        _seed("fpe", namespace="pan", provider_config=(("charset", "ALPHANUM"),)),
        ["Abc123", "Xyz789", None, "Test42", "Data99", "Code77"],
    ),
    "fpe_join_group": (
        _seed(
            "fpe",
            namespace="pjg",
            provider_config=(("charset", "digits"), ("fpe_join_group", "grp")),
        ),
        ["123456789", "000111222", None, "999", "42", "7654321"],
    ),
    "text_redact_detector_subset": (
        _seed("text_redact", provider_config=(("detectors", ("ssn",)), ("token", "[S]"))),
        [
            "ssn 123-45-6789 and phone 415-555-1234",
            "just 987-65-4321 here",
            None,
            "no id at all",
            "two 111-22-3333 x 222-33-4444",
            "plain text",
        ],
    ),
    "categorical_from_profile": (
        _seed(
            "categorical",
            namespace="cfp",
            provider_config=(
                ("from_profile", True),
                ("categories", ("A", "B", "C")),
                ("weights", (0.5, 0.3, 0.2)),
            ),
        ),
        ["alpha", "beta", None, "gamma", "delta", "epsilon"],
    ),
}


def _payload_edge_job(
    payload_seed: ColumnSeed, payload_vals: list[str | None], *, policy: OrphanPolicy
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    n = len(payload_vals)
    key = _seed("hash", namespace="kns")
    # child rows 0..n-1 reference parents; one is an orphan when the policy allows.
    parent = pa.table(
        {
            "pk": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "pay": pa.array(payload_vals, type=pa.string()),
        }
    )
    child_fk = [f"p{i}" for i in range(n)]
    if policy is not OrphanPolicy.FAIL:
        child_fk[1] = "orphan-x"
    child = pa.table(
        {
            "fk": pa.array(child_fk, type=pa.string()),
            "cpay": pa.array(list(reversed(payload_vals)), type=pa.string()),
        }
    )
    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("pk", key), ("pay", payload_seed)), per_group=())),
            ("child", TableSeed(per_column=(("fk", key), ("cpay", payload_seed)), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="kns",
                orphan_policy=policy,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child": child}, graph


@pytest.mark.parametrize("kind", list(_PAYLOADS))
@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.FAIL])
def test_group_b_payload_parity(kind: str, policy: OrphanPolicy) -> None:
    payload_seed, payload_vals = _PAYLOADS[kind]
    plan, sources, graph = _payload_edge_job(payload_seed, payload_vals, policy=policy)
    assert _gate_admits(plan, graph), f"{kind}/{policy.name}: expected gate to admit"
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    ooc = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    for table in oracle.outputs:
        _assert_value_equal(
            oracle.outputs[table], ooc.outputs[table], f"{kind}/{policy.name}:{table}"
        )


@pytest.mark.parametrize("kind", list(_PAYLOADS))
def test_group_b_payload_actually_transforms(kind: str) -> None:
    """Guard against a no-op port: the masked payload must differ from the source
    for at least one non-null row (otherwise a broken port that returned the
    input unchanged would still pass the parity check against a broken oracle)."""
    payload_seed, payload_vals = _PAYLOADS[kind]
    plan, sources, graph = _payload_edge_job(
        payload_seed, payload_vals, policy=OrphanPolicy.PRESERVE
    )
    ooc = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    src = sources["parent"].column("pay").to_pylist()
    out = ooc.outputs["parent"].column("pay").to_pylist()
    changed = [o for s, o in zip(src, out, strict=True) if s is not None and o != s]
    assert changed, f"{kind}: masked payload never differs from source (no-op port?)"


def test_text_redact_non_string_token_passthrough_parity() -> None:
    """SC4 carry-forward (SC3 MEDIUM): a non-string token makes the text_redact
    handler pass the column through unchanged. Pin that the out-of-core kernel's
    passthrough matches the oracle rather than, e.g., stringifying the token."""
    seed = _seed("text_redact", provider_config=(("token", 0),))
    plan, sources, graph = _payload_edge_job(
        seed,
        ["ssn 123-45-6789", "x", None, "415-555-1234", "plain", "y"],
        policy=OrphanPolicy.PRESERVE,
    )
    assert _gate_admits(plan, graph)
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    ooc = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], ooc.outputs[table], f"nonstr_token:{table}")


def test_text_redact_ner_path_parity() -> None:
    """SC4 carry-forward (SC3 MEDIUM): the NER-augmented text_redact path. Skipped
    unless spaCy + the model are installed (the model is not pip-resolvable, so this
    is environment-gated); when present, the out-of-core kernel reuses the same
    `iter_ner_spans` the oracle does, so parity must hold."""
    from decoy_engine.storm.ner import model_installed, spacy_installed

    if not (spacy_installed() and model_installed()):
        pytest.skip("NER spaCy model not installed; text_redact NER path not exercisable here")
    seed = _seed("text_redact", provider_config=(("ner", {"model": "en_core_web_sm"}),))
    plan, sources, graph = _payload_edge_job(
        seed,
        ["John Smith lives in Boston", "call 415-555-1234", None, "no entities", "Jane Doe", "x"],
        policy=OrphanPolicy.PRESERVE,
    )
    assert _gate_admits(plan, graph)
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    ooc = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], ooc.outputs[table], f"ner:{table}")


# ---------------------------------------------------------------------------
# Fail-closed MISS: Group (b) as an FK PARENT KEY, non-deterministic
# categorical, and the deferred strategies.
# ---------------------------------------------------------------------------


def _key_strategy_job(key_seed: ColumnSeed) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    parent = pa.table({"pk": pa.array(["100", "200", "300"], type=pa.string())})
    child = pa.table({"fk": pa.array(["100", "200", "300"], type=pa.string())})
    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("pk", key_seed),), per_group=())),
            ("child", TableSeed(per_column=(("fk", key_seed),), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace=key_seed.namespace or "kns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child": child}, graph


@pytest.mark.parametrize(
    "key_seed",
    [
        _seed("fpe", namespace="pii", provider_config=(("charset", "digits"),)),
        _seed("categorical", namespace="cat", provider_config=(("categories", ("A", "B")),)),
        _seed("text_redact"),
    ],
)
def test_group_b_as_parent_key_is_gate_miss(key_seed: ColumnSeed) -> None:
    plan, _sources, graph = _key_strategy_job(key_seed)
    assert not _gate_admits(plan, graph)
    assert "out_of_core_parent_strategy_unsupported" in _gate_codes(plan, graph)


def test_nondeterministic_categorical_is_gate_miss() -> None:
    seed = _seed(
        "categorical",
        namespace="cat",
        provider_config=(("categories", ("A", "B", "C")),),
        deterministic=False,
    )
    plan, sources, graph = _payload_edge_job(seed, ["a", "b", "c"], policy=OrphanPolicy.PRESERVE)
    assert not _gate_admits(plan, graph)
    assert "out_of_core_categorical_nondeterministic_unsupported" in _gate_codes(plan, graph)


@pytest.mark.parametrize(
    ("strategy", "provider_config", "code"),
    [
        ("bucketize", (("width", 10),), "out_of_core_row_error_strategy_unsupported"),
        (
            "date_shift",
            (("min_days", -5), ("max_days", 5)),
            "out_of_core_row_error_strategy_unsupported",
        ),
        ("faker", (), "out_of_core_faker_pool_unsupported"),
    ],
)
def test_deferred_group_b_is_documented_gate_miss(
    strategy: str, provider_config: tuple[tuple[str, Any], ...], code: str
) -> None:
    seed = _seed(strategy, namespace="dns", provider_config=provider_config)
    plan, sources, graph = _payload_edge_job(seed, ["1", "2", "3"], policy=OrphanPolicy.PRESERVE)
    assert not _gate_admits(plan, graph)
    assert code in _gate_codes(plan, graph)
