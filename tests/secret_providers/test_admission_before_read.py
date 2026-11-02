"""F13 §5 acceptance 1-3, 5, 7, 9: secret admission wins before any source is touched."""

from __future__ import annotations

import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked, run_pipeline
from decoy_engine.keyprovider import (
    KeyedStrategyRequiresSecret,
    MaskSecretError,
    SecretKeyProvider,
    SeedKeyProvider,
)
from tests.secret_providers._helpers import (
    ENGINE_VERSION,
    FAILURE_CLASSES,
    GOOD_SECRET,
    ProtocolProvider,
    TouchedSource,
    keyed_config,
    one_chunk,
    unkeyed_config,
)

CHUNKED = [
    pytest.param(run_mask_pipeline_chunked, id="run_mask_pipeline_chunked"),
    pytest.param(run_mask_chunked, id="run_mask_chunked"),
]


def _drain(entry, cfg, source, **kw):
    return list(entry(cfg, source, table="t", engine_version=ENGINE_VERSION, **kw))


@pytest.fixture
def profile_spy(monkeypatch):
    """Counts (and still performs) profile_source calls: the first protected read."""
    import decoy_engine.profile as profile_mod

    real = profile_mod.profile_source
    calls = []

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(profile_mod, "profile_source", spy)
    return calls


def test_missing_env_secret_fails_before_profiling(tmp_path, monkeypatch, profile_spy, registry):
    monkeypatch.delenv("F13_ABSENT", raising=False)
    cfg = keyed_config(tmp_path, mask_secret_ref="env:F13_ABSENT")
    # The CSV never exists: opening the source would raise a different error.
    with pytest.raises(MaskSecretError) as info:
        run_pipeline(cfg, engine_version=ENGINE_VERSION)
    assert info.value.code == "missing_mask_secret"
    assert profile_spy == []


@pytest.mark.parametrize("entry", CHUNKED)
@pytest.mark.parametrize(
    "ref, code",
    [
        ("kms:alias/x", "secret_provider_unknown"),
        ("env:F13_ABSENT", "missing_mask_secret"),
    ],
)
def test_chunked_admission_failures_never_touch_the_iterator(
    tmp_path, monkeypatch, registry, entry, ref, code
):
    monkeypatch.delenv("F13_ABSENT", raising=False)
    cfg = keyed_config(tmp_path, mask_secret_ref=ref)
    source = TouchedSource()
    with pytest.raises(MaskSecretError) as info:
        _drain(entry, cfg, source)
    assert info.value.code == code
    assert source.touched == 0


@pytest.mark.parametrize("entry", CHUNKED)
@pytest.mark.parametrize("code", sorted(FAILURE_CLASSES))
def test_provider_failure_classes_never_touch_the_iterator(tmp_path, registry, entry, code):
    registry.register(ProtocolProvider("fail", fail=FAILURE_CLASSES[code]))
    cfg = keyed_config(tmp_path, mask_secret_ref="fail:anything")
    source = TouchedSource()
    with pytest.raises(MaskSecretError) as info:
        _drain(entry, cfg, source)
    assert info.value.code == code
    assert source.touched == 0


@pytest.mark.parametrize("entry", CHUNKED)
def test_unknown_provider_is_fail_closed_with_no_fallback(tmp_path, registry, entry, monkeypatch):
    def boom(self):
        raise AssertionError("seed fallback ran")

    monkeypatch.setattr(SeedKeyProvider, "mask_key", boom)
    source = TouchedSource()
    cfg = keyed_config(tmp_path, mask_secret_ref="nope:thing")
    with pytest.raises(MaskSecretError) as info:
        _drain(entry, cfg, source)
    assert info.value.code == "secret_provider_unknown"
    assert source.touched == 0


@pytest.mark.parametrize("pre_ga", [True, False], ids=["pre-ga", "ga"])
@pytest.mark.parametrize("entry", CHUNKED)
def test_keyed_work_without_a_secret_never_falls_back(tmp_path, monkeypatch, entry, pre_ga):
    monkeypatch.setattr("decoy_engine.release.is_pre_ga", lambda: pre_ga)

    def boom(self):
        raise AssertionError("seed fallback ran")

    monkeypatch.setattr(SeedKeyProvider, "mask_key", boom)
    source = TouchedSource()
    with pytest.raises(KeyedStrategyRequiresSecret) as info:
        _drain(entry, keyed_config(tmp_path), source)
    assert info.value.code == "keyed_strategy_requires_secret"
    assert source.touched == 0


@pytest.mark.parametrize("pre_ga", [True, False], ids=["pre-ga", "ga"])
def test_run_pipeline_keyed_without_secret_never_falls_back(
    tmp_path, monkeypatch, pre_ga, profile_spy
):
    monkeypatch.setattr("decoy_engine.release.is_pre_ga", lambda: pre_ga)
    with pytest.raises(KeyedStrategyRequiresSecret):
        run_pipeline(keyed_config(tmp_path), engine_version=ENGINE_VERSION)
    assert profile_spy == []


def test_seed_key_provider_is_not_a_secret(tmp_path, profile_spy):
    with pytest.raises(KeyedStrategyRequiresSecret):
        run_pipeline(
            keyed_config(tmp_path),
            engine_version=ENGINE_VERSION,
            key_provider=SeedKeyProvider((7).to_bytes(8, "big")),
        )
    assert profile_spy == []


def test_unkeyed_work_needs_no_secret(tmp_path):
    source = TouchedSource()
    out = _drain(run_mask_pipeline_chunked, unkeyed_config(tmp_path), source)
    assert out and source.touched > 0


@pytest.mark.parametrize("entry", CHUNKED)
def test_lease_unsupported_reads_nothing_and_is_not_cached(tmp_path, registry, entry):
    prov = ProtocolProvider("lease", renewable=True)
    registry.register(prov)
    source = TouchedSource()
    with pytest.raises(MaskSecretError) as info:
        _drain(entry, keyed_config(tmp_path, mask_secret_ref="lease:x"), source)
    assert info.value.code == "secret_lease_unsupported"
    assert prov.resolve_calls == 0 and source.touched == 0


def test_one_provider_read_per_run_even_if_data_would_drift(tmp_path, registry):
    prov = ProtocolProvider("once", drift=True, value=b"k" * 40)
    registry.register(prov)
    cfg = keyed_config(tmp_path, mask_secret_ref="once:x?encoding=raw")
    _drain(run_mask_pipeline_chunked, cfg, [one_chunk(), one_chunk()])
    assert prov.resolve_calls == 1


def test_programmatic_provider_still_wins_over_the_ref(tmp_path, registry):
    prov = SecretKeyProvider(GOOD_SECRET)
    cfg = keyed_config(tmp_path, mask_secret_ref="nope:ignored")
    out = _drain(run_mask_pipeline_chunked, cfg, [one_chunk()], key_provider=prov)
    assert out
