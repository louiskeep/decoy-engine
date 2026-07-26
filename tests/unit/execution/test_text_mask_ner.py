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
from decoy_engine.storm.ner import (
    DEFAULT_NER_MODEL,
    installed_model_version,
    model_installed,
    spacy_installed,
)

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


# ── TX-2: the seed envelope stamps ner_model_version for text_mask too ──


class TestTextMaskNerModelVersionStamp:
    """_seed_envelope.py stamps `ner_model_version` for text_mask `ner`
    columns (was text_redact-only). Without a populated stamp, the handler's
    drift guard (`_text_mask.py`, `if ner_model and plan.ner_model_version`)
    is a silent no-op and a model upgrade would pass unnoticed. Mirrors
    test_ner_spans.py::TestModelVersionStamp for text_redact."""

    def _build_plan(self, provider_config: dict, monkeypatch, *, stub_version: str | None):
        import pandas as pd
        import pyarrow as pa

        from decoy_engine.execution._chunked_profile import first_chunk_profile
        from decoy_engine.plan import _compile as compile_mod
        from decoy_engine.plan import compile_plan

        if stub_version is not None:
            monkeypatch.setattr(
                "decoy_engine.storm.ner.installed_model_version", lambda model: stub_version
            )
        # Row 30 hard-fails when the model is absent; bypass it for the stamp
        # test (the stamp is independent of availability, exactly as the
        # text_redact row-13 stamp test does).
        monkeypatch.setattr(compile_mod, "check_text_mask_ner_available", lambda config: None)
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "notes",
                            "strategy": "text_mask",
                            "provider_config": provider_config,
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
        plan = compile_plan(cfg, profile, decoy_engine_version="x", no_profile=True)
        return dict(plan.seed_envelope.per_table[0][1].per_column)

    def test_text_mask_ner_column_carries_stubbed_version(self, monkeypatch) -> None:
        per_column = self._build_plan({"ner": True}, monkeypatch, stub_version="9.9.9")
        assert per_column["notes"].ner_model_version == "9.9.9"
        # A non-ner sibling column is never stamped.
        assert per_column["email"].ner_model_version is None

    @needs_ner
    def test_text_mask_ner_column_carries_real_installed_version(self, monkeypatch) -> None:
        # No stub: assert the stamp is populated from the REAL installed model
        # metadata (non-None) and matches installed_model_version -- proving the
        # end-to-end stamp works, not just the branch under a stub.
        per_column = self._build_plan({"ner": True}, monkeypatch, stub_version=None)
        stamped = per_column["notes"].ner_model_version
        assert stamped is not None
        assert stamped == installed_model_version(DEFAULT_NER_MODEL)


# ── TQ crown-jewels: mutation hardening of the non-NER handler layer ────
#
# Grades the non-NER layer of `_text_mask.py` (config resolution, cell
# iteration, output-Series assembly). Every test drives the handler with NO
# `ner` config, so no spaCy pipeline is needed. The NER resolution/guard/iter
# path is deferred to the NER-enabled env; see
# docs/quality/mutation-ledgers/execution_strategies_text_mask.md.


