"""Independent-reconstruction grading for `resolve_faker_pool_identity`
(Phase 3 Task 3.1 HIGH 1, `generation/pool/_identity.py`).

The oracle handler (`execution/_strategies/_faker.py`), the native chunked
route (`execution/native/_dispatch.py`), and `_warm_faker_pools`
(`execution/_chunked.py`) all delegate to this ONE resolver. Cross-checking
their outputs against each other only proves the three callers agree; a
mutant INSIDE the resolver would change all three identically and survive
that convergence check (the P3-T1 test plan's own naming of the gap). This
module instead grades the resolver against a hand-written reconstruction of
its docstring contract: `pool_size`/`locale`/`build_config` are derived by an
independently-written rule (not a call into the resolver), then fed straight
to `PoolBuilder.identity_for` -- the shared primitive the resolver itself
calls, not the resolver under test -- and compared against the resolver's
actual return. Provider is varied independently of every other axis so a
resolver bug that hands `identity_for` the wrong provider string (or a fixed
one) cannot hide behind three callers that all happen to pass the same one.
"""

from __future__ import annotations

from decoy_engine.generation.pool._builder import PoolBuilder
from decoy_engine.generation.pool._identity import resolve_faker_pool_identity
from decoy_engine.generation.pool._runtime_pool_size import DEFAULT_POOL_SIZE
from decoy_engine.providers_v2 import get_default_registry


def _builder() -> PoolBuilder:
    return PoolBuilder(get_default_registry())


def _reconstruct(
    *,
    builder: PoolBuilder,
    provider: str,
    plan_pool_size: int | None,
    namespace: str | None,
    job_seed: bytes,
    cfg: dict,
) -> tuple[int, str | None, dict, tuple]:
    """The frozen contract (`_identity.py`'s docstring), reimplemented by
    hand: `plan_pool_size` wins; otherwise fall back to `cfg["pool_size"]`
    coerced to `int`, or `DEFAULT_POOL_SIZE` when that key is absent/None
    (the same rule `resolve_runtime_pool_size` applies, restated here rather
    than called, so a resolver mutant that calls it wrong is still caught).
    `locale` and `build_config` split `cfg` the same way: `locale` is
    `cfg.get("locale")`; `build_config` is every OTHER key (`pool_size` and
    `locale` excluded, since they are pool-BUILD knobs, not part of the
    provider-method kwargs the config hash covers). The identity itself comes
    from `PoolBuilder.identity_for`, the shared builder primitive, called
    directly here -- never through the resolver.
    """
    raw_pool_size = cfg.get("pool_size")
    expected_pool_size = (
        plan_pool_size
        if plan_pool_size is not None
        else (int(raw_pool_size) if raw_pool_size is not None else DEFAULT_POOL_SIZE)
    )
    expected_locale = cfg.get("locale")
    expected_build_config = {k: v for k, v in cfg.items() if k not in ("pool_size", "locale")}
    expected_identity = builder.identity_for(
        provider,
        size=expected_pool_size,
        job_seed=job_seed,
        locale=expected_locale,
        config=expected_build_config,
        namespace=namespace,
    )
    return expected_pool_size, expected_locale, expected_build_config, expected_identity


def _resolve(
    *,
    builder: PoolBuilder,
    provider: str,
    plan_pool_size: int | None,
    namespace: str | None,
    job_seed: bytes,
    cfg: dict,
) -> tuple[int, str | None, dict, tuple]:
    return resolve_faker_pool_identity(
        builder=builder,
        provider=provider,
        plan_pool_size=plan_pool_size,
        namespace=namespace,
        job_seed=job_seed,
        cfg=cfg,
    )


# ---------------------------------------------------------------------------
# Baseline: the resolver matches the independent reconstruction exactly.
# ---------------------------------------------------------------------------


def test_resolver_matches_independent_reconstruction_on_a_typical_config() -> None:
    builder = _builder()
    job_seed = (20260830).to_bytes(8, "big")
    cfg = {"birthdate": "1990-01-01"}
    kwargs = dict(
        builder=builder,
        provider="person_first_name",
        plan_pool_size=5_000,
        namespace="ns_first",
        job_seed=job_seed,
        cfg=cfg,
    )
    assert _resolve(**kwargs) == _reconstruct(**kwargs)


# ---------------------------------------------------------------------------
# Each determinant, varied independently: the resolver output must both
# MATCH its own reconstruction and DIFFER from the baseline when the
# determinant changes -- proving it is a real, forwarded determinant, not
# dropped or overwritten by a fixed value.
# ---------------------------------------------------------------------------


def test_provider_is_a_real_determinant_forwarded_correctly() -> None:
    # Two allowlisted C1 providers, everything else identical. A mutant that
    # hardcodes one provider, drops it, or swaps it for another field's value
    # would make these collide or diverge from the reconstruction.
    builder = _builder()
    job_seed = (7).to_bytes(8, "big")
    cfg: dict = {}
    common = dict(plan_pool_size=1_000, namespace="ns_shared", job_seed=job_seed, cfg=cfg)

    actual_first = _resolve(builder=builder, provider="person_first_name", **common)
    actual_last = _resolve(builder=builder, provider="person_last_name", **common)
    expected_first = _reconstruct(builder=builder, provider="person_first_name", **common)
    expected_last = _reconstruct(builder=builder, provider="person_last_name", **common)

    assert actual_first == expected_first
    assert actual_last == expected_last
    assert actual_first != actual_last
    # PoolIdentity's own first field IS the provider (_builder.py::identity_for);
    # pin that directly so a mutant that reorders the tuple is also caught.
    assert actual_first[3][0] == "person_first_name"
    assert actual_last[3][0] == "person_last_name"


