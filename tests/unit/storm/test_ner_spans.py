"""NER-backed text_redact spans (capability-gaps WS2, 2026-06-12).

The regex span catalog deliberately omits person_name/address (no regex
shape); storm/ner.py fills the hole with spaCy NER under the same Span
contract. Inference cells skip when spacy or en_core_web_sm is absent
(the `ner` extra is optional); the availability/compile-check cells run
everywhere.
"""

from __future__ import annotations

import unicodedata

import pandas as pd
import pytest

from decoy_engine.storm.detectors import Span, iter_spans
from decoy_engine.storm.ner import (
    DEFAULT_NER_MODEL,
    model_installed,
    spacy_installed,
)

_NER_READY = spacy_installed() and model_installed(DEFAULT_NER_MODEL)
needs_ner = pytest.mark.skipif(
    not _NER_READY, reason="spacy or en_core_web_sm not installed (ner extra)"
)


class TestExtraSpansContract:
    """extra_spans joins the regex spans in ONE overlap resolution.
    No optional dependency needed."""

    def test_extra_spans_merge_and_sort(self) -> None:
        text = "call 555-867-5309 about Jane"
        extra = [Span("person_name", 24, 28, "Jane")]
        spans = iter_spans(text, ["us_phone"], extra_spans=extra)
        assert [s.detector_id for s in spans] == ["us_phone", "person_name"]

    def test_overlap_resolves_leftmost_longest_across_sources(self) -> None:
        text = "id 123-45-6789 end"
        # A fake NER span overlapping the SSN but starting later: the
        # regex SSN wins leftmost-then-longest; no double splice.
        extra = [Span("person_name", 6, 14, "45-6789 ")]
        spans = iter_spans(text, ["ssn"], extra_spans=extra)
        assert len(spans) == 1
        assert spans[0].detector_id == "ssn"

    def test_none_extra_spans_is_pre_ws2_behavior(self) -> None:
        text = "mail a@b.com now"
        assert iter_spans(text, ["email"]) == iter_spans(text, ["email"], extra_spans=None)


@needs_ner
class TestNerSpans:
    def test_person_name_found(self) -> None:
        from decoy_engine.storm.ner import iter_ner_spans

        spans = iter_ner_spans("Patient Marie Curie presented with chest pain.")
        names = [s for s in spans if s.detector_id == "person_name"]
        assert names, spans
        assert "Curie" in names[0].matched_text

    def test_entities_filter(self) -> None:
        from decoy_engine.storm.ner import iter_ner_spans

        text = "John Smith flew to Paris."
        only_loc = iter_ner_spans(text, entities=["location"])
        assert {s.detector_id for s in only_loc} <= {"location"}

    def test_deterministic(self) -> None:
        from decoy_engine.storm.ner import iter_ner_spans

        text = "Dr. Ada Lovelace of London reviewed the chart."
        assert iter_ner_spans(text) == iter_ner_spans(text)


