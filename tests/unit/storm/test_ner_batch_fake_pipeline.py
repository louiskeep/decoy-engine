"""Phase 5 (docs/plans/2026-09-08-p5-ner-batch-helper.md): spaCy-free tests for
`iter_ner_spans_batch`'s own logic (positivity/guard/cardinality/scatter/filter
branches), via a FAKE `_pipeline` -- no `spacy_installed()`/`model_installed()`
call at import time, unlike `test_ner_spans.py`'s `needs_ner` line, so this
module collects and runs cleanly with spaCy absent AND under a mutation harness
that trampoline-wraps every function in `storm/ner.py` (a module-level call to
a mutated function during collection reports as a pytest collection error, not
a test failure -- keeping these tests in their own import-time-clean module is
what lets `iter_ner_spans_batch` be graded in isolation).

`iter_ner_spans`'s single-cell body is untouched by Phase 5 (it stays the
independent oracle `test_ner_spans.py`'s differential test compares against);
this module never reuses it either, so a mapping/offset/filter bug in
`iter_ner_spans_batch` cannot hide behind a shared extractor.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.storm.detectors import Span


class _FakeEnt:
    """Stand-in for a spaCy `Span` (the entity, not this module's `Span`)."""

    def __init__(self, label_: str, start_char: int, end_char: int, text: str) -> None:
        self.label_ = label_
        self.start_char = start_char
        self.end_char = end_char
        self.text = text


class _FakeDoc:
    def __init__(self, ents: list[_FakeEnt]) -> None:
        self.ents = ents


class _FakePipeline:
    """Stub for `_pipeline(model)`: serves both `nlp(text)` (single-cell) and
    `nlp.pipe(texts, ...)` (batched) from the same `docs_by_text` map, records
    every `.pipe` call's arguments, and can simulate a doc-count mismatch or a
    mid-stream raise -- so a single fake proves single-cell/batched parity
    rather than risking two fakes silently drifting apart."""

    def __init__(
        self,
        docs_by_text: dict[str, _FakeDoc],
        *,
        raise_on: str | None = None,
        extra_docs: int = 0,
        missing_docs: int = 0,
    ) -> None:
        self._docs_by_text = docs_by_text
        self._raise_on = raise_on
        self._extra_docs = extra_docs
        self._missing_docs = missing_docs
        self.pipe_calls: list[dict[str, object]] = []

    def __call__(self, text: str) -> _FakeDoc:
        if self._raise_on is not None and text == self._raise_on:
            raise ValueError(f"simulated oversized cell ({len(text)} chars)")
        return self._docs_by_text.get(text, _FakeDoc([]))

    def pipe(self, texts: list[str], *, batch_size: int, n_process: int):
        texts = list(texts)
        self.pipe_calls.append({"texts": texts, "batch_size": batch_size, "n_process": n_process})
        emit = texts[: len(texts) - self._missing_docs] if self._missing_docs else texts
        for t in emit:
            if self._raise_on is not None and t == self._raise_on:
                raise ValueError(f"simulated oversized cell ({len(t)} chars)")
            yield self._docs_by_text.get(t, _FakeDoc([]))
        for _ in range(self._extra_docs):
            yield _FakeDoc([])


def _install_fake_pipeline(monkeypatch, fake: _FakePipeline) -> None:
    import decoy_engine.storm.ner as ner_mod

    monkeypatch.setattr(ner_mod, "_pipeline", lambda model=None: fake)


class TestCharacterizationGolden:
    """Spacy-free golden pinning the batch helper's extraction independently
    of `iter_ner_spans` (P1, breaks common-mode): entity order, label mapping,
    unmapped-label exclusion, exact offsets/matched_text, detector filtering."""

    _TEXT_A = "Marie Curie flew to Paris."
    _TEXT_B = "Acme Tower hosted Twitter Inc. on July 4."
    _TEXT_C = "Klaus works near Berlin Hbf."

    def _docs(self) -> dict[str, _FakeDoc]:
        return {
            self._TEXT_A: _FakeDoc(
                [
                    _FakeEnt("PERSON", 0, 11, "Marie Curie"),
                    _FakeEnt("GPE", 20, 25, "Paris"),
                ]
            ),
            self._TEXT_B: _FakeDoc(
                [
                    _FakeEnt("FAC", 0, 10, "Acme Tower"),
                    _FakeEnt("ORG", 18, 30, "Twitter Inc."),  # unmapped -> excluded
                    _FakeEnt("DATE", 34, 40, "July 4"),  # unmapped -> excluded
                ]
            ),
            self._TEXT_C: _FakeDoc(
                [
                    _FakeEnt("PER", 0, 5, "Klaus"),  # WikiNER scheme -> person_name too
                    _FakeEnt("LOC", 17, 27, "Berlin Hbf"),
                ]
            ),
        }

    def test_exact_spans_order_mapping_and_unmapped_exclusion(self, monkeypatch) -> None:
        from decoy_engine.storm.ner import iter_ner_spans_batch

        _install_fake_pipeline(monkeypatch, _FakePipeline(self._docs()))
        result = iter_ner_spans_batch([self._TEXT_A, self._TEXT_B, self._TEXT_C])
        assert result == [
            [
                Span("person_name", 0, 11, "Marie Curie"),
                Span("location", 20, 25, "Paris"),
            ],
            [
                Span("location", 0, 10, "Acme Tower"),
            ],
            [
                Span("person_name", 0, 5, "Klaus"),
                Span("location", 17, 27, "Berlin Hbf"),
            ],
        ]

    def test_entities_filter_by_detector_id(self, monkeypatch) -> None:
        from decoy_engine.storm.ner import iter_ner_spans_batch

        _install_fake_pipeline(monkeypatch, _FakePipeline(self._docs()))
        only_location = iter_ner_spans_batch([self._TEXT_A], entities=["location"])
        assert only_location == [[Span("location", 20, 25, "Paris")]]
        only_person = iter_ner_spans_batch([self._TEXT_A], entities=["person_name"])
        assert only_person == [[Span("person_name", 0, 11, "Marie Curie")]]

    def test_entities_empty_list_yields_no_spans(self, monkeypatch) -> None:
        from decoy_engine.storm.ner import iter_ner_spans_batch

        _install_fake_pipeline(monkeypatch, _FakePipeline(self._docs()))
        assert iter_ner_spans_batch([self._TEXT_A], entities=[]) == [[]]

    def test_entities_spacy_label_is_not_a_detector_id_yields_no_spans(self, monkeypatch) -> None:
        # "GPE" is the spaCy ENTITY LABEL, not the detector id ("location");
        # `entities` filters by detector id, so this must yield nothing.
        from decoy_engine.storm.ner import iter_ner_spans_batch

        _install_fake_pipeline(monkeypatch, _FakePipeline(self._docs()))
        assert iter_ner_spans_batch([self._TEXT_A], entities=["GPE"]) == [[]]


class TestWhitespaceEntersThePipe:
    """P0.1: only non-str and `""` are guarded out; whitespace-only text is
    non-empty and must reach `.pipe` exactly as it reaches `nlp()` today."""

    def test_whitespace_reaches_pipe_empty_and_nonstr_do_not(self, monkeypatch) -> None:
        from decoy_engine.storm.ner import iter_ner_spans_batch

        fake = _FakePipeline({})
        _install_fake_pipeline(monkeypatch, fake)
        result = iter_ner_spans_batch(["   ", "", None, 42, "hello"])
        assert fake.pipe_calls, "pipe was never called"
        seen_texts = fake.pipe_calls[0]["texts"]
        assert seen_texts == ["   ", "hello"]
        assert result[1] == []  # ""
        assert result[2] == []  # None
        assert result[3] == []  # 42

    def test_oversized_whitespace_raises_same_error_class_both_paths(self, monkeypatch) -> None:
        from decoy_engine.storm.ner import iter_ner_spans, iter_ner_spans_batch

        oversized_ws = " " * 5  # stands in for a whitespace cell over max_length
        fake = _FakePipeline({}, raise_on=oversized_ws)
        _install_fake_pipeline(monkeypatch, fake)

        with pytest.raises(ValueError) as single_exc:
            iter_ner_spans(oversized_ws)
        with pytest.raises(ValueError) as batch_exc:
            iter_ner_spans_batch([oversized_ws])
        assert str(single_exc.value) == str(batch_exc.value)


class TestBatchSizeAndCardinalityGuard:
    """P0.2: `batch_size<=0` must raise before any pipeline access (so this
    runs with no spacy installed); a fake `.pipe` yielding the wrong doc count
    must raise too, never degrade to an empty/truncated result."""

    @pytest.mark.parametrize("bad_batch_size", [0, -1])
    def test_non_positive_batch_size_raises_value_error(self, bad_batch_size: int) -> None:
        from decoy_engine.storm.ner import iter_ner_spans_batch

        # Assert the message, not just "some ValueError": a mutant that
        # relaxes the boundary to `< 0` would let `batch_size=0` fall through
        # to `.pipe`, which either raises a DIFFERENT error or (the P0 fear)
        # yields zero docs and trips the cardinality guard instead -- neither
        # carries this message, so a bare `pytest.raises(ValueError)` would
        # not tell the two apart.
        with pytest.raises(ValueError) as exc:
            iter_ner_spans_batch(["hello"], batch_size=bad_batch_size)
        assert "requires a positive batch_size" in str(exc.value)
        assert repr(bad_batch_size) in str(exc.value)

    def test_batch_size_one_is_valid_and_does_not_raise(self, monkeypatch) -> None:
        # The boundary is `<= 0`, not `<= 1`: batch_size=1 is the smallest
        # VALID value and must not raise.
        from decoy_engine.storm.ner import iter_ner_spans_batch

        fake = _FakePipeline({})
        _install_fake_pipeline(monkeypatch, fake)
        assert iter_ner_spans_batch(["hello"], batch_size=1) == [[]]

    def test_fewer_docs_than_inputs_raises_never_empty(self, monkeypatch) -> None:
        from decoy_engine.storm.ner import iter_ner_spans_batch

        _install_fake_pipeline(monkeypatch, _FakePipeline({}, missing_docs=1))
        with pytest.raises(ValueError):
            iter_ner_spans_batch(["a", "b", "c"])

    def test_more_docs_than_inputs_raises_never_empty(self, monkeypatch) -> None:
        from decoy_engine.storm.ner import iter_ner_spans_batch

        _install_fake_pipeline(monkeypatch, _FakePipeline({}, extra_docs=1))
        with pytest.raises(ValueError):
            iter_ner_spans_batch(["a", "b", "c"])

    def test_all_empty_batch_short_circuits_without_a_pipe_call(self, monkeypatch) -> None:
        from decoy_engine.storm.ner import iter_ner_spans_batch

        fake = _FakePipeline({})
        _install_fake_pipeline(monkeypatch, fake)
        assert iter_ner_spans_batch([]) == []
        assert iter_ner_spans_batch(["", None, ""]) == [[], [], []]
        assert not fake.pipe_calls


class TestScatterMixedPositions:
    def test_mixed_positions_scatter_to_guarded_out_and_correct_spans(self, monkeypatch) -> None:
        from decoy_engine.storm.ner import iter_ner_spans_batch

        entity_text = "ZQMARK"
        dup_text = "DUPTXT"
        docs_by_text = {
            entity_text: _FakeDoc([_FakeEnt("PERSON", 0, 6, entity_text)]),
            dup_text: _FakeDoc([_FakeEnt("GPE", 0, 6, dup_text)]),
        }
        fake = _FakePipeline(docs_by_text)
        _install_fake_pipeline(monkeypatch, fake)

        inputs = [None, 42, "", entity_text, pd.NA, dup_text, dup_text]
        result = iter_ner_spans_batch(inputs)
        assert result[0] == []  # None
        assert result[1] == []  # non-str
        assert result[2] == []  # ""
        assert result[3] == [Span("person_name", 0, 6, entity_text)]
        assert result[4] == []  # pd.NA
        assert result[5] == [Span("location", 0, 6, dup_text)]
        assert result[6] == [Span("location", 0, 6, dup_text)]  # duplicate, independent

        # Coercion once: pipe saw exactly the passing texts, in order, each
        # exactly as given -- not re-stringified, not deduplicated away.
        assert fake.pipe_calls[0]["texts"] == [entity_text, dup_text, dup_text]


class TestPipeArgForwarding:
    """The resolved `model` and the caller's own `batch_size` -- plus the
    hard-coded `n_process=1` (no multiprocessing, per the plan) -- must reach
    `_pipeline`/`.pipe` verbatim. A dropped arg has no visible symptom in the
    other tests (they all use the default model/batch_size and a fake that
    ignores its own inputs), so it needs a dedicated forwarding check."""

    def test_model_batch_size_and_n_process_forwarded_verbatim(self, monkeypatch) -> None:
        import decoy_engine.storm.ner as ner_mod
        from decoy_engine.storm.ner import iter_ner_spans_batch

        seen_model: list[object] = []
        fake = _FakePipeline({})

        def _pipeline_spy(model=None):
            seen_model.append(model)
            return fake

        monkeypatch.setattr(ner_mod, "_pipeline", _pipeline_spy)
        iter_ner_spans_batch(["hello"], model="custom_model_xyz", batch_size=7)
        assert seen_model == ["custom_model_xyz"]
        assert fake.pipe_calls[0]["batch_size"] == 7
        assert fake.pipe_calls[0]["n_process"] == 1
