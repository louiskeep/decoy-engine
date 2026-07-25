"""Property + metamorphic invariants for `keyprovider.py`, the keyed-mask
SECRET BOUNDARY (DE-02, crypto Sprint 4; TQ-1, Test-Quality Program).

`tests/unit/test_de02_keyprovider.py` already proves PATH-COMPLETENESS: every
keyed strategy's output changes under a real secret and the fail-closed gate
fires at every public entry point, through the full `run_pipeline` machinery.
That suite is example-based and expensive (it compiles plans and runs the
pipeline). This module is the oracle layer the playbook asks for: invariants
that must hold for EVERY input the boundary can see, stated independently of
any one example, so a mutation to the guard logic itself gets caught even if
no single example-based test happens to hit it.

The invariants come from the module's own docstring and the `MaskSecretError`
/ `require_mask_key` docstrings in `src/decoy_engine/keyprovider.py`:

- FAIL-CLOSED on a missing secret: a plan with a keyed strategy, given no
  provider (or a provider whose resolved key is short) at GA, ALWAYS raises
  `KeyedStrategyRequiresSecret` -- "an 8-byte job_seed fallback is NOT
  accepted" (module docstring; `require_mask_key` docstring's gate table).
- FAIL-CLOSED on a weak secret: `SecretKeyProvider.__post_init__` rejects any
  secret shorter than `MIN_SECRET_BYTES` (32, NIST SP 800-57 256-bit target;
  module docstring "Secret source" section) with `WeakMaskSecret`, always, and
  the boundary is a hard `<`/`>=` split -- 31 bytes rejects, 32 accepts.
- DETERMINISM: the same provider (secret + key_version) resolves the same
  `mask_key()` bytes every call ("Design" section: "a single 32-byte
  mask_root inherits every existing separation for free" presumes the root
  itself is pure).
- ISOLATION: distinct secrets -- or the same secret under a distinct
  `key_version` ("Rotating key_version deterministically re-keys the whole
  surface") -- derive distinct mask keys; `SeedKeyProvider` (identity) and
  `SecretKeyProvider` (HKDF) never collapse into each other on the same raw
  bytes.
- NO LEAKAGE: "the raw secret is NEVER serialized into the plan, logged, or
  printed" (module docstring) and the `SecretKeyProvider`/`SeedKeyProvider`
  `__repr__` overrides are explicitly there to redact (Codex MEDIUM 7,
  cited in both class docstrings) -- the raw bytes must never appear in a
  `repr()` or in any `MaskSecretError` message, including the WeakMaskSecret
  path where a short secret is itself the offending value.
- ERROR TAXONOMY: each documented failure mode (`MaskSecretError.__doc__`'s
  "Codes:" list) raises the exact `code`, not merely *an* error.

Run:  pytest tests/property/test_keyprovider_invariants.py -q
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from unittest import mock

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from decoy_engine.keyprovider import (
    _ALWAYS_KEYED_STRATEGIES,
    MIN_SECRET_BYTES,
    KeyedStrategyRequiresSecret,
    KeyProvider,
    MaskSecretError,
    MissingMaskSecret,
    SecretKeyProvider,
    SeedKeyProvider,
    WeakMaskSecret,
    key_provider_from_ref,
    mask_key_from_provider,
    plan_has_keyed_strategy,
    require_mask_key,
    resolve_key_provider,
    resolve_mask_key,
    resolve_mask_secret_ref,
)
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed

# Match the pilot's audit profile (tests/property/test_ri_graph_invariants.py):
# more examples than the 100-example default, no deadline (crypto derivation is
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
# A duck-typed Plan stand-in. `require_mask_key` / `plan_has_keyed_strategy`
# read only `plan.seed_envelope.job_seed` and `.per_table`; building a real
# compiled `Plan` (via profile_source + compile_plan, as the unit suite does)
# per Hypothesis example would be orders of magnitude slower than the
# property loop needs, and Plan is a plain frozen dataclass with no runtime
# type-check on its callers.
# --------------------------------------------------------------------------


class _MiniPlan:
    def __init__(self, seed_envelope: SeedEnvelope) -> None:
        self.seed_envelope = seed_envelope


def _col_seed(strategy: str, *, deterministic: bool = False, vault: bool = False) -> ColumnSeed:
    return ColumnSeed(
        namespace="ns",
        strategy=strategy,
        provider=None,
        backend_type="decoy_native",
        backend_version="v1",
        cardinality_mode="reuse",
        deterministic=deterministic,
        vault=vault,
    )


# A strategy name that is NOT in `_ALWAYS_KEYED_STRATEGIES` and is neither
# `deterministic` nor `vault` by default here -- the inert baseline a plan's
# non-keyed columns use.
_INERT_STRATEGY = "redact"
_non_keyed_column = _col_seed(_INERT_STRATEGY)

# Three independent ways a single column becomes keyed, per
# `_col_seed_is_keyed`'s own docstring: (1) an always-keyed strategy name,
# (2) the `deterministic` flag regardless of strategy, (3) `vault: true`
# regardless of strategy (a vault column persists reversible plaintext even
# under an anonymising strategy like `redact`).
_keyed_column_strategy = st.one_of(
    st.sampled_from(sorted(_ALWAYS_KEYED_STRATEGIES)).map(_col_seed),
    st.just(_col_seed(_INERT_STRATEGY, deterministic=True)),
    st.just(_col_seed(_INERT_STRATEGY, vault=True)),
)


@st.composite
def keyed_plan(draw: st.DrawFn) -> _MiniPlan:
    """A plan whose seed envelope carries AT LEAST ONE keyed column, mixed
    with a random number of inert columns across the table, so the property
    varies the SHAPE `plan_has_keyed_strategy` scans without changing the
    always-keyed outcome."""
    job_seed = draw(st.binary(min_size=8, max_size=8))
    n_keyed = draw(st.integers(min_value=1, max_value=3))
    n_plain = draw(st.integers(min_value=0, max_value=3))
    cols: list[tuple[str, ColumnSeed]] = [
        (f"k{i}", draw(_keyed_column_strategy)) for i in range(n_keyed)
    ]
    cols += [(f"p{i}", _non_keyed_column) for i in range(n_plain)]
    table = TableSeed(per_column=tuple(cols))
    return _MiniPlan(SeedEnvelope(job_seed=job_seed, per_table=(("t", table),)))


@st.composite
def unkeyed_plan(draw: st.DrawFn) -> _MiniPlan:
    """A plan with ZERO keyed columns (possibly zero columns at all)."""
    job_seed = draw(st.binary(min_size=8, max_size=8))
    n_plain = draw(st.integers(min_value=0, max_value=4))
    table = TableSeed(per_column=tuple((f"p{i}", _non_keyed_column) for i in range(n_plain)))
    return _MiniPlan(SeedEnvelope(job_seed=job_seed, per_table=(("t", table),)))


@dataclass(frozen=True)
class _CustomProvider:
    """A minimal `KeyProvider` whose `mask_key()` is an arbitrary fixed
    value -- used to probe the GA gate's "resolved-key-length" branch
    independent of `SecretKeyProvider`'s own construction-time guard."""

    key_version: str
    _key: bytes

    def mask_key(self) -> bytes:
        return self._key