@needs_ner
class TestTextRedactWithNer:
    def _run(self, values, provider_config: dict):
        from decoy_engine.execution._adapter import StrategyContext
        from decoy_engine.execution._strategies._text_redact import TextRedactHandler
        from decoy_engine.generation.pool._cache import PoolCache
        from decoy_engine.plan._types import ColumnSeed
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        seed = ColumnSeed(
            namespace=None,
            strategy="text_redact",
            provider="text_redact",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=tuple(provider_config.items()),
            coherent_with=(),
        )
        ctx = StrategyContext(
            registry=get_default_registry(),
            pool_cache=PoolCache(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
            job_seed=b"\x00" * 8,
        )
        df = pd.DataFrame({"notes": values})
        out, _ = TextRedactHandler().run(df, "notes", seed, ctx)
        return out["notes"].tolist()

    def test_ner_true_redacts_person_names(self) -> None:
        out = self._run(
            ["Patient Marie Curie, MRN visible at a@b.com."],
            {"ner": True, "label_token": True},
        )
        assert "[REDACTED:person_name]" in out[0]
        assert "[REDACTED:email]" in out[0]
        assert "Curie" not in out[0]

    def test_ner_off_is_byte_identical_to_pre_ws2(self) -> None:
        text = "Patient Marie Curie, reach a@b.com."
        out = self._run([text], {})
        # No ner key: person names stay (the regex catalog cannot see
        # them); only the email goes.
        assert "Marie Curie" in out[0]
        assert "a@b.com" not in out[0]

    def test_nulls_stay_null(self) -> None:
        out = self._run(["Marie Curie", None], {"ner": True})
        assert out[1] is None or pd.isna(out[1])

    def test_deterministic_across_runs(self) -> None:
        vals = ["Marie Curie wrote from Paris to a@b.com."]
        cfg = {"ner": True, "label_token": True}
        assert self._run(vals, cfg) == self._run(vals, cfg)


class TestCompileCheck:
    def _cfg(self, provider_config: dict) -> dict:
        return {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "notes",
                            "strategy": "text_redact",
                            "provider_config": provider_config,
                        }
                    ],
                }
            ],
        }

    def test_missing_model_rejected_config_only(self) -> None:
        from decoy_engine import run_config_only_checks
        from decoy_engine.plan import PlanCompileError

        cfg = self._cfg({"ner": {"model": "xx_no_such_model"}})
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code in ("ner_model_not_installed", "ner_spacy_not_installed")

    def test_no_ner_key_passes_everywhere(self) -> None:
        from decoy_engine import run_config_only_checks

        names = run_config_only_checks(self._cfg({"detectors": ["email"]}))
        assert "text_redact_ner_available" in names

    @needs_ner
    def test_ner_true_passes_when_installed(self) -> None:
        from decoy_engine import run_config_only_checks

        names = run_config_only_checks(self._cfg({"ner": True}))
        assert "text_redact_ner_available" in names


