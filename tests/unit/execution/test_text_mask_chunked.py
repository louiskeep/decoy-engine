"""Phase 4 slice 3: text_mask on the chunked route
(`docs/plans/2026-09-01-p4-slice3-text-mask-chunked.md`).

`text_mask` masks each detected PII span in a cell via a per-detector,
HMAC-keyed sub-strategy (`transforms/text_mask.mask_cell`): the REAL span
branches are `fpe`, `faker`, `date_shift`, `passthrough`, `redact`, and an
unknown-detector-strategy `redact` fallback. Each span's key is
`HMAC(mask_key, matched_text)`, with no whole-column or row-position state,
so it is own-value-keyed exactly like `text_redact` (already `CHUNK_SAFE`)
and joins `CHUNK_SAFE_STRATEGIES` directly -- no separate admitted set, and
correctly eligible as an FK-self-mask key (contrast `group_key`, which is
sibling-keyed and excluded). This module proves the traps the plan-gate
named:

1. Byte-identity to the pinned pandas oracle on the real
   `run_pipeline(auto_chunk=True)` route, per REAL span branch, with branch
   SEMANTICS asserted (not just full-vs-chunked equality).
2. Trap C: per-span date format survives a chunk boundary that splits
   same-format and mixed-format cells.
3. Trap A: FK self-mask RI holds for every real branch, with exact
   parent/child byte equality across chunk boundaries.
4. Trap A negative + namespace independence: a differing child config is
   rejected; an absent/differing column namespace does not change output.
5. Trap B: output dtype (object/string) is chunk-invariant across null
   shapes and unmatched_span_policy values.
6. Trap D: NER is off by default (chunk-invariant); the version-mismatch
   guard raises identically on both routes and only when a real version
   drift is detected.
7. Trap E: `text_mask` + `when:` is rejected, fail-closed, at both entry
   points.
8. Admission surfaces: manual entry, auto route, cross-substrate polars.
9. The handler's warnings contract (always `[]`) stays separate from the
   unmatched-passthrough log, which is identical between routes.

Built-in span detectors do not emit the Tier-2 (date/faker/NER-routed)
detector ids without a real spaCy model. `_install_fake_ner` bypasses the
spaCy dependency (absent in this environment) and stubs
`storm.ner.iter_ner_spans` to return hand-picked `Span` objects for a
literal marker substring, reaching the `date_shift`/`faker`/unknown-fallback
branches through the SAME `extra_spans` merge path real NER would use --
deterministically, with no model install. Any real-spaCy path stays gated
the same way `test_text_mask_ner.py` already gates it (unused here).
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_pipeline_chunked, run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import PolarsExecutionAdapter
from decoy_engine.execution._chunked import check_chunked_compatibility, concat_masked_chunks
from decoy_engine.execution._chunked_fk import CHUNK_SAFE_STRATEGIES, NAMESPACE_REQUIRING_STRATEGIES
from decoy_engine.execution._chunked_text_mask import (
    reject_text_mask_when,
    unsafe_text_mask_source_columns,
)
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._text_mask import TextMaskHandler
from decoy_engine.plan import PlanCompileError
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.storm.detectors import Span

_ENGINE_VERSION = "p4-slice3-text-mask-test"
_LOW_THRESHOLD = 10
_SEED = 100200300

_SSN = "123-45-6789"
_EMAIL = "a@example.com"
_URL = "https://example.com/x"
_PERSON_MARKER = "PERSON_MARK_ZQ"
_DATE_TEXT = "1990-06-15"
_UNKNOWN_MARKER = "UNKNOWN_MARK_ZQ"
_TOKEN = "[REDACTED]"


class _FakeCtx:
    mask_key = b"\xab" * 32


# ---------------------------------------------------------------------------
# Shared config / chunking helpers (mirror test_group_key_chunked.py).
# ---------------------------------------------------------------------------


def _validated_dump(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


def _config(
    tmp_path,
    columns: list[dict],
    *,
    table: str = "records",
    relationships: list[dict] | None = None,
    seed: int = _SEED,
    extra_tables: list[dict] | None = None,
) -> dict:
    tables = [{"name": table, "columns": columns}]
    sources = {table: {"type": "file", "format": "csv", "path": str(tmp_path / f"{table}.csv")}}
    targets = {table: {"type": "file", "format": "csv", "path": str(tmp_path / f"{table}_out.csv")}}
    for extra in extra_tables or []:
        tables.append(extra)
        name = extra["name"]
        sources[name] = {"type": "file", "format": "csv", "path": str(tmp_path / f"{name}.csv")}
        targets[name] = {"type": "file", "format": "csv", "path": str(tmp_path / f"{name}_out.csv")}
    cfg: dict = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": sources,
        "tables": tables,
        "targets": targets,
    }
    if relationships:
        cfg["relationships"] = relationships
    return _validated_dump(cfg)


def _pa_chunks(table: pa.Table, size: int) -> list[pa.Table]:
    return [table.slice(i, size) for i in range(0, table.num_rows, size)]


def _write_csv_stub(tmp_path, name: str, table: pa.Table) -> None:
    """Best-effort CSV mirror of `table` so the config's declared source path
    exists; the actual masking data always comes from the `sources=` kwarg,
    never a re-read of this file, so a lossy round-trip here is harmless."""
    try:
        table.to_pandas().to_csv(tmp_path / f"{name}.csv", index=False)
    except Exception:
        pd.DataFrame({c: [] for c in table.column_names}).to_csv(
            tmp_path / f"{name}.csv", index=False
        )


def _install_fake_ner(monkeypatch, spans_by_marker: dict[str, str]) -> None:
    """Route `ner: true` through hand-picked, deterministic spans instead of
    a real spaCy model (not installed in this environment). `spans_by_marker`
    maps a literal substring to the detector_id its Span should carry; the
    stub finds each marker in the cell text and returns a Span covering it,
    merged into `iter_spans`'s overlap resolution exactly like a real NER
    hit would be."""
    monkeypatch.setattr("decoy_engine.storm.ner.spacy_installed", lambda: True)
    monkeypatch.setattr("decoy_engine.storm.ner.model_installed", lambda model=None: True)

    def _fake_iter_ner_spans(text, *, model=None, entities=None):
        found = []
        for marker, detector_id in spans_by_marker.items():
            idx = text.find(marker)
            if idx >= 0:
                found.append(Span(detector_id, idx, idx + len(marker), marker))
        return found

    monkeypatch.setattr("decoy_engine.storm.ner.iter_ner_spans", _fake_iter_ner_spans)


def _extract_span(masked_cell: str) -> str:
    """`"pre {span} post"` under `unmatched_span_policy=passthrough` keeps the
    prefix/suffix verbatim, so the substituted span is whatever remains."""
    assert masked_cell.startswith("pre ") and masked_cell.endswith(" post"), masked_cell
    return masked_cell[len("pre ") : -len(" post")]


# ---------------------------------------------------------------------------
# Branch semantics: distinguishes a real dispatch from an accidental redact
# fallback (the exact hazard the plan calls out).
# ---------------------------------------------------------------------------


def _check_fpe(original: str, masked: str) -> None:
    assert masked != original
    assert re.fullmatch(r"\d{3}-\d{2}-\d{4}", masked), masked


def _check_faker(original: str, masked: str) -> None:
    assert masked != original
    assert masked != _TOKEN
    assert _PERSON_MARKER not in masked


def _check_date_shift(original: str, masked: str) -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", masked), masked
    # min_days == max_days == 1 -> the span is shifted by EXACTLY one day, so a
    # redact-fallback (masked == token) or an unshifted passthrough both fail.
    expected = (_dt.datetime.strptime(original, "%Y-%m-%d") + _dt.timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    assert masked == expected, f"{original} -> {masked}, expected {expected}"


def _check_passthrough(original: str, masked: str) -> None:
    assert masked == original


def _check_redact(original: str, masked: str) -> None:
    assert masked == _TOKEN


BRANCH_CASES: list[dict[str, Any]] = [
    {
        "name": "fpe",
        "provider_config": {"detectors": ["ssn"], "unmatched_span_policy": "passthrough"},
        "ner_markers": None,
        "span_text": _SSN,
        # The ssn regex excludes 000/666/9xx prefixes (SSA-invalid ranges);
        # every key here must be a genuinely matchable span.
        "fk_raw_keys": ["111-22-3333", "123-45-6789", "234-56-7890"],
        "fk_ner_markers": None,
        "check": _check_fpe,
    },
    {
        "name": "faker",
        "provider_config": {"ner": True, "unmatched_span_policy": "passthrough"},
        "ner_markers": {_PERSON_MARKER: "person_name"},
        "span_text": _PERSON_MARKER,
        "fk_raw_keys": ["PERSON_A_ZQ", "PERSON_B_ZQ", "PERSON_C_ZQ"],
        "fk_ner_markers": {
            "PERSON_A_ZQ": "person_name",
            "PERSON_B_ZQ": "person_name",
            "PERSON_C_ZQ": "person_name",
        },
        "check": _check_faker,
    },
    {
        "name": "date_shift",
        # min_days == max_days == 1 pins the keyed offset to exactly +1 day, so
        # _check_date_shift can assert the EXACT shifted date (proving a real
        # shift, not a redact-fallback that a "valid date" check would false-pass).
        "provider_config": {
            "ner": True,
            "unmatched_span_policy": "passthrough",
            "min_days": 1,
            "max_days": 1,
        },
        "ner_markers": {_DATE_TEXT: "iso_date"},
        "span_text": _DATE_TEXT,
        "fk_raw_keys": ["1990-06-15", "2001-11-02", "1975-01-30"],
        "fk_ner_markers": {
            "1990-06-15": "iso_date",
            "2001-11-02": "iso_date",
            "1975-01-30": "iso_date",
        },
        "check": _check_date_shift,
    },
    {
        "name": "passthrough",
        "provider_config": {
            "detectors": ["email"],
            "per_detector_strategy": {"email": "passthrough"},
            "unmatched_span_policy": "passthrough",
        },
        "ner_markers": None,
        "span_text": _EMAIL,
        "fk_raw_keys": ["a@example.com", "b@example.com", "c@example.com"],
        "fk_ner_markers": None,
        "check": _check_passthrough,
    },
    {
        "name": "redact",
        "provider_config": {"detectors": ["url"], "unmatched_span_policy": "passthrough"},
        "ner_markers": None,
        "span_text": _URL,
        "fk_raw_keys": [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ],
        "fk_ner_markers": None,
        "check": _check_redact,
    },
    {
        "name": "unknown_fallback",
        "provider_config": {"ner": True, "unmatched_span_policy": "passthrough"},
        "ner_markers": {_UNKNOWN_MARKER: "totally_unmapped_detector_xyz"},
        "span_text": _UNKNOWN_MARKER,
        "fk_raw_keys": ["UNK_A_ZQ", "UNK_B_ZQ", "UNK_C_ZQ"],
        "fk_ner_markers": {
            "UNK_A_ZQ": "totally_unmapped_detector_xyz",
            "UNK_B_ZQ": "totally_unmapped_detector_xyz",
            "UNK_C_ZQ": "totally_unmapped_detector_xyz",
        },
        "check": _check_redact,
    },
]


# ---------------------------------------------------------------------------
# 1. Byte-identity across chunkings, per REAL branch (branch semantics).
# ---------------------------------------------------------------------------


class TestBranchSemantics:
    @pytest.mark.parametrize("chunk_size", [1, 7, 500])
    @pytest.mark.parametrize("case", BRANCH_CASES, ids=lambda c: c["name"])
    def test_chunked_equals_full_frame_with_branch_semantics(
        self, tmp_path, monkeypatch, case, chunk_size
    ) -> None:
        if case["ner_markers"]:
            _install_fake_ner(monkeypatch, case["ner_markers"])
        span = case["span_text"]
        rows = [f"pre {span} post" if i % 3 == 0 else f"plain filler row {i}" for i in range(12)]
        table = pa.table({"cell": pa.array(rows, type=pa.string())})
        columns = [
            {"name": "cell", "strategy": "text_mask", "provider_config": case["provider_config"]}
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        sources = {"records": table}

        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=chunk_size,
        )
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked", (
            f"{case['name']} must actually route through the chunked entrypoint"
        )
        assert forced.quality_metrics["auto_chunk"]["mode"] == "full_frame"
        a, f = auto.outputs["records"], forced.outputs["records"]
        assert a.equals(f), f"{case['name']}: chunked output diverged from the full-frame oracle"

        masked = a.column("cell")[0].as_py()
        case["check"](span, _extract_span(masked))


class TestUnmatchedSpanPolicies:
    @pytest.mark.parametrize(
        "policy,expected_unmatched",
        [("redact", _TOKEN), ("passthrough", None), ("replace_with_token", "[UNMATCHED]")],
    )
    @pytest.mark.parametrize("chunk_size", [1, 7, 500])
    def test_policy_byte_identical_and_applies_to_fully_unmatched_cells(
        self, tmp_path, policy, expected_unmatched, chunk_size
    ) -> None:
        unmatched_row = "no ssn in this filler text"
        rows = [f"prefix {_SSN} suffix" if i % 4 == 0 else unmatched_row for i in range(12)]
        table = pa.table({"cell": pa.array(rows, type=pa.string())})
        columns = [
            {
                "name": "cell",
                "strategy": "text_mask",
                "provider_config": {"detectors": ["ssn"], "unmatched_span_policy": policy},
            }
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        sources = {"records": table}
        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=chunk_size,
        )
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        a, f = auto.outputs["records"], forced.outputs["records"]
        assert a.equals(f)

        actual_unmatched = a.column("cell")[1].as_py()  # row 1 is never index 0 mod 4
        if policy == "passthrough":
            assert actual_unmatched == unmatched_row
        else:
            assert actual_unmatched == expected_unmatched


# ---------------------------------------------------------------------------
# 2. Per-span date format survives a same/mixed-format chunk boundary
#    (Trap C).
# ---------------------------------------------------------------------------


class TestDateFormatChunkBoundary:
    @pytest.mark.parametrize("chunk_size", [1, 3, 4, 500])
    def test_same_and_mixed_format_dates_across_chunk_boundary(
        self, tmp_path, monkeypatch, chunk_size
    ) -> None:
        iso_date = "1990-06-15"
        us_date = "06/20/2001"
        _install_fake_ner(monkeypatch, {iso_date: "iso_date", us_date: "us_date"})
        # Alternating formats so every chunk size splits a mixed run somewhere.
        rows = [f"pre {iso_date} post" if i % 2 == 0 else f"pre {us_date} post" for i in range(12)]
        table = pa.table({"cell": pa.array(rows, type=pa.string())})
        columns = [
            {
                "name": "cell",
                "strategy": "text_mask",
                "provider_config": {"ner": True, "unmatched_span_policy": "passthrough"},
            }
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        sources = {"records": table}
        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=chunk_size,
        )
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        a, f = auto.outputs["records"], forced.outputs["records"]
        assert a.equals(f)

        iso_masked = _extract_span(a.column("cell")[0].as_py())
        us_masked = _extract_span(a.column("cell")[1].as_py())
        _dt.datetime.strptime(iso_masked, "%Y-%m-%d")  # each keeps its OWN format
        _dt.datetime.strptime(us_masked, "%m/%d/%Y")


# ---------------------------------------------------------------------------
# 3. FK self-mask RI, per branch (Trap A). Own-value-keyed: parent and child
#    mask an equal raw key to identical bytes regardless of chunking.
# ---------------------------------------------------------------------------


class TestFkSelfMaskRI:
    """Was a byte-parity proof (chunked FK self-mask == full-frame, across
    chunk boundaries, per text_mask branch): `text_mask` is one of the
    strategies the 2026-09-02 cascade-safety plan DROPS from the FK self-mask
    allowlist (`gate_fk_child_edges` condition (a) now admits `hash` only),
    so this is now a gate-kill instead -- the config is rejected at compile
    time, before any chunk is ever read, regardless of chunk_size or which
    span sub-provider `provider_config` dispatches to (the reject fires on
    the OUTER `text_mask` strategy name alone). Dropped the `chunk_size`
    parametrize dimension: a compile-time reject cannot depend on how the
    (never-read) input would have been chunked."""

    @pytest.mark.parametrize("case", BRANCH_CASES, ids=lambda c: c["name"])
    def test_fk_self_mask_now_gate_killed_regardless_of_branch(
        self, tmp_path, monkeypatch, case
    ) -> None:
        if case["fk_ner_markers"]:
            _install_fake_ner(monkeypatch, case["fk_ner_markers"])
        provider_config = case["provider_config"]
        raw_keys = case["fk_raw_keys"] * 3

        child_table = pa.table({"customer_id": pa.array(raw_keys, type=pa.string())})
        parent_columns = [
            {
                "name": "id",
                "strategy": "text_mask",
                "dtype": "string",
                "provider_config": provider_config,
            }
        ]
        child_columns = [
            {
                "name": "customer_id",
                "strategy": "text_mask",
                "dtype": "string",
                "provider_config": provider_config,
            }
        ]
        relationships = [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "remap",
            }
        ]
        cfg = _config(
            tmp_path,
            parent_columns,
            table="customers",
            relationships=relationships,
            extra_tables=[{"name": "orders", "columns": child_columns}],
        )

        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _pa_chunks(child_table, 2),
                    table="orders",
                    engine_version=_ENGINE_VERSION,
                )
            )
        assert exc.value.code == "chunked_fk_parent_strategy_not_self_mask_safe", case["name"]


class TestFkNegativeAndNamespaceIndependence:
    def test_text_mask_admitted_into_chunk_safe_and_excluded_from_namespace_requiring(
        self,
    ) -> None:
        assert "text_mask" in CHUNK_SAFE_STRATEGIES
        assert "text_mask" not in NAMESPACE_REQUIRING_STRATEGIES

    def test_child_config_mismatch_rejected(self, tmp_path) -> None:
        """`text_mask` is dropped from the FK self-mask allowlist by the
        2026-09-02 cascade-safety plan (condition (a) now admits `hash`
        only), which fires BEFORE condition (e)'s provider_config-mismatch
        check this test used to reach -- so the code flips to the allowlist
        reject. The provider_config-mismatch check itself is still fully
        exercised for `hash` in test_chunked_fk_gate_kills.py."""
        cfg = {
            "global_settings": {"seed": 7},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "id",
                            "strategy": "text_mask",
                            "dtype": "string",
                            "provider_config": {"detectors": ["ssn"], "token": "[REDACTED]"},
                        }
                    ],
                },
                {
                    "name": "orders",
                    "columns": [
                        {
                            "name": "customer_id",
                            "strategy": "text_mask",
                            "dtype": "string",
                            # Different token: the child would NOT reproduce
                            # the parent's masked bytes for redacted spans.
                            "provider_config": {"detectors": ["ssn"], "token": "[HIDDEN]"},
                        }
                    ],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "remap",
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="orders")
        assert exc.value.code == "chunked_fk_parent_strategy_not_self_mask_safe"

    def test_namespace_absent_vs_differing_produces_identical_output(self, tmp_path) -> None:
        """text_mask keys on ctx.mask_key, not a per-column namespace: two
        columns with NO namespace agreement (one absent, one set) must still
        mask an equal raw value to the identical bytes -- proving it correctly
        stays out of NAMESPACE_REQUIRING_STRATEGIES."""
        raw = _SSN
        table = pa.table(
            {"a": pa.array([raw], type=pa.string()), "b": pa.array([raw], type=pa.string())}
        )
        columns = [
            {"name": "a", "strategy": "text_mask", "provider_config": {"detectors": ["ssn"]}},
            {
                "name": "b",
                "strategy": "text_mask",
                "namespace": "some_other_ns",
                "provider_config": {"detectors": ["ssn"]},
            },
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        out = run_pipeline(
            cfg, sources={"records": table}, engine_version=_ENGINE_VERSION, auto_chunk=False
        ).outputs["records"]
        assert out.column("a")[0].as_py() == out.column("b")[0].as_py()


# ---------------------------------------------------------------------------
# 5. Output dtype invariance + null breadth (Trap B).
# ---------------------------------------------------------------------------


class TestOutputDtypeInvariance:
    @pytest.mark.parametrize("policy", ["redact", "passthrough", "replace_with_token"])
    def test_all_null_chunk_vs_masked_chunk_no_schema_mismatch(self, tmp_path, policy) -> None:
        values: list[str | None] = [None, None, None, "abc", _SSN, None, "def", None]
        table = pa.table({"cell": pa.array(values, type=pa.string())})
        columns = [
            {
                "name": "cell",
                "strategy": "text_mask",
                "provider_config": {"detectors": ["ssn"], "unmatched_span_policy": policy},
            }
        ]
        cfg = _config(tmp_path, columns)
        chunks = list(
            run_mask_pipeline_chunked(
                cfg, _pa_chunks(table, 3), table="records", engine_version=_ENGINE_VERSION
            )
        )
        assert len(chunks) == 3  # the first is entirely null
        for chunk in chunks:
            field_type = chunk.schema.field("cell").type
            assert field_type == pa.string() or pa.types.is_null(field_type)
        combined = concat_masked_chunks(chunks, table="records")
        assert combined.schema.field("cell").type == pa.string()
        masked_vals = combined.column("cell").to_pylist()
        assert masked_vals[0] is None and masked_vals[1] is None and masked_vals[2] is None
        assert masked_vals[4] != _SSN  # the ssn span, actually masked

    def test_extension_dtype_and_pandas_null_spellings_mask_without_error(self) -> None:
        """pd.NA / NaN / None / an extension-array column are a handler-level
        concern (the pandas<->cell boundary, not the pa.Table chunk boundary);
        mirrors test_text_mask_ner.py's extension-dtype coverage."""
        df = pd.DataFrame({"cell": pd.array([None, float("nan"), pd.NA, "", _SSN], dtype="object")})
        seed = ColumnSeed(
            namespace=None,
            strategy="text_mask",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="bijective",
            deterministic=False,
            provider_config=(("detectors", ("ssn",)),),
        )
        out, warnings = TextMaskHandler().run(df.copy(), "cell", seed, _FakeCtx())
        assert warnings == []
        vals = out["cell"].tolist()
        assert pd.isna(vals[0]) and pd.isna(vals[1]) and pd.isna(vals[2])
        assert vals[3] == ""  # empty string: falsy, mask_cell's guard returns it unchanged
        assert vals[4] != _SSN