class _CaptureMask:
    """Spy for the module-level `mask_cell`; records forwarded args/kwargs."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "<masked>"


def _run_capture(monkeypatch, provider_config: dict, values: list) -> _CaptureMask:
    spy = _CaptureMask()
    monkeypatch.setattr("decoy_engine.execution._strategies._text_mask.mask_cell", spy)
    df = pd.DataFrame({"notes": values})
    TextMaskHandler().run(df, "notes", _seed(provider_config), _FakeCtx())
    return spy


class TestArgForwarding:
    """Config -> `mask_cell` keyword forwarding. Asserting a forwarded kwarg
    kills both the value mutants (wrong key/default/case) and the kwarg-drop
    mutants: a dropped kwarg is absent from `**kwargs`, so the assertion raises
    KeyError."""

    def test_forwards_detector_list(self, monkeypatch) -> None:
        # detectors=["email"] must reach detector_ids verbatim, not None / [""] /
        # ["None"] from a broken resolution nor a dropped kwarg.
        spy = _run_capture(monkeypatch, {"detectors": ["email"]}, ["hello"])
        assert spy.calls[0][1]["detector_ids"] == ["email"]

    def test_absent_detectors_forwards_none(self, monkeypatch) -> None:
        # No `detectors` config -> detector_ids resolves to None (run all built-in
        # detectors), never "" from a broken else branch. The list-branch test
        # above does not exercise this path.
        spy = _run_capture(monkeypatch, {}, ["hello"])
        assert spy.calls[0][1]["detector_ids"] is None

    def test_forwards_per_detector_strategy_map(self, monkeypatch) -> None:
        spy = _run_capture(monkeypatch, {"per_detector_strategy": {"email": "redact"}}, ["hello"])
        assert spy.calls[0][1]["strategy_map"] == {"email": "redact"}

    def test_forwards_default_unmatched_policy(self, monkeypatch) -> None:
        # Absent config must default the policy to exactly "redact".
        spy = _run_capture(monkeypatch, {}, ["hello"])
        assert spy.calls[0][1]["unmatched_span_policy"] == "redact"

    def test_forwards_default_token(self, monkeypatch) -> None:
        spy = _run_capture(monkeypatch, {}, ["hello"])
        assert spy.calls[0][1]["token"] == "[REDACTED]"

    def test_forwards_custom_token(self, monkeypatch) -> None:
        # A configured token must win; a mutant reading the wrong key would fall
        # back to the default instead.
        spy = _run_capture(monkeypatch, {"token": "CUSTOM_TOK"}, ["hello"])
        assert spy.calls[0][1]["token"] == "CUSTOM_TOK"

    def test_forwards_date_bounds_as_cfg(self, monkeypatch) -> None:
        # min_days/max_days pass through as the cfg extra dict; a mutated key or
        # a None extra dict would drop or corrupt them.
        spy = _run_capture(monkeypatch, {"min_days": -10, "max_days": 20}, ["hello"])
        assert spy.calls[0][1]["cfg"] == {"min_days": -10, "max_days": 20}

    def test_extra_spans_none_without_ner(self, monkeypatch) -> None:
        # No `ner` config -> extra_spans forwarded as None, never "".
        spy = _run_capture(monkeypatch, {}, ["hello"])
        assert spy.calls[0][1]["extra_spans"] is None


class TestCellIteration:
    def test_coerces_non_str_cell_to_str(self, monkeypatch) -> None:
        # A non-string cell must reach mask_cell as str(value) ("123"), not the
        # raw int, None, or the literal "None".
        spy = _run_capture(monkeypatch, {}, [123])
        assert spy.calls[0][0][0] == "123"

    def test_null_cell_is_skipped_and_later_cells_still_masked(self) -> None:
        # A leading null must `continue`, not `break`: the SSN one row later must
        # still be masked.
        df = pd.DataFrame({"notes": [None, "ssn 123-45-6789"]})
        out, _ = TextMaskHandler().run(df, "notes", _seed({}), _FakeCtx())
        assert pd.isna(out["notes"].iloc[0])
        assert "123-45-6789" not in out["notes"].iloc[1]

    def test_extension_dtype_column_masks_without_error(self) -> None:
        # An extension-array column (pandas "string") must be boxed to object so
        # isna()/to_list() and the per-cell mask run; col=None or astype(None)
        # would raise here.
        df = pd.DataFrame({"notes": pd.array(["ssn 123-45-6789", None], dtype="string")})
        out, _ = TextMaskHandler().run(df, "notes", _seed({}), _FakeCtx())
        assert "123-45-6789" not in out["notes"].iloc[0]
        assert pd.isna(out["notes"].iloc[1])


class TestOutputSeries:
    def test_output_series_aligned_to_non_default_index(self) -> None:
        # The output Series must carry df.index; a RangeIndex (index=None or the
        # arg dropped) misaligns against a non-default index and blanks to NaN.
        df = pd.DataFrame({"notes": ["ssn 123-45-6789", "plain"]}, index=[10, 20])
        out, _ = TextMaskHandler().run(df, "notes", _seed({}), _FakeCtx())
        assert not out["notes"].isna().any()
        assert "123-45-6789" not in out["notes"].loc[10]
