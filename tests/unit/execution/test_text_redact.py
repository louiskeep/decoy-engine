"""MG-2 Step 2 (2026-05-31): text_redact strategy regression cells.

Locks the cell-level behavior of `TextRedactHandler`:
- Default token replaces every detected span.
- Non-PII text is preserved byte-for-byte around redacted spans.
- `label_token=True` emits per-detector labels.
- Custom token strings work, including metacharacters (literal).
- Null cells pass through.
- Subset detector_ids only redacts the listed detectors.
- Non-string token + non-list detectors fall back to passthrough.
"""

from __future__ import annotations

import pandas as pd
import pytest

import decoy_engine.execution._strategies._text_redact as txr_mod
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._text_redact import TextRedactHandler
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.storm.detectors import Span
from decoy_engine.storm.ner import DEFAULT_NER_MODEL, model_installed, spacy_installed

# TX-1 (2026-07-20): gates the real-model smoke test below on spaCy + the
# default model actually being importable, mirroring the ml/geo extras
# convention (importorskip) and the identical predicate used by
# tests/unit/storm/test_ner_spans.py. SKIPPED in the extras-free CI
# environment; PASSES once `pip install decoy-engine[ner]` + `python -m
# spacy download en_core_web_sm` have run.
needs_ner = pytest.mark.skipif(
    not (spacy_installed() and model_installed(DEFAULT_NER_MODEL)),
    reason="spacy or en_core_web_sm not installed (ner extra)",
)


def _seed(provider_config: dict) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="text_redact",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=tuple(sorted(provider_config.items())),
    )


class _FakeCtx:
    pass


# ── core redaction ────────────────────────────────────────────────────


