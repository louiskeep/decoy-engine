"""TX-2 (2026-07-20): NER wired into `text_mask`.

Mirrors `tests/unit/execution/test_text_redact.py`'s NER cells (WS2/TX-1) for
the `text_mask` handler: `ner: true` or `ner: {model, entities}` routes NER
spans (person_name, location) through `mask_cell(..., extra_spans=...)` so
they SYNTHESIZE (faker) instead of being blunt-redacted, the same fail-closed
`ner_model_version_mismatch` drift guard as `text_redact`, and the compile-
time availability check (`check_text_mask_ner_available`, row #30).
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._text_mask import TextMaskHandler
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.storm.detectors import Span
from decoy_engine.storm.ner import DEFAULT_NER_MODEL, model_installed, spacy_installed

needs_ner = pytest.mark.skipif(
    not (spacy_installed() and model_installed(DEFAULT_NER_MODEL)),
    reason="spacy or en_core_web_sm not installed (ner extra)",
)


def _seed(provider_config: dict, *, ner_model_version: str | None = None) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="text_mask",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=tuple(sorted(provider_config.items())),
        ner_model_version=ner_model_version,
    )


class _FakeCtx:
    def __init__(self, mask_key: bytes = b"\xab" * 32) -> None:
        self.mask_key = mask_key


# ── routing: NER spans reach mask_cell's extra_spans ──────────────────


class TestNerRouting:
    def test_text_mask_routes_ner_person_to_faker(self, monkeypatch) -> None:
        text = "Call Jane Doe today."
        fake_span = [
            Span("person_name", text.index("Jane Doe"), text.index("Jane Doe") + 8, "Jane Doe")
        ]
        monkeypatch.setattr(
            "decoy_engine.storm.ner.iter_ner_spans",
            lambda *a, **k: fake_span,
        )
        df = pd.DataFrame({"notes": [text]})
        handler = TextMaskHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({"ner": True}), _FakeCtx())
        cell = out["notes"].iloc[0]
        assert "Jane Doe" not in cell
        # unmatched_span_policy defaults to redact -- the synthesized name is
        # the only thing NOT necessarily a "[REDACTED]" token; the point of
        # this cell is that the name is GONE, not present verbatim.

    def test_text_mask_routes_ner_location_to_faker_not_redact(self, monkeypatch) -> None:
        text = "Seen in Chicago last week."
        fake_span = [Span("location", text.index("Chicago"), text.index("Chicago") + 7, "Chicago")]
        monkeypatch.setattr(
            "decoy_engine.storm.ner.iter_ner_spans",
            lambda *a, **k: fake_span,
        )
        df = pd.DataFrame({"notes": [text]})
        handler = TextMaskHandler()
        out, _ = handler.run(
            df.copy(),
            "notes",
            _seed({"ner": True, "unmatched_span_policy": "passthrough"}),
            _FakeCtx(),
        )
        cell = out["notes"].iloc[0]
        assert "Chicago" not in cell
        assert "[REDACTED" not in cell
        assert cell.startswith("Seen in ") and cell.endswith(" last week.")

    def test_no_ner_config_never_calls_iter_ner_spans(self, monkeypatch) -> None:
        calls: list[object] = []
        monkeypatch.setattr(
            "decoy_engine.storm.ner.iter_ner_spans",
            lambda *a, **k: calls.append(1) or [],
        )
        df = pd.DataFrame({"notes": ["ssn 123-45-6789 on file"]})
        handler = TextMaskHandler()
        handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert not calls

    def test_ner_dict_config_resolves_model_and_entities(self, monkeypatch) -> None:
        seen: dict[str, object] = {}

        def _fake_iter_ner_spans(text, *, model=None, entities=None):
            seen["model"] = model
            seen["entities"] = entities
            return []

        monkeypatch.setattr(
            "decoy_engine.storm.ner.iter_ner_spans",
            _fake_iter_ner_spans,
        )
        df = pd.DataFrame({"notes": ["hello"]})
        handler = TextMaskHandler()
        handler.run(
            df.copy(),
            "notes",
            _seed({"ner": {"model": "en_core_web_sm", "entities": ["person_name"]}}),
            _FakeCtx(),
        )
        assert seen["model"] == "en_core_web_sm"
        assert seen["entities"] == ["person_name"]


# ── determinism: model-version drift guard ─────────────────────────────


class TestNerVersionGuard:
    """Mirrors text_redact's F14b guard: a spaCy model update between plan
    compile and run must fail closed rather than silently change which
    spans mask_cell synthesizes for the same config + seed."""

    def test_version_mismatch_raises(self, monkeypatch) -> None:
        import decoy_engine.storm.ner as ner_mod

        monkeypatch.setattr(ner_mod, "installed_model_version", lambda model=None: "2.0.0")
        df = pd.DataFrame({"notes": ["Contact Jane Doe"]})
        handler = TextMaskHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(), "notes", _seed({"ner": True}, ner_model_version="1.0.0"), _FakeCtx()
            )
        assert exc.value.code == "ner_model_version_mismatch"

    def test_version_match_does_not_fire_guard(self, monkeypatch) -> None:
        import decoy_engine.storm.ner as ner_mod

        monkeypatch.setattr(ner_mod, "installed_model_version", lambda model=None: "1.0.0")
        monkeypatch.setattr(
            "decoy_engine.storm.ner.iter_ner_spans",
            lambda *a, **k: [],
        )
        df = pd.DataFrame({"notes": ["ssn 123-45-6789"]})
        handler = TextMaskHandler()
        out, _ = handler.run(
            df.copy(), "notes", _seed({"ner": True}, ner_model_version="1.0.0"), _FakeCtx()
        )
        # Guard did not fire; the regex ssn detector still masks.
        assert "123-45-6789" not in out["notes"].iloc[0]

    def test_no_stamped_version_skips_guard(self, monkeypatch) -> None:
        import decoy_engine.storm.ner as ner_mod

        monkeypatch.setattr(ner_mod, "installed_model_version", lambda model=None: "9.9.9")
        monkeypatch.setattr(
            "decoy_engine.storm.ner.iter_ner_spans",
            lambda *a, **k: [],
        )
        df = pd.DataFrame({"notes": ["ssn 123-45-6789"]})
        handler = TextMaskHandler()
        out, _ = handler.run(
            df.copy(), "notes", _seed({"ner": True}, ner_model_version=None), _FakeCtx()
        )
        assert "123-45-6789" not in out["notes"].iloc[0]


# ── compile-time availability check (row #30) ──────────────────────────


class TestCompileTimeNerAvailability:
    def _cfg(self, provider_config: dict) -> dict:
        return {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "notes",
                            "strategy": "text_mask",
                            "provider_config": provider_config,
                        }
                    ],
                }
            ],
        }

    def test_text_mask_ner_missing_spacy_fails_at_compile(self, monkeypatch) -> None:
        from decoy_engine.plan._checks import check_text_mask_ner_available

        monkeypatch.setattr("decoy_engine.storm.ner.spacy_installed", lambda: False)
        with pytest.raises(PlanCompileError) as exc:
            check_text_mask_ner_available(self._cfg({"ner": True}))
        assert exc.value.code == "ner_spacy_not_installed"

    def test_missing_model_rejected_config_only(self) -> None:
        from decoy_engine import run_config_only_checks

        cfg = self._cfg({"ner": {"model": "xx_no_such_model"}})
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code in ("ner_model_not_installed", "ner_spacy_not_installed")

    def test_no_ner_key_passes_everywhere(self) -> None:
        from decoy_engine import run_config_only_checks

        names = run_config_only_checks(self._cfg({"detectors": ["email"]}))
        assert "text_mask_ner_available" in names

    @needs_ner
    def test_ner_true_passes_when_installed(self) -> None:
        from decoy_engine import run_config_only_checks

        names = run_config_only_checks(self._cfg({"ner": True}))
        assert "text_mask_ner_available" in names


# ── TX-2: end-to-end smoke test on the real model ───────────────────────


@needs_ner
def test_text_mask_ner_synthesizes_person_name_and_location() -> None:
    """Proves `ner: true` end to end against the real installed spaCy model:
    person names AND locations synthesize (faker) rather than being redacted
    to a bare token -- the behavior that distinguishes text_mask from
    text_redact's NER path."""
    df = pd.DataFrame({"notes": ["Contact Jane Doe in Boston about the claim."]})
    handler = TextMaskHandler()
    out, _ = handler.run(
        df.copy(),
        "notes",
        _seed({"ner": True, "unmatched_span_policy": "passthrough"}),
        _FakeCtx(),
    )
    cell = out["notes"].iloc[0]
    assert "Jane Doe" not in cell
    assert "Boston" not in cell
    assert "[REDACTED" not in cell
    assert cell.startswith("Contact ") and cell.endswith(" about the claim.")