def test_custom_provider_satisfies_the_protocol() -> None:
    # Sanity: the runtime_checkable Protocol accepts a duck-typed provider,
    # which is what the fail-closed-gate properties below rely on.
    assert isinstance(_CustomProvider(key_version="x", _key=b"y"), KeyProvider)


# --------------------------------------------------------------------------
# FAIL-CLOSED on a missing (or too-short) secret.
# --------------------------------------------------------------------------


@given(keyed_plan())
def test_keyed_plan_ga_no_provider_fails_closed(plan: _MiniPlan) -> None:
    """A keyed plan with NO provider at GA always raises
    `KeyedStrategyRequiresSecret`, never silently falls back to job_seed."""
    assert plan_has_keyed_strategy(plan)  # sanity: the strategy built a keyed plan
    with mock.patch("decoy_engine.keyprovider.is_pre_ga", return_value=False):
        with pytest.raises(KeyedStrategyRequiresSecret) as ei:
            require_mask_key(plan, None)
        assert ei.value.code == "keyed_strategy_requires_secret"
        with pytest.raises(KeyedStrategyRequiresSecret):
            resolve_key_provider(plan=plan, key_provider=None)
        with pytest.raises(KeyedStrategyRequiresSecret):
            resolve_mask_key(plan=plan, key_provider=None)


