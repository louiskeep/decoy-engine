"""Property + metamorphic invariants for the determinism/_derive crypto core.

TQ-1 (Test-Quality Program, continuation of the TQ-0 pilot documented in
`docs/quality/module-test-quality-playbook.md`). `determinism/_derive.py`
is a MANDATORY-100% crypto module (playbook "Definition of done and bars"):
its `derive(seed, namespace, source) -> bytes` is the single primitive every
deterministic-mode column in the engine routes through, and every
cross-process golden fingerprint and vaulted-unmask round-trip rests on its
byte-stability. The existing `tests/unit/determinism/test_derive.py` suite
is example-based (fixed seed/namespace/source literals); these tests are
oracles independent of the implementation, generated over the input domain
with Hypothesis, so a mutation that only breaks on an untested corner of
that domain still gets caught.

Invariants and their source (module docstring of `_derive.py` unless noted):

- DETERMINISM ("same inputs produce byte-identical output... pure function",
  `derive` docstring): `test_derive_is_deterministic_across_calls`,
  `test_derive_index_is_deterministic`, `test_derive_value_is_deterministic`,
  `test_repeated_calls_are_stable_regardless_of_call_order` (SEED_PROTOCOL_
  VERSION / byte-stability metamorphic relation: two calls with the same
  inputs are byte-identical, exercised over the full Hypothesis input
  domain rather than one fixed example).
- DOMAIN SEPARATION / no-collision ("Length-prefixing on namespace + source
  makes the concatenation injective", `_derive.py` module docstring; and
  each of `seed`/`namespace`/`source` is mixed into the HMAC input):
  `test_different_seed_changes_output`, `test_different_namespace_changes_
  output`, `test_different_source_changes_output`,
  `test_length_prefix_prevents_concatenation_collision` (the injective-
  concatenation claim, generalized over random split points instead of the
  single "abc"/"def" vs "abcd"/"ef" example in the unit suite).
- SEED SENSITIVITY: covered by `test_different_seed_changes_output` across
  both valid seed lengths (8-byte job_seed, 32-byte mask_key per DE-02) and
  cross-length pairs.
- RANGE / BOUNDS (`derive_index` docstring: "Return a stable index in
  `[0, pool_size)`"): `test_derive_index_within_bounds`,
  `test_derive_index_matches_manual_computation`,
  `test_derive_index_overflow_raises`, `test_derive_index_invalid_pool_size_
  raises`.
- COMPOSITION (`derive_value` docstring: "Calls `domain.from_bytes(b)`
  exactly once with the 32-byte output of `derive(...)`"):
  `test_derive_value_composes_with_derive_via_identity_domain`.
- VALIDATION invariants (`derive` docstring "Raises: DeterminismError on
  invalid inputs"): `test_seed_wrong_length_raises_for_any_invalid_length`,
  `test_empty_namespace_raises_regardless_of_seed_and_source`.
- DeriveContext byte-parity ("Output is byte-identical to
  `derive(seed, namespace, source)`", `DeriveContext` docstring):
  `test_derive_context_matches_scalar_derive`, generalized over the input
  domain from the unit suite's fixed 5-source list.

Run:  pytest tests/property/test_derive_invariants.py -q
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from decoy_engine.determinism import (
    DeterminismError,
    IdentityDomain,
    derive,
    derive_index,
    derive_value,
)
from decoy_engine.determinism._derive import _POOL_SIZE_MAX, _SEED_LENGTHS, DeriveContext

# Match the pilot's audit profile (test_ri_graph_invariants.py): more
# examples than the 100-example default, no deadline (HMAC-SHA256 calls are
# cheap but Hypothesis shrinking can trip the 200ms wall), print_blob so any
# counterexample is replayable.
settings.register_profile(
    "audit",
    max_examples=300,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("audit")

# --------------------------------------------------------------------------
# Strategies. `derive`'s only two valid seed lengths are 8 (job_seed) and 32
# (mask_key, DE-02); everything else must raise. `namespace` excludes
# surrogate code points (category "Cs") because those cannot round-trip
# through `str.encode("utf-8")`, which every namespace hits before hashing.
# --------------------------------------------------------------------------

_valid_seed = st.one_of(
    st.binary(min_size=8, max_size=8),
    st.binary(min_size=32, max_size=32),
)
_namespace_text = st.text(
    alphabet=st.characters(exclude_categories=("Cs",)), min_size=1, max_size=64
)
_source = st.binary(max_size=256)


# --------------------------------------------------------------------------
# DETERMINISM
# --------------------------------------------------------------------------


@given(_valid_seed, _namespace_text, _source)
def test_derive_is_deterministic_across_calls(seed, namespace, source) -> None:
    """THE guarantee the whole engine's reproducibility rests on: identical
    (seed, namespace, source) produces byte-identical output, always."""
    a = derive(seed, namespace, source)
    b = derive(seed, namespace, source)
    assert a == b
    assert len(a) == 32


@given(_valid_seed, _namespace_text, _source, st.integers(min_value=1, max_value=_POOL_SIZE_MAX))
def test_derive_index_is_deterministic(seed, namespace, source, pool_size) -> None:
    a = derive_index(seed, namespace, source, pool_size=pool_size)
    b = derive_index(seed, namespace, source, pool_size=pool_size)
    assert a == b


@given(_valid_seed, _namespace_text, _source)
def test_derive_value_is_deterministic(seed, namespace, source) -> None:
    domain = IdentityDomain()
    a = derive_value(seed, namespace, source, domain=domain)
    b = derive_value(seed, namespace, source, domain=domain)
    assert a == b


@given(_valid_seed, _namespace_text, _source, _valid_seed, _namespace_text, _source)
def test_repeated_calls_are_stable_regardless_of_call_order(
    seed_a, ns_a, src_a, seed_b, ns_b, src_b
) -> None:
    """Metamorphic byte-stability: interleaving calls for a DIFFERENT input
    between two calls for the SAME input must not perturb the result. The
    only process-global state `derive` touches is the immutable `_SALT` and
    `SEED_PROTOCOL_VERSION` constants, so no call may leak state into a
    later, unrelated call (the process-stability axis this pins in-process;
    `tests/unit/determinism/test_process_stability.py` pins the
    cross-process axis via subprocess)."""
    first = derive(seed_a, ns_a, src_a)
    derive(seed_b, ns_b, src_b)  # unrelated call in between
    second = derive(seed_a, ns_a, src_a)
    assert first == second


# --------------------------------------------------------------------------
# DOMAIN SEPARATION / no-collision
# --------------------------------------------------------------------------


@given(_valid_seed, _valid_seed, _namespace_text, _source)
def test_different_seed_changes_output(seed_a, seed_b, namespace, source) -> None:
    """A different seed (job_seed or mask_key, either length) must not leak
    seed-independent output: `derive` is the IKM-bound root of the whole
    determinism envelope."""
    assume(seed_a != seed_b)
    assert derive(seed_a, namespace, source) != derive(seed_b, namespace, source)


@given(_valid_seed, _namespace_text, _namespace_text, _source)
def test_different_namespace_changes_output(seed, ns_a, ns_b, source) -> None:
    assume(ns_a != ns_b)
    assert derive(seed, ns_a, source) != derive(seed, ns_b, source)


@given(_valid_seed, _namespace_text, _source, _source)
def test_different_source_changes_output(seed, namespace, src_a, src_b) -> None:
    assume(src_a != src_b)
    assert derive(seed, namespace, src_a) != derive(seed, namespace, src_b)


@st.composite
def _combined_string_and_two_splits(draw: st.DrawFn) -> tuple[str, int, int]:
    """A random string plus two DIFFERENT code-point split points, drawn
    together so both come from the same Hypothesis example (and shrink
    together on failure)."""
    combined = draw(
        st.text(alphabet=st.characters(exclude_categories=("Cs",)), min_size=2, max_size=40)
    )
    split_a = draw(st.integers(min_value=1, max_value=len(combined) - 1))
    split_b = draw(st.integers(min_value=1, max_value=len(combined) - 1))
    assume(split_a != split_b)
    return combined, split_a, split_b


@given(_valid_seed, _combined_string_and_two_splits())
def test_length_prefix_prevents_concatenation_collision(seed, split_data) -> None:
    """DOMAIN SEPARATION, generalized: `_derive.py`'s module docstring
    states length-prefixing "makes the concatenation injective (without it,
    'abc' + 'def' and 'abcd' + 'ef' would collide)". The unit suite
    (`test_length_prefix_prevents_collision`) pins exactly that one example;
    this property draws a random string and two different split points,
    splits it two ways into (namespace, source), and asserts the outputs
    still differ -- the injective-concatenation claim holds for the whole
    domain, not just the one hand-picked pair. UTF-8 encoding is
    concatenative (encode(a + b) == encode(a) + encode(b)), so splitting the
    python str at two different code-point offsets and re-encoding each half
    independently reproduces the "same total bytes, different split" setup
    the docstring describes."""
    combined, split_a, split_b = split_data
    ns_a, src_a = combined[:split_a], combined[split_a:].encode("utf-8")
    ns_b, src_b = combined[:split_b], combined[split_b:].encode("utf-8")
    assert derive(seed, ns_a, src_a) != derive(seed, ns_b, src_b)


# --------------------------------------------------------------------------
# RANGE / BOUNDS (derive_index)
# --------------------------------------------------------------------------


@given(_valid_seed, _namespace_text, _source, st.integers(min_value=1, max_value=_POOL_SIZE_MAX))
def test_derive_index_within_bounds(seed, namespace, source, pool_size) -> None:
    """`derive_index` docstring: "Return a stable index in
    `[0, pool_size)`". Holds for every valid pool_size up to the documented
    `2**56` ceiling, not just the small pool sizes (100, 1000, 10) the unit
    suite hand-picks."""
    idx = derive_index(seed, namespace, source, pool_size=pool_size)
    assert 0 <= idx < pool_size


@given(_valid_seed, _namespace_text, _source, st.integers(min_value=1, max_value=_POOL_SIZE_MAX))
def test_derive_index_matches_manual_computation(seed, namespace, source, pool_size) -> None:
    """Composition: `derive_index`'s documented implementation is "the first
    8 bytes of `derive(...)`, interpreted as big-endian uint64, `% pool_size`
    " -- assert the public function actually IS that composition, not just
    that it happens to land in range."""
    expected = int.from_bytes(derive(seed, namespace, source)[:8], "big") % pool_size
    assert derive_index(seed, namespace, source, pool_size=pool_size) == expected


@given(
    _valid_seed,
    _namespace_text,
    _source,
    st.integers(min_value=_POOL_SIZE_MAX + 1, max_value=_POOL_SIZE_MAX * 4),
)
def test_derive_index_overflow_raises(seed, namespace, source, pool_size) -> None:
    with pytest.raises(DeterminismError) as excinfo:
        derive_index(seed, namespace, source, pool_size=pool_size)
    assert excinfo.value.code == "pool_size_overflow"


@given(_valid_seed, _namespace_text, _source, st.integers(min_value=-(2**32), max_value=0))
def test_derive_index_invalid_pool_size_raises(seed, namespace, source, pool_size) -> None:
    with pytest.raises(DeterminismError) as excinfo:
        derive_index(seed, namespace, source, pool_size=pool_size)
    assert excinfo.value.code == "pool_size_invalid"


# --------------------------------------------------------------------------
# COMPOSITION (derive_value)
# --------------------------------------------------------------------------


@given(_valid_seed, _namespace_text, _source)
def test_derive_value_composes_with_derive_via_identity_domain(seed, namespace, source) -> None:
    """Metamorphic composition: `derive_value`'s contract is "calls
    `domain.from_bytes(b)` exactly once with the 32-byte output of
    `derive(...)`". `IdentityDomain.from_bytes` returns its input unchanged,
    so `derive_value(..., domain=IdentityDomain())` must equal `derive(...)`
    for the whole input domain, not just the fixed example in the unit
    suite."""
    assert derive_value(seed, namespace, source, domain=IdentityDomain()) == derive(
        seed, namespace, source
    )


# --------------------------------------------------------------------------
# VALIDATION
# --------------------------------------------------------------------------


@given(
    st.integers(min_value=0, max_value=64).filter(lambda n: n not in _SEED_LENGTHS),
    _namespace_text,
    _source,
)
def test_seed_wrong_length_raises_for_any_invalid_length(bad_length, namespace, source) -> None:
    """Every seed length other than the two documented valid ones (8, 32)
    must raise `seed_wrong_length`, not just the +/-1 boundary cases
    (7, 9) the unit suite pins."""
    seed = b"\x00" * bad_length
    with pytest.raises(DeterminismError) as excinfo:
        derive(seed, namespace, source)
    assert excinfo.value.code == "seed_wrong_length"


@given(_valid_seed, _source)
def test_empty_namespace_raises_regardless_of_seed_and_source(seed, source) -> None:
    with pytest.raises(DeterminismError) as excinfo:
        derive(seed, "", source)
    assert excinfo.value.code == "namespace_empty"


# --------------------------------------------------------------------------
# DeriveContext byte-parity (composition: the amortised-HKDF fast path must
# never diverge from the scalar function it is an optimization of)
# --------------------------------------------------------------------------


@given(_valid_seed, _namespace_text, _source)
def test_derive_context_matches_scalar_derive(seed, namespace, source) -> None:
    """`DeriveContext` docstring: "Output is byte-identical to
    `derive(seed, namespace, source)` for the same inputs." The unit suite
    pins this over a fixed 5-source list for one seed/namespace; this
    property generalizes it over the full input domain, so a mutation that
    only breaks the fast path on an untested seed/namespace/source shape
    still gets caught."""
    ctx = DeriveContext.for_column(seed, namespace)
    assert ctx.derive_source(namespace, source) == derive(seed, namespace, source)
