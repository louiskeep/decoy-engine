"""Property-based invariants for the core mask strategies.

Audit artifact (2026-06-11). Hypothesis-generated inputs exercise the
non-negotiable masking guarantees that example-based tests can miss:
null preservation, determinism + namespace isolation, no-source-leakage,
and per-strategy structural invariants.

The properties are deliberately *strategy-aware* (see PLAN): passthrough and
shuffle legitimately reproduce source values, so a blanket "no masked cell
equals its source" assertion would be wrong for them. Each strategy asserts
only what it actually promises.

Drives strategy handlers directly through the `.run(df, column, ColumnSeed,
ctx)` contract used by tests/unit/execution/test_fpe_strategy.py et al. Scoped
to the config-light scalar strategies (no pool/provider wiring required):
passthrough, redact, hash, shuffle, truncate, fpe, date_shift.

Run:  pytest tests/property -q
"""

from __future__ import annotations

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.execution._strategies._hash import HashStrategyHandler
from decoy_engine.execution._strategies._passthrough import PassthroughHandler
from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.execution._strategies._shuffle import ShuffleStrategyHandler
from decoy_engine.execution._strategies._truncate import TruncateHandler
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

# One audit profile: more examples than the default, no deadline (pandas ops
# are slow enough to trip the default 200ms deadline and produce flaky noise),
# and print_blob so a counterexample is replayable by a downstream verifier.
settings.register_profile(
    "audit",
    max_examples=400,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("audit")

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0xC0FFEE).to_bytes(8, "big")


def _ctx() -> StrategyContext:
    return StrategyContext(
        registry=_REG,
        pool_cache=PoolCache(),
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
        job_seed=_SEED,
    )


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    deterministic: bool = False,
    provider: str | None = None,
    provider_config: dict | None = None,
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=provider,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=tuple(sorted((provider_config or {}).items())),
        coherent_with=(),
    )


def _run(handler, df: pd.DataFrame, column: str, seed: ColumnSeed) -> pd.DataFrame:
    out, _warnings = handler.run(df.copy(deep=True), column, seed, _ctx())
    return out


# ── Input strategies ────────────────────────────────────────────────────────
# Non-null cell values. Kept ASCII-ish but include awkward cases: empty string,
# whitespace, leading zeros, unicode.
_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FF, blacklist_categories=("Cs",)),
    min_size=0,
    max_size=20,
)
# A column = list of cells, each possibly None (null).
_CELL = st.one_of(st.none(), _TEXT)
_COLUMN = st.lists(_CELL, min_size=1, max_size=40)

_DIGITS = st.text(alphabet="0123456789", min_size=1, max_size=12)
_DIGIT_COLUMN = st.lists(st.one_of(st.none(), _DIGITS), min_size=1, max_size=40)

_DATES = st.dates(min_value=pd.Timestamp("1950-01-01").date(),
                  max_value=pd.Timestamp("2050-12-31").date()).map(lambda d: d.isoformat())
_DATE_COLUMN = st.lists(st.one_of(st.none(), _DATES), min_size=1, max_size=40)


def _df(values: list) -> pd.DataFrame:
    return pd.DataFrame({"c": pd.Series(values, dtype=object)})


def _null_mask(series: pd.Series) -> list[bool]:
    return series.isna().tolist()


# ── passthrough ─────────────────────────────────────────────────────────────
@given(_COLUMN)
def test_passthrough_is_identity(values):
    out = _run(PassthroughHandler(), _df(values), "c", _col("passthrough"))
    src = _df(values)["c"]
    # Identity: nulls and values both preserved, position-for-position.
    assert _null_mask(out["c"]) == _null_mask(src)
    for a, b in zip(out["c"].tolist(), src.tolist(), strict=True):
        if pd.isna(a) and pd.isna(b):
            continue
        assert a == b