# ---------------------------------------------------------------------------
# 6. NER default (chunk-invariant) + version-mismatch guard (Trap D).
# ---------------------------------------------------------------------------


class TestNerDefaultAndVersionGuard:
    def test_default_no_ner_job_chunks_byte_identically(self, tmp_path) -> None:
        rows = [f"contact us at {_EMAIL}" if i % 2 == 0 else "plain row" for i in range(12)]
        table = pa.table({"cell": pa.array(rows, type=pa.string())})
        columns = [
            {"name": "cell", "strategy": "text_mask", "provider_config": {"detectors": ["email"]}}
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        sources = {"records": table}
        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=5,
        )
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["records"].equals(forced.outputs["records"])

    def _install_version_drift(self, monkeypatch, *, compiled: str, runtime: str) -> None:
        """The compile-time stamp reads `installed_model_version` ONCE
        (`_seed_envelope.py`); each `handler.run()` call reads it again at
        execution. Returning `compiled` on the first call and `runtime` on
        every call after reproduces a real model upgrade between compile and
        run without touching the filesystem."""
        monkeypatch.setattr("decoy_engine.storm.ner.spacy_installed", lambda: True)
        monkeypatch.setattr("decoy_engine.storm.ner.model_installed", lambda model=None: True)
        monkeypatch.setattr("decoy_engine.storm.ner.iter_ner_spans", lambda *a, **k: [])
        calls = {"n": 0}

        def _fake_installed_model_version(model=None):
            calls["n"] += 1
            return compiled if calls["n"] == 1 else runtime

        monkeypatch.setattr(
            "decoy_engine.storm.ner.installed_model_version", _fake_installed_model_version
        )

    def test_stamped_version_mismatch_raises_full_frame(self, tmp_path, monkeypatch) -> None:
        self._install_version_drift(monkeypatch, compiled="1.0.0", runtime="2.0.0")
        table = pa.table({"cell": pa.array(["hello there"], type=pa.string())})
        columns = [{"name": "cell", "strategy": "text_mask", "provider_config": {"ner": True}}]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        with pytest.raises(StrategyError) as exc:
            run_pipeline(
                cfg, sources={"records": table}, engine_version=_ENGINE_VERSION, auto_chunk=False
            )
        assert exc.value.code == "ner_model_version_mismatch"

    def test_stamped_version_mismatch_raises_chunked(self, tmp_path, monkeypatch) -> None:
        self._install_version_drift(monkeypatch, compiled="1.0.0", runtime="2.0.0")
        table = pa.table({"cell": pa.array(["hello there"] * 6, type=pa.string())})
        columns = [{"name": "cell", "strategy": "text_mask", "provider_config": {"ner": True}}]
        cfg = _config(tmp_path, columns)
        with pytest.raises(StrategyError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, _pa_chunks(table, 2), table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "ner_model_version_mismatch"

    def test_runtime_version_lookup_returning_none_does_not_fire_guard(self, monkeypatch) -> None:
        """A stamp is present (ner_model_version="1.0.0") but the RUNTIME
        lookup returns None (e.g. the package was uninstalled between compile
        and run): the guard must not fire on an unknowable comparison."""
        monkeypatch.setattr(
            "decoy_engine.storm.ner.installed_model_version", lambda model=None: None
        )
        monkeypatch.setattr("decoy_engine.storm.ner.iter_ner_spans", lambda *a, **k: [])
        df = pd.DataFrame({"cell": ["hello there"]})
        seed = ColumnSeed(
            namespace=None,
            strategy="text_mask",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="bijective",
            deterministic=False,
            provider_config=(("ner", True),),
            ner_model_version="1.0.0",
        )
        # No StrategyError: the guard's inner `current_version is not None`
        # check short-circuits before comparing to plan.ner_model_version.
        out, warnings = TextMaskHandler().run(df.copy(), "cell", seed, _FakeCtx())
        assert warnings == []
        # Ordinary redact of the unmatched cell -- proves execution proceeded
        # past the guard rather than the guard call being skipped entirely.
        assert out["cell"].iloc[0] == _TOKEN


# ---------------------------------------------------------------------------
# 7. `text_mask` + `when:` is REJECTED (Trap E), fail-closed.
# ---------------------------------------------------------------------------


def _when_bearing_text_mask_cfg(tmp_path) -> dict:
    # A column name that does NOT collide with any word in the rejection
    # message's own prose ("cell" would, since the message explains the
    # handler str()-converts "every non-null cell" -- a substring check
    # against the column name would then pass on ANY name, masking a mutant
    # that drops the real name entirely).
    columns = [
        {"name": "target_col", "strategy": "text_mask", "provider_config": {"detectors": ["ssn"]}}
    ]
    cfg = _config(tmp_path, columns)
    cfg["tables"][0]["columns"][0]["when"] = "target_col != ''"
    return cfg


class TestWhenRejection:
    def test_manual_entrypoint_raises(self, tmp_path) -> None:
        table = pa.table({"target_col": pa.array(["a", "b"], type=pa.string())})
        cfg = _when_bearing_text_mask_cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, _pa_chunks(table, 1), table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "chunked_text_mask_when_not_supported"

    def test_check_chunked_compatibility_raises_directly(self, tmp_path) -> None:
        cfg = _when_bearing_text_mask_cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="records")
        assert exc.value.code == "chunked_text_mask_when_not_supported"
        assert exc.value.path == "tables.records.columns"
        assert "column(s) target_col combine" in exc.value.message  # the offending column, by NAME
        # Pin each trailing "why" fragment's exact casing: kills a mutant that
        # UPPERCASES a whole fragment. A mutant that only pads a fragment with
        # an "XX" marker (leaving the inner text intact) survives this style of
        # check -- killable only via brittle full-message equality -- and is
        # accepted as a documented non-contract survivor, the same policy
        # `_chunked_dgrn.py`'s mutation ledger established for the sibling
        # `reject_windowed_date_when`.
        assert (
            "with a 'when:' predicate, which is not supported on the chunked" in exc.value.message
        )
        assert "route: the handler str()-converts every non-null cell" in exc.value.message
        assert "when-gated column leaves non-matching rows at their original" in exc.value.message
        assert "(possibly numeric) dtype while matching rows become masked" in exc.value.message
        assert "chunk-boundary-dependent output dtype" in exc.value.message

    def test_two_offending_columns_both_named_and_comma_joined(self, tmp_path) -> None:
        """Pins the exact `', '.join(...)` separator and that BOTH offending
        column names are read (not a hardcoded default)."""
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "col_a",
                    "strategy": "text_mask",
                    "provider_config": {"detectors": ["ssn"]},
                },
                {
                    "name": "col_b",
                    "strategy": "text_mask",
                    "provider_config": {"detectors": ["ssn"]},
                },
            ],
        )
        cfg["tables"][0]["columns"][0]["when"] = "col_a != ''"
        cfg["tables"][0]["columns"][1]["when"] = "col_b != ''"
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="records")
        assert "col_a, col_b" in exc.value.message

    def test_column_missing_name_key_falls_back_to_placeholder(self, tmp_path) -> None:
        """A malformed column entry with no `name` key must not crash the
        gate: it falls back to the `"?"` placeholder, same as every other
        name-reading gate in this codebase."""
        cfg = _config(
            tmp_path,
            [{"name": "keep", "strategy": "text_mask", "provider_config": {"detectors": ["ssn"]}}],
        )
        cfg["tables"][0]["columns"].append(
            {
                "strategy": "text_mask",
                "when": "1 == 1",
                "provider_config": {"detectors": ["ssn"]},
            }
        )
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="records")
        assert "column(s) ? combine" in exc.value.message

    def test_reject_text_mask_when_direct_unit_raises_on_when(self, tmp_path) -> None:
        cfg = _when_bearing_text_mask_cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            reject_text_mask_when(cfg["tables"][0], table="records")
        assert exc.value.code == "chunked_text_mask_when_not_supported"

    def test_reject_text_mask_when_direct_unit_returns_without_when(self, tmp_path) -> None:
        columns = [
            {
                "name": "target_col",
                "strategy": "text_mask",
                "provider_config": {"detectors": ["ssn"]},
            }
        ]
        cfg = _config(tmp_path, columns)
        reject_text_mask_when(cfg["tables"][0], table="records")  # must not raise

    def test_auto_route_falls_back_to_full_frame(self, tmp_path) -> None:
        rows = [f"row {i}" for i in range(30)]
        table = pa.table({"target_col": pa.array(rows, type=pa.string())})
        cfg = _when_bearing_text_mask_cfg(tmp_path)
        _write_csv_stub(tmp_path, "records", table)
        result = run_pipeline(
            cfg,
            sources={"records": table},
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=7,
        )
        assert result.quality_metrics["auto_chunk"]["mode"] == "full_frame"