@given(keyed_plan())
def test_keyed_plan_pre_ga_no_provider_falls_back_to_job_seed(plan: _MiniPlan) -> None:
    """Complement of the previous property: pre-GA the SAME keyed plan with
    no provider legitimately returns `job_seed` (byte-identical to
    pre-DE-02). This pins the GA gate to the `is_pre_ga()` branch, not to
    some property of the plan shape."""
    with mock.patch("decoy_engine.keyprovider.is_pre_ga", return_value=True):
        assert require_mask_key(plan, None) == plan.seed_envelope.job_seed


@given(keyed_plan(), st.binary(min_size=0, max_size=MIN_SECRET_BYTES - 1))
def test_keyed_plan_ga_short_resolved_key_fails_closed(plan: _MiniPlan, weak_key: bytes) -> None:
    """Provenance-blind gate: PRESENCE of a provider is not enough at GA. A
    resolved key shorter than `MIN_SECRET_BYTES` (a `SeedKeyProvider`'s
    8-byte fallback, or a hollow custom provider) is rejected exactly like
    no provider at all (Codex BLOCKER 2, cited in `require_mask_key`'s
    docstring)."""
    provider = _CustomProvider(key_version="custom", _key=weak_key)
    with mock.patch("decoy_engine.keyprovider.is_pre_ga", return_value=False):
        with pytest.raises(KeyedStrategyRequiresSecret):
            require_mask_key(plan, provider)


@given(keyed_plan(), st.binary(min_size=MIN_SECRET_BYTES, max_size=64))
def test_keyed_plan_ga_strong_resolved_key_passes(plan: _MiniPlan, strong_key: bytes) -> None:
    """The mirror image of the short-key property: a resolved key that
    clears the length bar at GA is accepted and returned verbatim."""
    provider = _CustomProvider(key_version="custom", _key=strong_key)
    with mock.patch("decoy_engine.keyprovider.is_pre_ga", return_value=False):
        assert require_mask_key(plan, provider) == strong_key


@given(unkeyed_plan())
def test_unkeyed_plan_ga_no_provider_never_gates(plan: _MiniPlan) -> None:
    """No over-gating: a plan with zero keyed columns is inert to the secret
    gate at GA (the mask key is unused, but the call must not raise)."""
    assert not plan_has_keyed_strategy(plan)
    with mock.patch("decoy_engine.keyprovider.is_pre_ga", return_value=False):
        assert require_mask_key(plan, None) == plan.seed_envelope.job_seed
        assert resolve_key_provider(plan=plan, key_provider=None) is None


# --------------------------------------------------------------------------
# FAIL-CLOSED on a weak secret, with the exact strength boundary.
# --------------------------------------------------------------------------


@given(st.binary(min_size=0, max_size=MIN_SECRET_BYTES - 1))
def test_secret_below_min_bytes_always_rejected(secret: bytes) -> None:
    with pytest.raises(WeakMaskSecret) as ei:
        SecretKeyProvider(secret)
    assert ei.value.code == "weak_mask_secret"


@given(st.binary(min_size=MIN_SECRET_BYTES, max_size=300))
def test_secret_at_or_above_min_bytes_always_accepted(secret: bytes) -> None:
    provider = SecretKeyProvider(secret)
    assert len(provider.mask_key()) == 32  # HKDF output length, fixed regardless of input length


def test_boundary_one_byte_short_rejects_exact_threshold_accepts() -> None:
    """The exact strength boundary cited in the module docstring (NIST
    SP 800-57 256-bit target = 32 bytes): 31 rejects, 32 accepts."""
    with pytest.raises(WeakMaskSecret):
        SecretKeyProvider(b"x" * (MIN_SECRET_BYTES - 1))
    provider = SecretKeyProvider(b"x" * MIN_SECRET_BYTES)
    assert len(provider.mask_key()) == 32


@given(st.binary(min_size=1, max_size=31))
def test_weak_secret_via_ref_is_rejected_regardless_of_content(secret: bytes) -> None:
    """The weak-secret gate also fires through `key_provider_from_ref`'s
    ref -> bytes -> SecretKeyProvider path, not only direct construction.
    `min_size=1`: an EMPTY secret is a different failure mode entirely (the
    ref resolver's own `bad_secret_ref` "material is empty" guard, not the
    length gate this property targets).

    Sets the env var by hand (rather than the `monkeypatch` fixture)
    because Hypothesis flags function-scoped fixtures under `@given` as
    unsafe -- the fixture would be set up once for the whole example loop,
    not per example.
    """
    name = "DECOY_TQ_KP_WEAK_VIA_REF"
    previous = os.environ.get(name)
    os.environ[name] = secret.hex()
    try:
        with pytest.raises(WeakMaskSecret):
            key_provider_from_ref(f"env:{name}")
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