class TestCoreRedaction:
    def test_text_redact_replaces_all_spans_with_default_token(self):
        df = pd.DataFrame({"notes": ["Contact alice@example.com, SSN 123-45-6789."]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        cell = out["notes"].iloc[0]
        assert "alice@example.com" not in cell
        assert "123-45-6789" not in cell
        assert cell.count("[REDACTED]") == 2

    def test_all_null_float_column_output_is_object_dtype(self):
        # The output column is typed object deliberately (the contract at the
        # Arrow/concat boundary). An all-null FLOAT column would infer float64
        # without the explicit dtype=object; pin object so a dtype-drop mutant
        # fails. Redacted values are identical (all null); only the dtype differs.
        df = pd.DataFrame({"notes": pd.Series([float("nan"), float("nan")])})
        out, _ = TextRedactHandler().run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert out["notes"].dtype == object

    def test_empty_detectors_list_redacts_all_not_nothing(self):
        # S5c F2 anti-PHI-leak invariant: detectors=[] means "run all detectors",
        # never "redact nothing". An empty list must still redact PII.
        df = pd.DataFrame({"notes": ["alice@example.com SSN 123-45-6789"]})
        out, _ = TextRedactHandler().run(df.copy(), "notes", _seed({"detectors": []}), _FakeCtx())
        cell = out["notes"].iloc[0]
        assert "alice@example.com" not in cell
        assert "123-45-6789" not in cell

    def test_text_redact_preserves_non_pii_text_byte_for_byte(self):
        original = "Patient presented with cough. Phone (212) 555-1234. Discharged."
        df = pd.DataFrame({"notes": [original]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        cell = out["notes"].iloc[0]
        # Non-PII chunks preserved verbatim.
        assert cell.startswith("Patient presented with cough. Phone ")
        assert cell.endswith(". Discharged.")
        # The phone span is replaced.
        assert "(212) 555-1234" not in cell

    def test_text_redact_label_token_emits_per_detector_label(self):
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({"label_token": True}), _FakeCtx())
        assert out["notes"].iloc[0] == "[REDACTED:email]"

    def test_text_redact_custom_token_string(self):
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({"token": "<PHI>"}), _FakeCtx())
        assert out["notes"].iloc[0] == "<PHI>"


# ── null + empty + no-match ───────────────────────────────────────────


class TestNullAndEmpty:
    def test_text_redact_null_cell_stays_null(self):
        df = pd.DataFrame({"notes": ["alice@example.com", None, "no pii"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert out["notes"].iloc[0] == "[REDACTED]"
        assert pd.isna(out["notes"].iloc[1])
        assert out["notes"].iloc[2] == "no pii"

    def test_text_redact_empty_column_no_error(self):
        df = pd.DataFrame({"notes": pd.Series([], dtype=object)})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert len(out) == 0

    def test_text_redact_column_with_no_matches_passes_through_unchanged(self):
        df = pd.DataFrame({"notes": ["just prose", "no identifiers here"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert out["notes"].tolist() == ["just prose", "no identifiers here"]


# ── overlaps + detector selection ─────────────────────────────────────


class TestOverlapAndSelection:
    def test_text_redact_handles_overlapping_matches_per_iter_spans_policy(self):
        # `iter_spans` is responsible for de-overlapping; this test
        # exercises the strategy's `_splice` walking non-overlapping spans
        # (a regression cell that catches a future iter_spans bug from
        # producing overlapping output).
        df = pd.DataFrame({"notes": ["alice@example.com bob@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert out["notes"].iloc[0] == "[REDACTED] [REDACTED]"

    def test_text_redact_subset_detectors_only_redacts_listed(self):
        df = pd.DataFrame({"notes": ["Contact alice@example.com, SSN 123-45-6789."]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({"detectors": ["email"]}), _FakeCtx())
        cell = out["notes"].iloc[0]
        assert "alice@example.com" not in cell
        # SSN was not in the list -> stays.
        assert "123-45-6789" in cell


# ── bad config ────────────────────────────────────────────────────────


class TestBadConfig:
    def test_text_redact_non_string_token_falls_back_to_passthrough(self):
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({"token": 42}), _FakeCtx())
        # Bad config -> unchanged.
        assert out["notes"].iloc[0] == "alice@example.com"

    def test_text_redact_non_list_detectors_falls_back_to_passthrough(self):
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(
            df.copy(),
            "notes",
            _seed({"detectors": "email"}),  # str, not list
            _FakeCtx(),
        )
        assert out["notes"].iloc[0] == "alice@example.com"

    def test_text_redact_token_with_regex_metacharacters_treated_as_literal(self):
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(
            df.copy(),
            "notes",
            _seed({"token": r"<\d+>"}),
            _FakeCtx(),
        )
        # The token is emitted as-is; no regex interpretation.
        assert out["notes"].iloc[0] == r"<\d+>"


# ── pandas null markers (audit H1) ───────────────────────────────────


class TestPandasNullMarkers:
    """Audit H1 (2026-06-12): pd.NA / pd.NaT survived the None/float-nan
    guard, fell through to str(text), and shipped the literal strings
    '<NA>' / 'NaT' into masked output - a null-preservation contract
    break. The guard must catch every pandas null marker."""

    def test_pd_na_in_arrow_string_column_stays_null(self):
        df = pd.DataFrame(
            {"notes": pd.array(["a@x.com", pd.NA, "b@x.com"], dtype="string[pyarrow]")}
        )
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert pd.isna(out["notes"].iloc[1])
        assert not isinstance(out["notes"].iloc[1], str)  # not the literal '<NA>'
        assert "[REDACTED]" in out["notes"].iloc[0]

    def test_pd_nat_in_datetime_column_stays_null(self):
        df = pd.DataFrame({"notes": pd.Series([pd.Timestamp("2026-01-01"), pd.NaT])})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert pd.isna(out["notes"].iloc[1])
        assert not isinstance(out["notes"].iloc[1], str)  # not the literal 'NaT'

    def test_nullable_int_na_stays_null(self):
        df = pd.DataFrame({"notes": pd.array([123456789, None], dtype="Int64")})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert pd.isna(out["notes"].iloc[1])


def _ner_seed(ner_model_version: str | None) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="text_redact",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=(("ner", True),),
        ner_model_version=ner_model_version,
    )


class TestF14bNerVersionGuard:
    """F14b (2026-06-26): text_redact must refuse to run when the installed
    spaCy model version differs from the version stamped into the plan at
    compile -- a silent model update would otherwise change redactions for the
    same config + seed with no error. Tested without spaCy by mocking the
    version lookup (the guard runs before any iter_ner_spans call)."""

    def test_version_mismatch_raises(self, monkeypatch):
        import decoy_engine.storm.ner as ner_mod

        monkeypatch.setattr(ner_mod, "installed_model_version", lambda model=None: "2.0.0")
        df = pd.DataFrame({"notes": ["Contact alice@example.com"]})
        handler = TextRedactHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(df.copy(), "notes", _ner_seed("1.0.0"), _FakeCtx())
        assert exc.value.code == "ner_model_version_mismatch"
        # `.strategy` is a machine field consumers branch on, so pin it too.
        assert exc.value.strategy == "text_redact"

    def test_version_match_does_not_fire_guard(self, monkeypatch):
        # Same installed version as stamped -> guard passes; stub iter_ner_spans
        # (no real spaCy) so the rest of the handler runs without error.
        import decoy_engine.storm.ner as ner_mod

        monkeypatch.setattr(ner_mod, "installed_model_version", lambda model=None: "1.0.0")
        monkeypatch.setattr(ner_mod, "iter_ner_spans", lambda *a, **k: [])
        df = pd.DataFrame({"notes": ["Contact alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _ner_seed("1.0.0"), _FakeCtx())
        # Regex detectors still redact the email; the guard did not fire.
        assert "alice@example.com" not in out["notes"].iloc[0]

    def test_no_stamped_version_skips_guard(self, monkeypatch):
        # A plan with no stamped version (ner_model_version=None) cannot be
        # compared, so the guard is skipped even if a version is installed.
        import decoy_engine.storm.ner as ner_mod

        monkeypatch.setattr(ner_mod, "installed_model_version", lambda model=None: "9.9.9")
        monkeypatch.setattr(ner_mod, "iter_ner_spans", lambda *a, **k: [])
        df = pd.DataFrame({"notes": ["Contact alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _ner_seed(None), _FakeCtx())
        assert "alice@example.com" not in out["notes"].iloc[0]


# ── TQ crown-jewels: loop control, coercion, index, NER forwarding ────


def _seed_v(provider_config: dict, ner_model_version: str | None = None) -> ColumnSeed:
    # Like _seed but also stamps ner_model_version (for the version guard).
    return ColumnSeed(
        namespace=None,
        strategy="text_redact",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=tuple(sorted(provider_config.items())),
        ner_model_version=ner_model_version,
    )


class TestLoopControl:
    """The per-cell loop must not terminate early: a `continue` flipped to a
    `break` on either the null-skip or the no-match branch would leave every
    later cell unredacted (silent PHI leak)."""

    def test_null_cell_does_not_truncate_later_cells(self):
        # A leading null must be skipped, not break the loop -- the later PII
        # cell still has to be redacted.
        df = pd.DataFrame({"notes": [None, "SSN 123-45-6789 on file"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert pd.isna(out["notes"].iloc[0])
        assert "123-45-6789" not in out["notes"].iloc[1]

    def test_no_match_cell_does_not_truncate_later_cells(self):
        # A no-match cell passes through, but the loop must continue to the
        # later PII cell rather than break.
        df = pd.DataFrame({"notes": ["just prose", "alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert out["notes"].iloc[0] == "just prose"
        assert "alice@example.com" not in out["notes"].iloc[1]


class TestNonStringCoercion:
    def test_non_string_non_null_cell_coerced_to_str(self):
        # A non-null, non-str cell must be str()-coerced before scanning;
        # dropping/inverting the coercion leaves the raw int (or None/"None").
        df = pd.DataFrame({"notes": [42]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert out["notes"].iloc[0] == "42"


class TestOutputIndexAlignment:
    def test_output_aligned_to_non_default_index(self):
        # The rebuilt Series must carry the frame's own index; a RangeIndex
        # (index=None / index-arg dropped) misaligns on assignment and blanks
        # every row to NaN.
        df = pd.DataFrame({"notes": ["alice@example.com", "bob@example.com"]}, index=[10, 20])
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert out["notes"].loc[10] == "[REDACTED]"
        assert out["notes"].loc[20] == "[REDACTED]"


class TestExtraSpansForwarding:
    def test_extra_spans_none_without_ner(self, monkeypatch):
        # With no NER config, `extra` stays None and must be forwarded as
        # extra_spans=None (a "" init is invisible in output but wrong).
        captured: dict = {}

        def spans_spy(*args, **kwargs):
            captured["extra_spans"] = kwargs.get("extra_spans")
            return []

        monkeypatch.setattr(txr_mod, "iter_spans", spans_spy)
        df = pd.DataFrame({"notes": ["hello world"]})
        handler = TextRedactHandler()
        handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert captured["extra_spans"] is None


class TestNerConfigResolution:
    """Dict NER config (`ner: {model, entities}`) must resolve and forward the
    exact model + entities to iter_ner_spans. iter_ner_spans is a monkeypatch
    boundary, so this is fully gradeable off-spaCy (see F14b class)."""

    def _run_with_ner_spy(self, monkeypatch, ner_cfg):
        import decoy_engine.storm.ner as ner_mod

        captured: dict = {}

        def ner_spy(*args, **kwargs):
            captured["called"] = True
            captured["text"] = args[0] if args else None
            captured["model"] = kwargs.get("model")
            captured["entities"] = kwargs.get("entities")
            return []

        monkeypatch.setattr(ner_mod, "iter_ner_spans", ner_spy)
        df = pd.DataFrame({"notes": ["Alice went home"]})
        handler = TextRedactHandler()
        handler.run(df.copy(), "notes", _seed_v({"ner": ner_cfg}), _FakeCtx())
        return captured

    def test_ner_dict_model_and_entities_forwarded(self, monkeypatch):
        captured = self._run_with_ner_spy(
            monkeypatch,
            {"model": "custom_test_model", "entities": ["person_name", "location"]},
        )
        assert captured.get("called") is True
        assert captured["model"] == "custom_test_model"
        assert captured["entities"] == ["person_name", "location"]

    def test_ner_dict_without_entities_forwards_none(self, monkeypatch):
        captured = self._run_with_ner_spy(monkeypatch, {"model": "custom_test_model"})
        assert captured["model"] == "custom_test_model"
        assert captured["entities"] is None

    def test_ner_dict_empty_entities_forwards_none(self, monkeypatch):
        # An empty entities list means "all mapped entities", i.e. forward None
        # -- not an empty selection.
        captured = self._run_with_ner_spy(
            monkeypatch, {"model": "custom_test_model", "entities": []}
        )
        assert captured["entities"] is None


class TestNerCallSiteForwarding:
    """NER span result and the iter_spans call-site args: the per-cell text,
    the resolved model/entities, and the NER spans injected as extra_spans must
    all reach their call sites. Killed off-spaCy via the mock boundary."""

    def test_ner_result_and_call_args_forwarded(self, monkeypatch):
        import decoy_engine.storm.ner as ner_mod

        expected_span = Span("person_name", 0, 5, "Alice")
        captured: dict = {}

        def ner_spy(*args, **kwargs):
            captured["ner_text"] = args[0] if args else None
            captured["ner_model"] = kwargs.get("model")
            captured["ner_entities"] = kwargs.get("entities")
            return [expected_span]

        def spans_spy(*args, **kwargs):
            captured["extra_spans"] = kwargs.get("extra_spans")
            return []

        monkeypatch.setattr(ner_mod, "iter_ner_spans", ner_spy)
        monkeypatch.setattr(txr_mod, "iter_spans", spans_spy)
        df = pd.DataFrame({"notes": ["Alice went home"]})
        handler = TextRedactHandler()
        handler.run(
            df.copy(),
            "notes",
            _seed_v({"ner": {"model": "custom_test_model", "entities": ["person_name"]}}),
            _FakeCtx(),
        )
        # iter_ner_spans call-site args.
        assert captured["ner_text"] == "Alice went home"
        assert captured["ner_model"] == "custom_test_model"
        assert captured["ner_entities"] == ["person_name"]
        # The NER spans must flow into iter_spans' overlap resolution.
        assert captured["extra_spans"] == [expected_span]

    def test_version_guard_checks_the_resolved_model(self, monkeypatch):
        # The version lookup must query the RESOLVED model, not None: only the
        # resolved model reports the drifting version that fires the guard.
        import decoy_engine.storm.ner as ner_mod

        monkeypatch.setattr(
            ner_mod,
            "installed_model_version",
            lambda model=None: "2.0.0" if model == "custom_test_model" else None,
        )
        monkeypatch.setattr(ner_mod, "iter_ner_spans", lambda *a, **k: [])
        df = pd.DataFrame({"notes": ["Alice went home"]})
        handler = TextRedactHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(),
                "notes",
                _seed_v({"ner": {"model": "custom_test_model"}}, ner_model_version="1.0.0"),
                _FakeCtx(),
            )
        assert exc.value.code == "ner_model_version_mismatch"


# ── TX-1: end-to-end smoke test on the real model ────────────────────


@needs_ner
def test_text_redact_ner_redacts_person_name_and_location():
    """Proves `ner: true` end to end against the real installed spaCy
    model (not a stub): person names AND locations, which the regex
    catalog has no shape for, are detected and redacted."""
    df = pd.DataFrame({"notes": ["Contact Jane Doe in Boston about the claim."]})
    handler = TextRedactHandler()
    out, _ = handler.run(df.copy(), "notes", _seed({"ner": True}), _FakeCtx())
    cell = out["notes"].iloc[0]
    assert "Jane Doe" not in cell
    assert "Boston" not in cell