def test_namespace_is_a_real_determinant() -> None:
    builder = _builder()
    job_seed = (11).to_bytes(8, "big")
    cfg: dict = {}
    common = dict(provider="person_first_name", plan_pool_size=1_000, job_seed=job_seed, cfg=cfg)

    actual_a = _resolve(builder=builder, namespace="ns_a", **common)
    actual_b = _resolve(builder=builder, namespace="ns_b", **common)

    assert actual_a == _reconstruct(builder=builder, namespace="ns_a", **common)
    assert actual_b == _reconstruct(builder=builder, namespace="ns_b", **common)
    assert actual_a != actual_b


def test_job_seed_is_a_real_determinant() -> None:
    builder = _builder()
    cfg: dict = {}
    common = dict(
        builder=builder, provider="person_first_name", plan_pool_size=1_000, namespace="ns", cfg=cfg
    )

    actual_a = _resolve(job_seed=(1).to_bytes(8, "big"), **common)
    actual_b = _resolve(job_seed=(2).to_bytes(8, "big"), **common)

    assert actual_a == _reconstruct(job_seed=(1).to_bytes(8, "big"), **common)
    assert actual_b == _reconstruct(job_seed=(2).to_bytes(8, "big"), **common)
    assert actual_a != actual_b


def test_locale_is_a_real_determinant_and_excluded_from_build_config() -> None:
    builder = _builder()
    job_seed = (3).to_bytes(8, "big")
    common = dict(
        builder=builder,
        provider="person_first_name",
        plan_pool_size=1_000,
        namespace="ns",
        job_seed=job_seed,
    )

    actual_en = _resolve(cfg={"locale": "en_US"}, **common)
    actual_fr = _resolve(cfg={"locale": "fr_FR"}, **common)

    assert actual_en == _reconstruct(cfg={"locale": "en_US"}, **common)
    assert actual_fr == _reconstruct(cfg={"locale": "fr_FR"}, **common)
    assert actual_en != actual_fr
    # locale surfaces as its own return field, not folded into build_config's
    # config-hash input.
    assert actual_en[1] == "en_US"
    _, _, build_config_en, _ = actual_en
    assert "locale" not in build_config_en


def test_pool_size_is_a_real_determinant_and_excluded_from_build_config() -> None:
    builder = _builder()
    job_seed = (4).to_bytes(8, "big")
    common = dict(
        builder=builder,
        provider="person_first_name",
        namespace="ns",
        job_seed=job_seed,
        cfg={},
    )

    actual_small = _resolve(plan_pool_size=100, **common)
    actual_large = _resolve(plan_pool_size=100_000, **common)

    assert actual_small == _reconstruct(plan_pool_size=100, **common)
    assert actual_large == _reconstruct(plan_pool_size=100_000, **common)
    assert actual_small != actual_large
    assert actual_small[0] == 100
    assert actual_large[0] == 100_000
    _, _, build_config, _ = actual_small
    assert "pool_size" not in build_config


def test_build_config_hash_is_a_real_determinant() -> None:
    # Two cfgs differing only in a non-pool_size/locale key: the config hash
    # `identity_for` derives must differ, so the identity must differ too.
    builder = _builder()
    job_seed = (5).to_bytes(8, "big")
    common = dict(
        builder=builder,
        provider="person_first_name",
        plan_pool_size=1_000,
        namespace="ns",
        job_seed=job_seed,
    )

    actual_a = _resolve(cfg={"birthdate": "1990-01-01"}, **common)
    actual_b = _resolve(cfg={"birthdate": "2000-06-15"}, **common)

    assert actual_a == _reconstruct(cfg={"birthdate": "1990-01-01"}, **common)
    assert actual_b == _reconstruct(cfg={"birthdate": "2000-06-15"}, **common)
    assert actual_a != actual_b


def test_raw_config_pool_size_fallback_used_only_without_a_plan_pool_size() -> None:
    # Only reachable for a hand-built ColumnSeed that bypassed compile_plan
    # (native admission always requires a compiled, non-None pool_size); the
    # resolver's raw-config fallback path is still part of its contract.
    builder = _builder()
    job_seed = (6).to_bytes(8, "big")
    common = dict(builder=builder, provider="person_first_name", namespace="ns", job_seed=job_seed)

    actual = _resolve(plan_pool_size=None, cfg={"pool_size": 250}, **common)
    assert actual == _reconstruct(plan_pool_size=None, cfg={"pool_size": 250}, **common)
    assert actual[0] == 250


def test_absent_pool_size_everywhere_falls_back_to_the_shared_default() -> None:
    builder = _builder()
    job_seed = (8).to_bytes(8, "big")
    actual = _resolve(
        builder=builder,
        provider="person_first_name",
        plan_pool_size=None,
        namespace="ns",
        job_seed=job_seed,
        cfg={},
    )
    assert actual[0] == DEFAULT_POOL_SIZE