# --------------------------------------------------------------------------
# DETERMINISM.
# --------------------------------------------------------------------------


@given(st.binary(min_size=32, max_size=64), st.text(min_size=1, max_size=8))
def test_secret_key_provider_mask_key_is_deterministic(secret: bytes, key_version: str) -> None:
    a = SecretKeyProvider(secret, key_version=key_version)
    b = SecretKeyProvider(secret, key_version=key_version)
    assert a.mask_key() == b.mask_key() == a.mask_key()


@given(st.binary(min_size=1, max_size=64))
def test_seed_key_provider_mask_key_is_deterministic(job_seed: bytes) -> None:
    provider = SeedKeyProvider(job_seed)
    assert provider.mask_key() == provider.mask_key() == job_seed


@given(keyed_plan(), st.binary(min_size=32, max_size=64))
def test_require_mask_key_is_deterministic(plan: _MiniPlan, secret: bytes) -> None:
    """The execution choke-point gate itself is pure: repeated calls with the
    same (plan, provider) return the same bytes (matters because
    `require_mask_key` runs at every choke point, per its own docstring)."""
    provider = SecretKeyProvider(secret)
    first = require_mask_key(plan, provider)
    second = require_mask_key(plan, provider)
    assert first == second == provider.mask_key()


# --------------------------------------------------------------------------
# ISOLATION: distinct secrets / key_versions / provider types never collide.
# --------------------------------------------------------------------------


@given(st.binary(min_size=32, max_size=64), st.binary(min_size=32, max_size=64))
def test_distinct_secrets_derive_distinct_mask_keys(secret_a: bytes, secret_b: bytes) -> None:
    assume(secret_a != secret_b)
    key_a = SecretKeyProvider(secret_a).mask_key()
    key_b = SecretKeyProvider(secret_b).mask_key()
    assert key_a != key_b


@given(
    st.binary(min_size=32, max_size=64),
    st.text(min_size=1, max_size=8),
    st.text(min_size=1, max_size=8),
)
def test_distinct_key_versions_derive_distinct_mask_keys(
    secret: bytes, version_a: str, version_b: str
) -> None:
    """'Rotating key_version deterministically re-keys the whole surface'
    (module docstring) -- the same secret under two different versions must
    not collapse to the same root."""
    assume(version_a != version_b)
    key_a = SecretKeyProvider(secret, key_version=version_a).mask_key()
    key_b = SecretKeyProvider(secret, key_version=version_b).mask_key()
    assert key_a != key_b


@given(st.binary(min_size=32, max_size=64))
def test_seed_vs_secret_provider_paths_stay_distinct(raw: bytes) -> None:
    """ISOLATION between the two `KeyProvider` implementations: the SAME raw
    bytes behave differently depending on which provider wraps them.
    `SeedKeyProvider.mask_key()` is the identity (byte-identical to
    pre-DE-02); `SecretKeyProvider.mask_key()` always runs the bytes through
    HKDF-SHA256. The one-slot-IKM substitution (module docstring "Design"
    section) must not blur the two paths into each other."""
    assert SeedKeyProvider(raw).mask_key() == raw
    assert SecretKeyProvider(raw).mask_key() != raw
    assert SeedKeyProvider(raw).key_version == "seed"
    assert SecretKeyProvider(raw).key_version == "v1"


@given(st.binary(min_size=32, max_size=64), st.binary(min_size=8, max_size=8))
def test_mask_key_from_provider_prefers_provider_over_job_seed(
    secret: bytes, job_seed: bytes
) -> None:
    provider = SecretKeyProvider(secret)
    assert mask_key_from_provider(provider, job_seed) == provider.mask_key()
    assert mask_key_from_provider(None, job_seed) == job_seed


# --------------------------------------------------------------------------
# NO LEAKAGE: the raw secret never appears in a repr() or error message.
# --------------------------------------------------------------------------