# ── redact ──────────────────────────────────────────────────────────────────
@given(_COLUMN)
def test_redact_preserves_nulls_and_replaces_values(values):
    out = _run(RedactHandler(), _df(values), "c", _col("redact"))
    src = _df(values)["c"]
    # Null preservation.
    assert _null_mask(out["c"]) == _null_mask(src)
    # Every non-null becomes the constant; the only "source value" that may
    # survive is one that already equalled the redaction constant.
    for o, s in zip(out["c"].tolist(), src.tolist(), strict=True):
        if pd.isna(s):
            continue
        assert o == "REDACTED"


# ── hash ────────────────────────────────────────────────────────────────────
@given(_COLUMN)
def test_hash_null_preservation_and_determinism_and_joinability(values):
    seed = _col("hash", namespace="ns_a", deterministic=True)
    out1 = _run(HashStrategyHandler(), _df(values), "c", seed)
    out2 = _run(HashStrategyHandler(), _df(values), "c", seed)
    src = _df(values)["c"]
    # Null preservation.
    assert _null_mask(out1["c"]) == _null_mask(src)
    # Determinism: same seed+namespace+input -> identical tokens.
    for a, b in zip(out1["c"].tolist(), out2["c"].tolist(), strict=True):
        if pd.isna(a) and pd.isna(b):
            continue
        assert a == b
    # Joinability: equal source values -> equal tokens within a namespace.
    by_src: dict = {}
    for s, t in zip(src.tolist(), out1["c"].tolist(), strict=True):
        if pd.isna(s):
            continue
        by_src.setdefault(s, set()).add(t)
    for toks in by_src.values():
        assert len(toks) == 1, "hash broke joinability: same source value -> different tokens"


@given(_COLUMN)
def test_hash_namespace_isolation(values):
    # Two namespaces must not produce identical tokens for the same source
    # value (determinism domain separation). Assert on at least one non-null.
    non_null = [v for v in values if v is not None]
    if not non_null:
        return
    out_a = _run(HashStrategyHandler(), _df(values), "c",
                 _col("hash", namespace="ns_a", deterministic=True))
    out_b = _run(HashStrategyHandler(), _df(values), "c",
                 _col("hash", namespace="ns_b", deterministic=True))
    src = _df(values)["c"].tolist()
    # For every non-null source value, the token must differ across namespaces.
    collisions = 0
    total = 0
    for s, a, b in zip(src, out_a["c"].tolist(), out_b["c"].tolist(), strict=True):
        if s is None:
            continue
        total += 1
        if a == b:
            collisions += 1
    # A genuine 256-bit hash collision across namespaces is astronomically
    # unlikely; any collision indicates namespace material is not mixed in.
    assert collisions == 0, f"namespace isolation broken: {collisions}/{total} tokens identical across namespaces"


# ── shuffle ─────────────────────────────────────────────────────────────────
@given(_COLUMN)
def test_shuffle_is_multiset_permutation_with_nulls_in_place(values):
    from collections import Counter

    seed = _col("shuffle", namespace="sh", deterministic=True)
    out = _run(ShuffleStrategyHandler(), _df(values), "c", seed)
    src = _df(values)["c"]
    # Null positions are preserved in place (shuffle permutes non-nulls only).
    assert _null_mask(out["c"]) == _null_mask(src)
    # The non-null multiset is exactly preserved (it is a permutation).
    src_vals = Counter(v for v in src.tolist() if not pd.isna(v))
    out_vals = Counter(v for v in out["c"].tolist() if not pd.isna(v))
    assert src_vals == out_vals


# ── truncate ────────────────────────────────────────────────────────────────
@given(_COLUMN, st.integers(min_value=1, max_value=8))
def test_truncate_output_is_substring_within_length(values, length):
    seed = _col("truncate", provider_config={"length": length, "keep": "head"})
    out = _run(TruncateHandler(), _df(values), "c", seed)
    src = _df(values)["c"]
    assert _null_mask(out["c"]) == _null_mask(src)
    for o, s in zip(out["c"].tolist(), src.tolist(), strict=True):
        if pd.isna(s):
            continue
        s_str = str(s)
        # head-keep, no mask_char: output is the leading <=length chars.
        assert len(o) <= length
        assert s_str.startswith(o)
        assert o == s_str[:length]