# ---------------------------------------------------------------------------
# 8. Admission surfaces: manual entry, auto route, cross-substrate polars.
# ---------------------------------------------------------------------------


class TestSourceDtypeGate:
    """text_mask requires a chunk-stable STRING source. A non-string (int)
    source diverges by chunk boundary under the handler's str()-conversion (a
    null-free chunk stays int64 -> "1"; a null-bearing chunk widens to float64
    -> "1.0"), and would break byte-parity + FK RI on the manual/FK route
    (the manual/FK-route byte-parity + RI hazard). Rejected fail-closed at both entries."""

    def _int_source_cfg(self, tmp_path):
        columns = [
            {"name": "amount", "strategy": "text_mask", "provider_config": {"detectors": ["ssn"]}}
        ]
        return _config(tmp_path, columns)

    def test_manual_entry_raises_on_int_source(self, tmp_path) -> None:
        table = pa.table({"amount": pa.array([1, None, 2], type=pa.int64())})
        cfg = self._int_source_cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, _pa_chunks(table, 1), table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "chunked_text_mask_source_dtype_unsupported"
        assert "amount" in exc.value.message

    def test_manual_entry_raises_on_later_chunk_dtype_drift(self, tmp_path) -> None:
        # A caller feeding a STRING first chunk (passes admission) then a
        # divergent INT chunk must be caught PER CHUNK, not just on the first
        # (the manual iterable's dtype can drift across chunks).
        cfg = self._int_source_cfg(tmp_path)  # text_mask on "amount"
        chunk1 = pa.table({"amount": pa.array(["a", "b"], type=pa.string())})
        chunk2 = pa.table({"amount": pa.array([1, None, 2], type=pa.int64())})
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, [chunk1, chunk2], table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "chunked_text_mask_source_dtype_unsupported"

    def test_auto_route_falls_back_to_oracle_on_int_source(self, tmp_path) -> None:
        # Null-free int: the pre-existing integer-with-nulls gate does NOT catch
        # it, so this proves THIS slice's source-dtype gate closes the auto route.
        table = pa.table({"amount": pa.array([1, 2, 3, 4, 5, 6], type=pa.int64())})
        cfg = self._int_source_cfg(tmp_path)
        _write_csv_stub(tmp_path, "records", table)
        result = run_pipeline(
            cfg,
            sources={"records": table},
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=2,
        )
        assert result.quality_metrics["auto_chunk"]["mode"] == "full_frame"

    def test_collector_flags_non_string_admits_string(self) -> None:
        # Direct (fast) unit test of the shared collector. A text_mask node on a
        # string source is safe (empty); on an int source it is offending.
        from types import SimpleNamespace

        def node(strategy: str, column: str, table: str = "records", kind: str = "scalar"):
            return SimpleNamespace(table=table, kind=kind, strategy=strategy, columns=(column,))

        str_schema = pa.schema([("cell", pa.string())])
        int_schema = pa.schema([("cell", pa.int64())])
        nodes = [node("text_mask", "cell"), node("hash", "other")]
        assert unsafe_text_mask_source_columns(nodes, str_schema, table="records") == []
        assert unsafe_text_mask_source_columns(nodes, int_schema, table="records") == ["cell"]
        # a large_string source is also safe; a non-text_mask node is ignored
        assert (
            unsafe_text_mask_source_columns(
                nodes, pa.schema([("cell", pa.large_string())]), table="records"
            )
            == []
        )

    def test_collector_skips_other_table_and_non_scalar_nodes(self) -> None:
        # Pins the skip guard's three dimensions (table / kind / strategy): a
        # text_mask node on a DIFFERENT table, or a NON-scalar text_mask node,
        # even on an int source, must be ignored for `table="records"`.
        from types import SimpleNamespace

        def node(strategy, column, table="records", kind="scalar"):
            return SimpleNamespace(table=table, kind=kind, strategy=strategy, columns=(column,))

        int_schema = pa.schema([("cell", pa.int64())])
        # only same-table + scalar + text_mask counts:
        assert (
            unsafe_text_mask_source_columns(
                [node("text_mask", "cell", table="other_tbl")], int_schema, table="records"
            )
            == []
        )
        assert (
            unsafe_text_mask_source_columns(
                [node("text_mask", "cell", kind="composite")], int_schema, table="records"
            )
            == []
        )
        # and a genuine same-table scalar text_mask on the int source IS flagged
        assert unsafe_text_mask_source_columns(
            [node("text_mask", "cell")], int_schema, table="records"
        ) == ["cell"]
        # a SKIP node (other strategy) BEFORE the matching text_mask node must
        # not stop the scan (guards `continue`, not `break`).
        assert unsafe_text_mask_source_columns(
            [node("hash", "x"), node("text_mask", "cell")], int_schema, table="records"
        ) == ["cell"]
        # a column absent from the schema cannot be proven safe -> flagged
        assert unsafe_text_mask_source_columns(
            [node("text_mask", "missing")], int_schema, table="records"
        ) == ["missing"]

    def _compiled(self, cfg, table_data, table: str = "records"):
        from decoy_engine.execution._chunked_profile import first_chunk_profile
        from decoy_engine.plan import compile_plan
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph

        profile = first_chunk_profile(table_data, table=table, engine_version=_ENGINE_VERSION)
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION, no_profile=True)
        return plan, get_default_registry(), RelationshipGraph(edges=(), ordering=())

    def test_reject_wrapper_returns_on_string_raises_on_int(self, tmp_path) -> None:
        # Fast direct coverage of the raising wrapper's guard (string -> return;
        # int -> raise), so it does not depend on the slow full-pipeline tests.
        from decoy_engine.execution._chunked_text_mask import reject_unsafe_text_mask_source_dtype

        cfg = self._int_source_cfg(tmp_path)  # text_mask on "amount"
        str_table = pa.table({"amount": pa.array(["a", "b"], type=pa.string())})
        plan_s, reg, graph = self._compiled(cfg, str_table)
        # string source -> must NOT raise
        reject_unsafe_text_mask_source_dtype(
            plan_s, str_table.schema, table="records", registry=reg, relationship_graph=graph
        )
        int_table = pa.table({"amount": pa.array([1, 2], type=pa.int64())})
        plan_i, _, _ = self._compiled(cfg, int_table)
        with pytest.raises(PlanCompileError) as exc:
            reject_unsafe_text_mask_source_dtype(
                plan_i, int_table.schema, table="records", registry=reg, relationship_graph=graph
            )
        assert exc.value.code == "chunked_text_mask_source_dtype_unsupported"
        assert exc.value.path == "tables.records.columns"  # pins the coded path field
        assert "amount" in exc.value.message

    def test_reject_wrapper_two_offending_columns_comma_joined(self, tmp_path) -> None:
        # Two non-string text_mask columns -> both named, comma-joined (pins the
        # `', '.join(...)` separator, not a hardcoded single name).
        from decoy_engine.execution._chunked_text_mask import reject_unsafe_text_mask_source_dtype

        cfg = _config(
            tmp_path,
            [
                {"name": "aaa", "strategy": "text_mask", "provider_config": {"detectors": ["ssn"]}},
                {"name": "bbb", "strategy": "text_mask", "provider_config": {"detectors": ["ssn"]}},
            ],
        )
        table = pa.table(
            {"aaa": pa.array([1, 2], type=pa.int64()), "bbb": pa.array([3, 4], type=pa.int64())}
        )
        plan, reg, graph = self._compiled(cfg, table)
        with pytest.raises(PlanCompileError) as exc:
            reject_unsafe_text_mask_source_dtype(
                plan, table.schema, table="records", registry=reg, relationship_graph=graph
            )
        assert "aaa, bbb" in exc.value.message

    def test_registry_is_load_bearing_with_a_provider_backed_sibling(self, tmp_path) -> None:
        # A text_mask column beside a provider-backed faker column: build_work_list
        # consults the registry (provider_is_composite) for the faker node, so the
        # source-column collector must thread the REAL registry through -- passing
        # None would raise on the faker node. Pins that the registry is load-bearing
        # here (the build_work_list registry argument is not a dead value).
        from decoy_engine.execution._chunked_text_mask import (
            reject_unsafe_text_mask_source_dtype,
            text_mask_source_columns,
        )

        cfg = _config(
            tmp_path,
            [
                {
                    "name": "note",
                    "strategy": "text_mask",
                    "provider_config": {"detectors": ["ssn"]},
                },
                {"name": "nm", "strategy": "faker", "provider": "person_first_name"},
            ],
        )
        table = pa.table(
            {"note": pa.array(["hi"], type=pa.string()), "nm": pa.array(["Ann"], type=pa.string())}
        )
        plan, reg, graph = self._compiled(cfg, table)
        assert text_mask_source_columns(plan, reg, graph, table="records") == ["note"]
        # string source -> the reject wrapper must not raise; its own build_work_list
        # call must likewise receive the real registry, not None.
        reject_unsafe_text_mask_source_dtype(
            plan, table.schema, table="records", registry=reg, relationship_graph=graph
        )