class TestStreetAddressDetector:
    """Pure-regex street detector (deferred follow-up 8a): house number +
    USPS Pub 28 C1 suffix anchor, no spaCy needed."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("ship to 123 Main St today", "123 Main St"),
            ("at 4567 North Harbor Boulevard, suite open", "4567 North Harbor Boulevard"),
            ("she lives at 9 Oak Ridge Rd.", "9 Oak Ridge Rd."),
            ("deliver to 123 Main St Apt 4 by noon", "123 Main St Apt 4"),
            ("office: 800 W 5th Ave Suite 210, floor 2", "800 W 5th Ave Suite 210"),
        ],
    )
    def test_addresses_found(self, text: str, expected: str) -> None:
        spans = iter_spans(text, ["street_address"])
        assert [s.matched_text for s in spans] == [expected]
        assert spans[0].detector_id == "street_address"

    @pytest.mark.parametrize(
        "text",
        [
            "chapter 12 main idea recap",  # no suffix anchor
            "the 5 best ways to win",  # no suffix
            "Main Street is busy",  # no house number
            "version 2 stable release",  # nothing address-shaped
        ],
    )
    def test_prose_not_shredded(self, text: str) -> None:
        assert iter_spans(text, ["street_address"]) == []

    def test_in_default_detector_set(self) -> None:
        spans = iter_spans("ship to 123 Main St today")
        assert any(s.detector_id == "street_address" for s in spans)

    def test_street_span_resolves_with_regex_overlaps(self) -> None:
        # A zip inside the address text: leftmost-longest keeps the street
        # span whole instead of double-splicing (the text_redact handler
        # splices whatever iter_spans resolves).
        text = "invoice for 99 Elm Street, attn billing"
        spans = iter_spans(text, ["street_address", "us_zip"])
        assert [s.matched_text for s in spans] == ["99 Elm Street"]


class TestLocaleLabelScheme:
    def test_wikiner_per_label_mapped(self) -> None:
        """Non-English models emit PER (WikiNER), not PERSON (OntoNotes);
        both must land on person_name or locale models silently drop
        every person hit."""
        from decoy_engine.storm.ner import NER_ENTITY_MAP

        assert NER_ENTITY_MAP["PER"] == "person_name"
        assert NER_ENTITY_MAP["PERSON"] == "person_name"
        assert NER_ENTITY_MAP["LOC"] == "location"

    @pytest.mark.skipif(
        not (spacy_installed() and model_installed("de_core_news_sm")),
        reason="de_core_news_sm not installed (locale model)",
    )
    def test_german_model_finds_person_and_location(self) -> None:
        from decoy_engine.storm.ner import iter_ner_spans

        spans = iter_ner_spans("Angela Schmidt wohnt in Berlin.", model="de_core_news_sm")
        detectors = {s.detector_id for s in spans}
        assert "person_name" in detectors
        assert "location" in detectors


class TestModelVersionStamp:
    def test_installed_model_version_resolves_known_package(self) -> None:
        from decoy_engine.storm.ner import installed_model_version

        # pytest is always installed; the helper is a plain metadata lookup.
        assert installed_model_version("pytest")
        assert installed_model_version("xx_definitely_not_installed") is None

    def test_column_seed_carries_ner_model_version(self, monkeypatch, tmp_path) -> None:
        import pandas as pd
        import pyarrow as pa

        from decoy_engine.execution._chunked_profile import first_chunk_profile
        from decoy_engine.plan import _compile as compile_mod
        from decoy_engine.plan import compile_plan

        monkeypatch.setattr("decoy_engine.storm.ner.installed_model_version", lambda model: "9.9.9")
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "notes",
                            "strategy": "text_redact",
                            "provider_config": {"ner": True},
                        },
                        {"name": "email", "strategy": "hash", "namespace": "n"},
                    ],
                }
            ],
        }
        df = pd.DataFrame({"notes": ["hello"], "email": ["a@b.com"]})
        profile = first_chunk_profile(
            pa.Table.from_pandas(df, preserve_index=False), table="t", engine_version="x"
        )
        # Row 13 hard-fails when the model is absent; bypass it for the
        # stamp test (the stamp is independent of availability).
        monkeypatch.setattr(compile_mod, "check_text_redact_ner_available", lambda config: None)
        plan = compile_plan(cfg, profile, decoy_engine_version="x", no_profile=True)
        per_column = dict(plan.seed_envelope.per_table[0][1].per_column)
        assert per_column["notes"].ner_model_version == "9.9.9"
        assert per_column["email"].ner_model_version is None

    def test_unresolvable_version_warns(self, monkeypatch) -> None:
        import pandas as pd
        import pyarrow as pa

        from decoy_engine.execution._chunked_profile import first_chunk_profile
        from decoy_engine.plan import _compile as compile_mod
        from decoy_engine.plan import compile_plan

        monkeypatch.setattr("decoy_engine.storm.ner.installed_model_version", lambda model: None)
        monkeypatch.setattr(compile_mod, "check_text_redact_ner_available", lambda config: None)
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "notes",
                            "strategy": "text_redact",
                            "provider_config": {"ner": {"model": "xx_meta_free"}},
                        }
                    ],
                }
            ],
        }
        df = pd.DataFrame({"notes": ["hello"]})
        profile = first_chunk_profile(
            pa.Table.from_pandas(df, preserve_index=False), table="t", engine_version="x"
        )
        plan = compile_plan(cfg, profile, decoy_engine_version="x", no_profile=True)
        assert any("ner_model_version_unavailable" in w for w in plan.plan_compile.warnings)


# ---------------------------------------------------------------------------
# Phase 5 (docs/plans/2026-09-08-p5-ner-batch-helper.md): `iter_ner_spans_batch`
# batches NER inference through `nlp.pipe` instead of one `nlp(text)` call per
# cell, with NO change to the observable span contract. `iter_ner_spans`'s
# single-cell body stays untouched (the independent oracle the differential
# test below compares against). The spaCy-free characterization/guard/scatter
# tests for the batch helper's own logic live in
# `test_ner_batch_fake_pipeline.py` -- a separate module so they collect
# without this file's module-level `spacy_installed()`/`model_installed()`
# call, which a mutation harness that trampoline-wraps `storm/ner.py` turns
# into a collection error rather than a test result.
# ---------------------------------------------------------------------------


@needs_ner
class TestBatchDifferentialIdentity:
    """Primary gate: `iter_ner_spans_batch(corpus) == [iter_ner_spans(t) for t
    in corpus]`, element for element, against the REAL installed model."""

    def _corpus(self) -> list[object]:
        astral_emoji = "\U0001f600 Marie Curie visited Zurich."
        nfc = "Café Müller lived in Söderköping."
        nfd = unicodedata.normalize("NFD", nfc)
        curly_apostrophe = "O’Brien traveled to O’Fallon."  # noqa: RUF001 - deliberate test unicode
        nonascii_location = "she moved to São Paulo for work"
        duplicate = "John Smith met John Smith in Boston."
        oversized_adjacent = ("word " * 5000).strip()  # long, still under max_length
        return [
            astral_emoji,
            nfc,
            nfd,
            curly_apostrophe,
            nonascii_location,
            duplicate,
            duplicate,
            "",
            "   ",
            12345,
            oversized_adjacent,
            "plain text with no entities at all",
        ]

    def test_differential_identity_over_unicode_corpus(self) -> None:
        from decoy_engine.storm.ner import iter_ner_spans, iter_ner_spans_batch

        corpus = self._corpus()
        assert iter_ner_spans_batch(corpus) == [iter_ner_spans(t) for t in corpus]

    def test_differential_identity_with_entities_filter(self) -> None:
        from decoy_engine.storm.ner import iter_ner_spans, iter_ner_spans_batch

        corpus = self._corpus()
        assert iter_ner_spans_batch(corpus, entities=["location"]) == [
            iter_ner_spans(t, entities=["location"]) for t in corpus
        ]

    def test_all_empty_batch(self) -> None:
        from decoy_engine.storm.ner import iter_ner_spans_batch

        assert iter_ner_spans_batch([]) == []
        assert iter_ner_spans_batch(["", "   " * 0, None]) == [[], [], []]


@needs_ner
class TestBatchSizeInvariance:
    def test_batch_size_invariant_over_a_mixed_corpus(self) -> None:
        from decoy_engine.storm.ner import iter_ner_spans_batch

        corpus = [
            "Marie Curie visited Paris.",
            "no entities in this filler row",
            "",
            "   ",
            "John Smith flew to São Paulo.",
        ] * 3
        results = {bs: iter_ner_spans_batch(corpus, batch_size=bs) for bs in (1, 3, 256)}
        assert results[1] == results[3] == results[256]


@needs_ner
class TestMaxLengthRaiseParity:
    def test_oversized_cell_raises_same_error_class_from_batch_helper(self) -> None:
        from decoy_engine.storm.ner import (
            DEFAULT_NER_MODEL,
            _pipeline,
            iter_ner_spans,
            iter_ner_spans_batch,
        )

        nlp = _pipeline(DEFAULT_NER_MODEL)
        oversized = "a" * (nlp.max_length + 1)
        with pytest.raises(ValueError) as single_exc:
            iter_ner_spans(oversized)
        with pytest.raises(ValueError) as batch_exc:
            iter_ner_spans_batch([oversized])
        assert type(single_exc.value) is type(batch_exc.value)


@needs_ner
class TestPeakMemoryCharacterization:
    """P2: a wide column processed in `_NER_APPLY_WINDOW`-sized windows (the
    oracle caller's actual pattern, see `_text_mask.py` / `_text_redact.py`)
    keeps peak memory close to ONE window's footprint, the same bounded
    profile a per-cell loop has, rather than growing with the column's total
    width. A characterization test (generous headroom), not a tight
    regression trip-wire -- `nlp.pipe`'s own internal batch buffering makes a
    tight per-call comparison against `iter_ner_spans` noisy and misleading."""

    def test_windowed_apply_bounds_peak_memory_independent_of_column_width(self) -> None:
        import tracemalloc

        from decoy_engine.storm.ner import _NER_APPLY_WINDOW, iter_ner_spans_batch

        def _peak_processing_windowed(n_rows: int) -> int:
            texts = [f"plain filler row {i} with no entities" for i in range(n_rows)]
            tracemalloc.start()
            for start in range(0, len(texts), _NER_APPLY_WINDOW):
                iter_ner_spans_batch(texts[start : start + _NER_APPLY_WINDOW])
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return peak

        one_window_peak = _peak_processing_windowed(_NER_APPLY_WINDOW)
        four_windows_peak = _peak_processing_windowed(_NER_APPLY_WINDOW * 4)
        # Each window's span lists are discarded before the next window
        # starts, so processing 4x the rows through 4x the windows must not
        # cost ~4x the peak: that would mean the window bound isn't doing its
        # job and peak RSS is tracking the WHOLE column instead of one window.
        assert four_windows_peak <= one_window_peak * 2 + 1_000_000