# `min_size=32`: a valid SecretKeyProvider construction needs a full-strength
# secret (a shorter one raises WeakMaskSecret before there is a repr to
# check), and 32+ bytes (64+ hex chars) also makes an accidental substring
# collision with unrelated repr text astronomically unlikely.
@given(st.binary(min_size=32, max_size=128), st.text(min_size=1, max_size=12))
def test_secret_key_provider_repr_never_contains_secret_bytes(
    secret: bytes, key_version: str
) -> None:
    provider = SecretKeyProvider(secret, key_version=key_version)
    rendered = repr(provider)
    assert secret.hex() not in rendered
    assert base64.b64encode(secret).decode() not in rendered
    assert f"<redacted {len(secret)} bytes>" in rendered  # the length is safe to show


@given(st.binary(min_size=4, max_size=64))
def test_seed_key_provider_repr_never_contains_job_seed_bytes(job_seed: bytes) -> None:
    provider = SeedKeyProvider(job_seed)
    rendered = repr(provider)
    assert job_seed.hex() not in rendered
    assert f"<redacted {len(job_seed)} bytes>" in rendered


@given(st.binary(min_size=4, max_size=31))
def test_weak_secret_error_never_contains_the_offending_secret_bytes(secret: bytes) -> None:
    """The WeakMaskSecret path is the trickiest leak surface: the offending
    value IS the secret, so the error message must report only its LENGTH,
    never the bytes themselves (hex or base64)."""
    with pytest.raises(WeakMaskSecret) as ei:
        SecretKeyProvider(secret)
    message = str(ei.value)
    assert secret.hex() not in message
    assert base64.b64encode(secret).decode() not in message
    assert str(len(secret)) in message  # the length itself IS load-bearing


# The two FIXED (non-ref) fragments of `resolve_mask_secret_ref`'s
# unknown-kind message, split around where `_redact_ref(ref)` is
# interpolated (the digit run between them always breaks letter-contiguity,
# so ref -- letters-only -- can never straddle the split). A random
# letters-only ref of 5-40 chars occasionally draws an ordinary English
# word that is ALSO plain English prose already in the template (e.g.
# "start", from "...must start with...", or "chars", from the redaction
# marker itself) -- a coincidental containment match, not a real leak. This
# is not a hypothetical: `ref="start"` was an observed full-suite flake.
# Filtering candidates against these exact fixed fragments removes every
# such coincidence while leaving the leak-detection power intact: a
# regression that made `_redact_ref` embed the real `ref` bytes would still
# be caught, because the leaked text would NOT be a substring of either
# static fragment alone -- it would be `ref` sitting where the length
# digits belong.
_BAD_SECRET_REF_MESSAGE_FRAGMENTS = (
    "mask_secret_ref must start with 'env:' or 'file:'; got <redacted ref, ",
    " chars>.",
)


@given(st.text(alphabet=st.characters(whitelist_categories=("Ll", "Lu")), min_size=5, max_size=40))
def test_redacted_ref_error_never_contains_the_raw_ref_content(ref: str) -> None:
    """A `mask_secret_ref` of an unrecognized kind could itself be a raw
    secret pasted directly as the ref (a plausible operator mistake); the
    error must show only its length via `_redact_ref`, never the content.
    Letters-only means `ref` can never start with `env:`/`file:` (no colon
    in the alphabet), so every draw hits the redacted-ref branch."""
    assume(all(ref not in fragment for fragment in _BAD_SECRET_REF_MESSAGE_FRAGMENTS))
    with pytest.raises(MaskSecretError) as ei:
        resolve_mask_secret_ref(ref)
    assert ei.value.code == "bad_secret_ref"
    assert ref not in ei.value.message
    assert f"<redacted ref, {len(ref)} chars>" in ei.value.message


# --------------------------------------------------------------------------
# ERROR TAXONOMY: exact `code`/type per documented failure mode, not just
# "an error was raised". `MaskSecretError.__doc__`'s "Codes:" list is the
# source.
# --------------------------------------------------------------------------