class TestAdmissionSurfaces:
    def test_manual_entry_admits_text_mask_job(self, tmp_path) -> None:
        table = pa.table({"cell": pa.array([f"id {i} {_SSN}" for i in range(5)], type=pa.string())})
        columns = [
            {"name": "cell", "strategy": "text_mask", "provider_config": {"detectors": ["ssn"]}}
        ]
        cfg = _config(tmp_path, columns)
        out = list(
            run_mask_pipeline_chunked(
                cfg, _pa_chunks(table, 2), table="records", engine_version=_ENGINE_VERSION
            )
        )
        assert sum(c.num_rows for c in out) == 5

    def test_auto_route_selects_chunked_mode(self, tmp_path) -> None:
        table = pa.table(
            {"cell": pa.array([f"id {i} {_SSN}" for i in range(30)], type=pa.string())}
        )
        columns = [
            {"name": "cell", "strategy": "text_mask", "provider_config": {"detectors": ["ssn"]}}
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        result = run_pipeline(
            cfg,
            sources={"records": table},
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=7,
        )
        assert result.quality_metrics["auto_chunk"]["mode"] == "chunked"

    def test_cross_substrate_polars_value_equals_pandas_oracle(self, tmp_path) -> None:
        table = pa.table(
            {"cell": pa.array([f"id {i} {_SSN}" for i in range(20)], type=pa.string())}
        )
        columns = [
            {"name": "cell", "strategy": "text_mask", "provider_config": {"detectors": ["ssn"]}}
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        full = run_pipeline(
            cfg, sources={"records": table}, engine_version=_ENGINE_VERSION
        ).outputs["records"]
        polars_chunked = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _pa_chunks(table, 6),
                    table="records",
                    engine_version=_ENGINE_VERSION,
                    adapter=PolarsExecutionAdapter(),
                )
            )
        ).combine_chunks()
        assert polars_chunked.column_names == full.column_names
        assert polars_chunked.to_pydict() == full.to_pydict()