# ── fpe (digits) ────────────────────────────────────────────────────────────
@given(_DIGIT_COLUMN)
def test_fpe_format_preserving_null_and_deterministic(values):
    seed = _col("fpe", namespace="fpe_ns", deterministic=True,
                provider="fpe", provider_config={"charset": "digits"})
    out1 = _run(FpeStrategyHandler(chunk_count=1), _df(values), "c", seed)
    out2 = _run(FpeStrategyHandler(chunk_count=4), _df(values), "c", seed)
    src = _df(values)["c"]
    assert _null_mask(out1["c"]) == _null_mask(src)
    for o, s in zip(out1["c"].tolist(), src.tolist(), strict=True):
        if pd.isna(s):
            continue
        # Format preservation: same length, still all digits.
        assert len(o) == len(str(s))
        assert o.isdigit()
    # Chunk-count invariance (the engine's own non-negotiable gate): chunk=1
    # and chunk=4 must be byte-identical.
    for a, b in zip(out1["c"].tolist(), out2["c"].tolist(), strict=True):
        if pd.isna(a) and pd.isna(b):
            continue
        assert a == b, "FPE chunk_count changed the output (parallelism non-determinism)"


# ── text_redact ─────────────────────────────────────────────────────────────
@given(_COLUMN)
def test_text_redact_preserves_nulls_and_never_emits_null_literals(values):
    from decoy_engine.execution._strategies._text_redact import TextRedactHandler

    out = _run(TextRedactHandler(), _df(values), "c", _col("text_redact"))
    src = _df(values)["c"]
    # Null preservation: every source null stays null (audit H1: pd.NA /
    # pd.NaT previously leaked as the literal strings '<NA>' / 'NaT').
    assert _null_mask(out["c"]) == _null_mask(src)
    for o, s in zip(out["c"].tolist(), src.tolist(), strict=True):
        if pd.isna(s):
            continue
        assert isinstance(o, str)
        assert o not in ("<NA>", "NaT") or s in ("<NA>", "NaT")


# ── date_shift ──────────────────────────────────────────────────────────────
@given(_DATE_COLUMN)
def test_date_shift_preserves_nulls_and_emits_valid_dates(values):
    seed = _col("date_shift", namespace="ds", deterministic=True,
                provider_config={"min_days": -365, "max_days": 365})
    out = _run(DateShiftStrategyHandler(), _df(values), "c", seed)
    src = _df(values)["c"]
    # Null/unparseable preserved as null; valid dates stay parseable dates.
    src_null = _null_mask(src)
    out_null = _null_mask(out["c"])
    for i, was_null in enumerate(src_null):
        if was_null:
            assert out_null[i], "date_shift invented a value for a null source"
    for o, s in zip(out["c"].tolist(), src.tolist(), strict=True):
        if pd.isna(s) or pd.isna(o):
            continue
        # Output must be a real, parseable date.
        assert pd.notna(pd.to_datetime(o, errors="coerce")), f"date_shift emitted non-date {o!r}"


# ── fpe round-trip (WS1 detokenization) ─────────────────────────────────────
_FPE_CHARSETS = st.sampled_from(["digits", "alpha", "ALPHA", "alphanum", "ALPHANUM"])


@given(
    charset_name=_FPE_CHARSETS,
    key=st.binary(min_size=32, max_size=32),
    tweak=st.binary(min_size=1, max_size=32),
    body_len=st.integers(min_value=1, max_value=24),
    data=st.data(),
)
def test_fpe_decrypt_inverts_encrypt(charset_name, key, tweak, body_len, data):
    from decoy_engine.transforms.fpe import (
        _CHARSETS,
        fpe_decrypt_value,
        fpe_encrypt_value,
    )

    charset = _CHARSETS[charset_name]
    value = "".join(
        data.draw(st.sampled_from(charset)) for _ in range(body_len)
    )
    enc = fpe_encrypt_value(value, key, charset, tweak)
    assert len(enc) == len(value)
    assert all(ch in charset for ch in enc)
    assert fpe_decrypt_value(enc, key, charset, tweak) == value


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
