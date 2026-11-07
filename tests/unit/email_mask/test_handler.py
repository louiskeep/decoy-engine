"""email_mask handler: the kept property, keying, collisions, errors, secrets, verification."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st

from decoy_engine import KeyedStrategyRequiresSecret, run_pipeline
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution._strategies._email_mask import _address as address
from decoy_engine.execution._strategies._email_mask._freemail import FREEMAIL_DOMAINS
from decoy_engine.secret_providers._requirements import (
    ALWAYS_KEYED_STRATEGIES,
    KNOWN_UNKEYED_STRATEGIES,
    column_is_keyed,
)
from decoy_engine.verification.postmask.residual_pii import check_residual_pii
from tests.unit.email_mask import _support as s

# --- AT-1: the property, generated ---

_LDH = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12)
_DOMAINS = st.one_of(
    st.sampled_from(sorted(FREEMAIL_DOMAINS)),
    st.sampled_from(["GMail.com", "Yahoo.CO.UK", "OUTLOOK.com"]),
    st.builds(lambda a: f"{a}.edu", _LDH),
    st.builds(lambda a, c: f"{a}.ac.{c}", _LDH, st.sampled_from(["uk", "jp", "nz"])),
    st.builds(lambda a: f"{a}.gov", _LDH),
    st.builds(lambda a, c: f"{a}.gov.{c}", _LDH, st.sampled_from(["uk", "au", "in"])),
    st.builds(lambda a: f"{a}.mil", _LDH),
    st.builds(lambda a, b: f"{a}.{b}.com", _LDH, _LDH),
    st.builds(lambda a: f"{a}ü.de", _LDH),
)
_LOCALS = st.one_of(
    st.text(min_size=1, max_size=30).filter(lambda t: t.strip() == t and t != ""),
    st.builds(lambda t: f'"{t}"', st.text(max_size=10)),
    st.builds(lambda a, b: f"{a}@{b}", _LDH, _LDH),
)
_ADDRESSES = st.builds(lambda local, domain: f"{local}@{domain}", _LOCALS, _DOMAINS)
_VALUES = st.one_of(_ADDRESSES, st.text(max_size=20), st.none(), st.integers(), st.floats())


def _expected_domain(value: Any, keep: str) -> str:
    domain = address.split_domain(value)
    if domain is None:
        return "unknown.invalid"
    if keep == "domain":
        return domain
    klass = address.domain_class(domain)
    if klass == "freemail":
        return domain.lower()
    return {"gov": "gov.example", "edu": "edu.example", "corporate": "corporate.example"}[klass]


def _is_null(value: Any) -> bool:
    return value is None or (isinstance(value, float) and value != value)


@seed(20261107)
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    values=st.lists(_VALUES, min_size=1, max_size=12),
    keep=st.sampled_from(["domain", "domain_class"]),
)
def test_every_row_keeps_exactly_the_stated_property(values: list[Any], keep: str) -> None:
    out, _ = s.run_handler(values, keep)
    for value, masked in zip(values, out, strict=True):
        if _is_null(value):
            assert masked is None
            continue
        local, _, domain = masked.partition("@")
        assert re.fullmatch(s.LOCAL, local), masked
        assert local == s.oracle_local(value)
        assert domain == _expected_domain(value, keep)
        source_local = str(value).rpartition("@")[0]
        if len(source_local) >= 8:
            assert source_local not in local


def test_the_mixed_rows_match_the_behaviour_table() -> None:
    out, _ = s.run_handler(s.MIXED, "domain_class")
    domains = [None if o is None else o.partition("@")[2] for o in out]
    assert domains == [
        "corporate.example",
        "gmail.com",
        "edu.example",
        "edu.example",
        "gov.example",
        "gov.example",
        "gov.example",
        "corporate.example",
        "corporate.example",
        "corporate.example",
        None,
        "unknown.invalid",
        "unknown.invalid",
        "unknown.invalid",
        "unknown.invalid",
        "corporate.example",
    ]
    kept, _ = s.run_handler(s.MIXED, "domain")
    assert kept[1].endswith("@Gmail.com") and kept[9].endswith("@bücher.de")


def test_the_pipeline_output_equals_the_oracle(tmp_path: Path) -> None:
    values = [v for v in s.MIXED if isinstance(v, str) or v is None]
    out = s.run_single(values, tmp_path, "domain")
    for value, masked in zip(values, out, strict=True):
        if value is None:
            assert masked is None
        else:
            assert masked == f"{s.oracle_local(value)}@{_expected_domain(value, 'domain')}"


# --- AT-2: determinism and keying ---


def test_two_runs_are_identical(tmp_path: Path) -> None:
    values = [f"user{i}@acme.com" for i in range(30)]
    assert s.run_single(values, tmp_path) == s.run_single(values, tmp_path)


def test_another_namespace_or_key_changes_every_local_part() -> None:
    values = [f"user{i}@acme.com" for i in range(50)]
    base, _ = s.run_handler(values)
    other_ns, _ = s.run_handler(values, namespace="ns_other")
    other_key, _ = s.run_handler(values, mask_key=s.OTHER_KEY)
    for a, b, c in zip(base, other_ns, other_key, strict=True):
        assert a.split("@")[0] != b.split("@")[0]
        assert a.split("@")[0] != c.split("@")[0]


# --- AT-3: collisions and uniqueness ---


def test_same_local_part_at_two_corporate_domains_stays_distinct() -> None:
    out, _ = s.run_handler(["a@acme.com", "a@globex.com"], "domain_class")
    assert out[0] != out[1]
    assert out[0].endswith("@corporate.example") and out[1].endswith("@corporate.example")


def test_ten_thousand_distinct_addresses_give_ten_thousand_outputs() -> None:
    values = [f"user{i}@acme.com" for i in range(10_000)]
    out, _ = s.run_handler(values)
    assert len(set(out)) == 10_000


@pytest.mark.parametrize("keep", ["domain", "domain_class"])
def test_nfd_and_nfc_spellings_give_identical_output(keep: str) -> None:
    nfc = "\u00fc@b\u00fccher.de"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfd != nfc
    out, warnings = s.run_handler([nfd, nfc], keep)
    assert out[0] == out[1]
    assert out[0].split("@")[0] == s.oracle_local(nfc)
    if keep == "domain":
        assert out[0].endswith("@b\u00fccher.de")
    assert warnings == []


def test_a_kelvin_sign_is_ascii_k_after_nfc() -> None:
    # NFC maps U+212A to ASCII "K" before parsing, so the domain is classed and kept as ASCII.
    kelvin = chr(0x212A)
    values = [f"x@{kelvin}orea.gov", f"x@yahoo.co.u{kelvin}"]
    by_class, _ = s.run_handler(values, "domain_class")
    assert [v.split("@")[1] for v in by_class] == ["gov.example", "yahoo.co.uk"]
    kept, _ = s.run_handler(values, "domain")
    assert [v.split("@")[1] for v in kept] == ["Korea.gov", "yahoo.co.uK"]
    assert [v.split("@")[0] for v in kept] == [s.oracle_local(v) for v in values]


def test_the_token_is_the_prefix_of_the_hash_token_under_the_same_namespace() -> None:
    # Documented linkage: a hash column sharing the namespace joins to this one.
    values = ["alice@acme.com", "Bob@Gmail.com", "\u00fc@b\u00fccher.de"]
    out, _ = s.run_handler(values)
    frame = pd.DataFrame({"e": pd.Series(values, dtype=object)})
    hashed, _ = SCALAR_HANDLERS["hash"].run(
        frame, "e", replace(s.seed({}), strategy="hash"), s.ctx()
    )
    for masked, token in zip(out, hashed["e"].tolist(), strict=True):
        assert token.startswith(masked.split("@")[0])


def test_a_repeated_input_maps_to_one_output() -> None:
    out, _ = s.run_handler(["a@acme.com", "b@acme.com", "a@acme.com"])
    assert out[0] == out[2] != out[1]


# --- AT-5: value-free errors and warnings ---

SENTINELS = [987654321, 1234.5678, datetime(2001, 2, 3, 4, 5, 6), True]


@pytest.mark.parametrize("value", SENTINELS, ids=lambda v: type(v).__name__)
def test_a_non_string_column_is_refused_without_its_values(value: Any) -> None:
    frame = pd.DataFrame({"e": [value, value]})
    assert frame["e"].dtype != object
    with pytest.raises(StrategyError) as caught:
        s.handler().run(frame, "e", s.seed({"keep": "domain"}), s.ctx())
    err = caught.value
    assert err.code == "email_mask_requires_string_dtype"
    assert str(value) not in str(err) and "987654321" not in str(err)
    assert err.__cause__ is None and err.__context__ is None


def test_the_malformed_warning_carries_only_a_count() -> None:
    secret = "secret-local-part"
    _, warnings = s.run_handler([f"{secret}@acme..com", "bad", "ok@acme.com", None])
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.code == "email_mask_malformed_values"
    assert warning.detail == {"malformed_count": 2}
    assert secret not in repr(warning) and "bad" not in repr(warning.detail)


def test_no_warning_when_nothing_is_malformed() -> None:
    _, warnings = s.run_handler(["a@acme.com", None])
    assert warnings == []


@pytest.mark.parametrize(
    "provider_config",
    [{}, {"keep": "Domain"}, {"keep": True}, {"keep": "domain", "keep_domain": True}],
    ids=["missing", "case", "bool", "unknown_key"],
)
def test_a_bad_config_driven_past_compile_is_refused(provider_config: dict[str, Any]) -> None:
    frame = pd.DataFrame({"e": ["a@acme.com"]})
    with pytest.raises(StrategyError) as caught:
        s.handler().run(frame, "e", s.seed(provider_config), s.ctx())
    assert caught.value.code == "email_mask_config_invalid"
    assert "a@acme.com" not in str(caught.value)


def test_a_missing_namespace_driven_past_compile_is_refused() -> None:
    frame = pd.DataFrame({"e": ["a@acme.com"]})
    with pytest.raises(StrategyError) as caught:
        s.handler().run(frame, "e", s.seed({"keep": "domain"}, namespace=None), s.ctx())
    assert caught.value.code == "email_mask_config_invalid"


# --- AT-12: all-null columns ---


@pytest.mark.parametrize("dtype", ["float64", "object"])
def test_an_all_null_column_passes_and_stays_null(dtype: str) -> None:
    out, warnings = s.run_handler(pd.Series([None, None, None], dtype=dtype))
    assert all(_is_null(v) for v in out)
    assert warnings == []


# --- AT-13: canonical free-mail spelling ---


def test_class_mode_emits_the_lists_spelling_of_a_free_mail_domain() -> None:
    out, _ = s.run_handler(["Bob@GMail.COM"], "domain_class")
    assert out[0].endswith("@gmail.com")


# --- AT-11: a mask secret is required ---


def test_the_strategy_is_classified_keyed() -> None:
    assert column_is_keyed("email_mask", deterministic=False, vault=False, provider_config={})
    assert "email_mask" in ALWAYS_KEYED_STRATEGIES
    assert "email_mask" not in KNOWN_UNKEYED_STRATEGIES


def test_the_handler_refuses_a_key_shorter_than_the_mask_root() -> None:
    frame = pd.DataFrame({"e": ["a@acme.com"]})
    with pytest.raises(KeyedStrategyRequiresSecret) as caught:
        s.handler().run(frame, "e", s.seed({"keep": "domain"}), s.ctx((7).to_bytes(8, "big")))
    assert caught.value.code == "keyed_strategy_requires_secret"


@pytest.fixture(params=["pre_ga", "ga"])
def phase(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("decoy_engine.release.is_pre_ga", lambda: request.param == "pre_ga")
    return str(request.param)


class _RecordingSources(dict):  # type: ignore[type-arg]
    """A sources mapping that records any read of a table."""

    reads: list[str]

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.reads = []

    def __getitem__(self, key: str) -> Any:
        self.reads.append(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        self.reads.append(key)
        return super().get(key, default)


@pytest.mark.no_default_secret
def test_a_run_without_a_secret_is_refused_before_any_read(
    tmp_path: Path, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, source = s.single_table(["a@acme.com"], tmp_path)
    sources = _RecordingSources({"t": source})
    calls: list[str] = []
    # The handler has its own short-key refusal; admission must refuse before it is reached.
    monkeypatch.setattr(type(s.handler()), "run", lambda *a, **k: calls.append("run"))
    with pytest.raises(KeyedStrategyRequiresSecret) as caught:
        run_pipeline(cfg, sources=sources, engine_version=s.ENGINE_VERSION, auto_chunk=False)
    assert caught.value.code == "keyed_strategy_requires_secret"
    assert sources.reads == []
    assert calls == []


# --- AT-10: residual-PII verification ---


def test_residual_pii_reads_masked_emails_as_expected() -> None:
    values = [f"user{i}@acme.com" for i in range(40)]
    out, _ = s.run_handler(values)
    cfg = {"tables": [{"name": "t", "columns": [s.column()]}]}
    findings = check_residual_pii(
        {"t": pd.DataFrame({"email": out})},
        cfg,
        source_frames={"t": pd.DataFrame({"email": values})},
    )
    email = [f for f in findings if f.column == "email"]
    assert email and all(f.severity == "info" for f in email)


def test_residual_pii_fails_an_email_mask_column_that_mostly_did_not_change() -> None:
    # Substitution semantics: a majority of rows left unmasked is a failed mask, not only all.
    values = [f"user{i}@acme.com" for i in range(40)]
    masked, _ = s.run_handler(values)
    half = values[:30] + masked[30:]
    cfg = {"tables": [{"name": "t", "columns": [s.column()]}]}
    findings = check_residual_pii(
        {"t": pd.DataFrame({"email": half})},
        cfg,
        source_frames={"t": pd.DataFrame({"email": values})},
    )
    assert [f.severity for f in findings if f.column == "email"] == ["fail"]


def test_residual_pii_fails_an_email_mask_column_that_did_not_change() -> None:
    values = [f"user{i}@acme.com" for i in range(40)]
    cfg = {"tables": [{"name": "t", "columns": [s.column()]}]}
    findings = check_residual_pii(
        {"t": pd.DataFrame({"email": values})},
        cfg,
        source_frames={"t": pd.DataFrame({"email": values})},
    )
    assert [f.severity for f in findings if f.column == "email"] == ["fail"]


# --- post-validation leakage scan: email_mask is a substitution strategy ---


def test_post_validation_leakage_passes_when_free_mail_domains_are_kept(tmp_path: Path) -> None:
    # Set membership compares whole values; a kept domain is a substring, never a whole source
    # value, so only a real leak (an output equal to some source address) can fail the scan.
    values = [f"user{i}@gmail.com" for i in range(20)] + [f"u{i}@acme.com" for i in range(20)]
    for keep in ("domain", "domain_class"):
        out = s.run_single(
            values, tmp_path, keep, post_validation=True, post_validation_enforce=True
        )
        assert not set(out) & set(values)