class TestErrorTaxonomy:
    def test_mask_secret_error_code_and_str_with_message(self) -> None:
        exc = MaskSecretError(code="bad_secret_ref", message="details")
        assert exc.code == "bad_secret_ref"
        assert str(exc) == "bad_secret_ref: details"

    def test_mask_secret_error_code_and_str_without_message(self) -> None:
        exc = MaskSecretError(code="bad_secret_ref")
        assert exc.code == "bad_secret_ref"
        assert str(exc) == "bad_secret_ref"

    def test_keyed_strategy_requires_secret_is_the_ga_gate_code(self) -> None:
        exc = KeyedStrategyRequiresSecret("x")
        assert exc.code == "keyed_strategy_requires_secret"
        assert isinstance(exc, MaskSecretError)

    def test_missing_mask_secret_is_the_unresolvable_ref_code(self) -> None:
        exc = MissingMaskSecret("x")
        assert exc.code == "missing_mask_secret"
        assert isinstance(exc, MaskSecretError)
        assert not isinstance(exc, WeakMaskSecret)

    def test_weak_mask_secret_is_the_strength_gate_code(self) -> None:
        exc = WeakMaskSecret("x")
        assert exc.code == "weak_mask_secret"
        assert isinstance(exc, MaskSecretError)
        assert not isinstance(exc, MissingMaskSecret)

    def test_non_bytes_secret_is_bad_secret_ref_not_weak_mask_secret(self) -> None:
        """A type violation (not a strength violation) must raise the
        `bad_secret_ref` code, not get misclassified as `weak_mask_secret`
        just because `len()` would also fail on a non-sequence."""
        with pytest.raises(MaskSecretError) as ei:
            SecretKeyProvider("not-bytes")  # type: ignore[arg-type]
        assert ei.value.code == "bad_secret_ref"
        assert not isinstance(ei.value, WeakMaskSecret)

    def test_resolve_ref_unknown_kind_is_bad_secret_ref(self) -> None:
        with pytest.raises(MaskSecretError) as ei:
            resolve_mask_secret_ref("kms://tenant/key")
        assert ei.value.code == "bad_secret_ref"

    def test_resolve_ref_empty_env_name_is_bad_secret_ref(self) -> None:
        with pytest.raises(MaskSecretError) as ei:
            resolve_mask_secret_ref("env:")
        assert ei.value.code == "bad_secret_ref"

    def test_resolve_ref_empty_file_path_is_bad_secret_ref(self) -> None:
        with pytest.raises(MaskSecretError) as ei:
            resolve_mask_secret_ref("file:")
        assert ei.value.code == "bad_secret_ref"

    def test_resolve_ref_missing_env_is_missing_mask_secret(self) -> None:
        with pytest.raises(MissingMaskSecret) as ei:
            resolve_mask_secret_ref("env:DECOY_TQ_KEYPROVIDER_TEST_UNSET_XYZ")
        assert ei.value.code == "missing_mask_secret"

    def test_resolve_ref_missing_file_is_missing_mask_secret(self, tmp_path) -> None:
        with pytest.raises(MissingMaskSecret) as ei:
            resolve_mask_secret_ref(f"file:{tmp_path / 'does-not-exist.bin'}")
        assert ei.value.code == "missing_mask_secret"

    def test_resolve_ref_invalid_material_is_bad_secret_ref_not_missing(self, monkeypatch) -> None:
        """Present-but-undecodable is a DIFFERENT failure mode from absent:
        `bad_secret_ref`, not `missing_mask_secret`."""
        monkeypatch.setenv("DECOY_TQ_KEYPROVIDER_BAD", "not-hex-not-base64!!!")
        with pytest.raises(MaskSecretError) as ei:
            resolve_mask_secret_ref("env:DECOY_TQ_KEYPROVIDER_BAD")
        assert ei.value.code == "bad_secret_ref"
        assert not isinstance(ei.value, MissingMaskSecret)

    def test_key_provider_from_ref_weak_secret_is_weak_mask_secret(self, monkeypatch) -> None:
        monkeypatch.setenv("DECOY_TQ_KEYPROVIDER_WEAK", os.urandom(10).hex())
        with pytest.raises(WeakMaskSecret) as ei:
            key_provider_from_ref("env:DECOY_TQ_KEYPROVIDER_WEAK")
        assert ei.value.code == "weak_mask_secret"

    def test_ga_missing_secret_is_keyed_strategy_requires_secret_not_other_codes(
        self,
    ) -> None:
        plan = _MiniPlan(
            SeedEnvelope(
                job_seed=b"\x00" * 8,
                per_table=(("t", TableSeed(per_column=(("k", _col_seed("hash")),))),),
            )
        )
        with mock.patch("decoy_engine.keyprovider.is_pre_ga", return_value=False):
            with pytest.raises(KeyedStrategyRequiresSecret) as ei:
                require_mask_key(plan, None)
        assert ei.value.code == "keyed_strategy_requires_secret"
        assert not isinstance(ei.value, (MissingMaskSecret, WeakMaskSecret))