# ---------------------------------------------------------------------------
# 9. Warnings contract vs the unmatched-passthrough log.
# ---------------------------------------------------------------------------


class TestWarningsAndLogging:
    def test_handler_always_returns_no_warnings(self) -> None:
        df = pd.DataFrame({"cell": ["hello", None, _SSN]})
        seed = ColumnSeed(
            namespace=None,
            strategy="text_mask",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="bijective",
            deterministic=False,
            provider_config=(("detectors", ("ssn",)),),
        )
        _, warnings = TextMaskHandler().run(df.copy(), "cell", seed, _FakeCtx())
        assert warnings == []

    def test_passthrough_policy_log_identical_between_chunked_and_full_frame(
        self, tmp_path, caplog
    ) -> None:
        rows = [f"plain filler {i}" for i in range(12)]  # no spans: the whole cell "unmatched"
        table = pa.table({"cell": pa.array(rows, type=pa.string())})
        columns = [
            {
                "name": "cell",
                "strategy": "text_mask",
                "provider_config": {"detectors": ["ssn"], "unmatched_span_policy": "passthrough"},
            }
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)

        caplog.set_level(logging.WARNING, logger="decoy_engine.transforms.text_mask")
        caplog.clear()
        forced = run_pipeline(
            cfg, sources={"records": table}, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        forced_count = sum(1 for r in caplog.records if "passthrough" in r.message)

        caplog.clear()
        auto = run_pipeline(
            cfg,
            sources={"records": table},
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=5,
        )
        auto_count = sum(1 for r in caplog.records if "passthrough" in r.message)

        assert forced.quality_metrics["auto_chunk"]["mode"] == "full_frame"
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert forced_count == 12
        assert auto_count == forced_count
