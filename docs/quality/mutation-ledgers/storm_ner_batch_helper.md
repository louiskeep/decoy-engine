# Mutation grading: `storm/ner.py::iter_ner_spans_batch` -- 91.4% (0 unresolved logic)

Phase 5 slice (`docs/plans/2026-09-08-p5-ner-batch-helper.md`): the batched NER
inference helper `iter_ner_spans_batch` that four callers (oracle text_mask/
text_redact, out-of-core group b/c) switch to instead of one `nlp(text)` call
per cell. `storm/ner.py` also holds `iter_ner_spans` (the unchanged single-cell
oracle), `_pipeline`, `ensure_ner_available`, `spacy_installed`,
`model_installed`, `installed_model_version`, and `NerUnavailableError` --
`[tool.mutmut].only_mutate` targets the whole file (mutmut has no
function-level granularity), so this ledger reports the file's raw tally but
grades and adjudicates ONLY `iter_ner_spans_batch`'s own mutants -- the
positivity/guard/cardinality/scatter/filter branches the plan's verify step
asks for. `iter_ner_spans`, `_pipeline`, and the availability-check functions
are exercised by the real-model suites in `tests/unit/storm/test_ner_spans.py`
and `tests/unit/execution/test_text_mask_ner.py` / `test_text_redact.py`
instead (not this narrow selection), so their mutants are out of scope here,
not a coverage gap.

Graded via `scripts/tq_mutate.py` (mutmut generation + full-selection
subprocess re-adjudication, promoting mutmut's in-process false-timeouts and
false-survivors to their true verdict), selection
`tests/unit/storm/test_ner_batch_fake_pipeline.py` -- a SEPARATE module from
`test_ner_spans.py` on purpose: `test_ner_spans.py` computes `_NER_READY =
spacy_installed() and model_installed(...)` at import time, and calling a
trampoline-wrapped (mutated) function during collection reports as a pytest
COLLECTION ERROR rather than a test result, which trips `tq_mutate.py`'s own
forced-fail harness-soundness check (P1-3) before any mutant is graded. The
fake-pipeline module never touches `spacy_installed`/`model_installed` at
import time (only inside test bodies, via a monkeypatched `_pipeline`), so it
collects cleanly under the trampoline and doubles as the "runs without spaCy"
coverage the plan's acceptance test #11 asks for.

## Numbers

**53/58 = 91.38% of `iter_ner_spans_batch`'s own mutants killed, 0 unresolved
logic. 5 survivors, ALL ACCEPTED NON-CONTRACT** (explanatory-prose casing/`XX`-
wrap mutants inside the `ValueError` message, whose coded fields --
`"requires a positive batch_size"` and `repr(batch_size)` -- are independently
pinned by `test_non_positive_batch_size_raises_value_error`). File-wide raw
tally: 132 total mutants, 59 killed, 73 survived (`iter_ner_spans` 6/26,
`ensure_ner_available` 0/20, `_pipeline` 0/18, `spacy_installed` 0/4,
`NerUnavailableError.__init__` 0/3, `model_installed` 0/2,
`installed_model_version` 0/1 -- all out of THIS selection's scope, graded
elsewhere as described above).

Two fixes closed the first pass's real gaps (12 survivors -> 5, all now
accepted-prose):

- `batch_size <= 0` -> `batch_size < 0` (`mutmut_1`) and `-> batch_size <= 1`
  (`mutmut_2`) both survived a bare `pytest.raises(ValueError)`: `<0` still
  raises for `batch_size=-1` (parametrized) and, for `batch_size=0`, falls
  through to `.pipe(..., batch_size=0)`, whose ZERO-doc yield trips the
  cardinality `zip(strict=True)` guard instead -- a DIFFERENT `ValueError`,
  but still a `ValueError`, so the bare assertion could not tell the two
  guards apart. Fixed by asserting the message
  (`"requires a positive batch_size"` and `repr(bad_batch_size)`) instead of
  just the exception type.
- `f"...got {batch_size!r}: " ...` -> `None` (`mutmut_3`, the whole message
  body replaced) survived for the same reason: `ValueError(None)` is still a
  `ValueError`. Killed by the same message assertion.
- `_pipeline(model)` -> `_pipeline(None)` (`mutmut_26`), `.pipe(...,
  batch_size=batch_size, ...)` -> `batch_size=None` (`mutmut_29`), and
  `n_process=1` -> `n_process=None`/`2` (`mutmut_30`, `mutmut_34`) all
  survived because every OTHER test either uses the default `model`/
  `batch_size` or a fake that ignores its own arguments. Added
  `TestPipeArgForwarding` (spies on `_pipeline` and records `.pipe`'s kwargs)
  asserting the resolved `model`, the caller's own `batch_size`, and the
  hard-coded `n_process=1` all reach their call sites verbatim.

## What is PINNED (so a future edit cannot silently regress it)

- **Positivity guard**: `batch_size <= 0` raises `ValueError` with
  `"requires a positive batch_size"` + `repr(batch_size)` in the message;
  `batch_size == 1` does NOT raise (boundary tested both sides).
- **Cardinality guard**: a fake `.pipe` yielding fewer OR more docs than
  passing texts raises `ValueError`, never returns a truncated/padded result
  (`test_fewer_docs_than_inputs_raises_never_empty` /
  `test_more_docs_than_inputs_raises_never_empty`).
- **Guard predicate**: only non-`str` and `""` are excluded; `"   "` (and any
  other non-empty string) reaches `.pipe` (`TestWhitespaceEntersThePipe`).
- **Label mapping + filtering**: `PERSON`/`PER` -> `person_name`; `GPE`/`LOC`/
  `FAC` -> `location`; `ORG`/`DATE` (unmapped) excluded; `entities=[]` -> no
  spans; `entities=["GPE"]` (a spaCy label, not a detector id) -> no spans
  (`TestCharacterizationGolden`).
- **Scatter**: mixed guarded/entity/duplicate positions land at the correct
  original indices, with exactly the passing texts (in order, each exactly
  once) reaching `.pipe` (`TestScatterMixedPositions`).
- **Arg forwarding**: `model`, the caller's `batch_size`, and `n_process=1`
  reach `_pipeline`/`.pipe` verbatim (`TestPipeArgForwarding`).

## ACCEPTED NON-CONTRACT (5): positivity-error prose casing/padding

All five live inside the `ValueError` raised by the positivity guard, in the
message's SECOND and THIRD f-string segments (the "why" explanation), never
the FIRST segment (`"iter_ner_spans_batch requires a positive batch_size, got
{batch_size!r}: "`) that the test pins. Same policy this codebase's other
ledgers use for free-text prose whose coded fields are independently asserted
(see `execution_chunked_text_mask.md`, `determinism_derive.md`): killable only
via brittle full-message string equality, which buys nothing beyond what the
coded-field assertion already proves.

| Mutant | Mutation |
|---|---|
| `iter_ner_spans_batch__mutmut_4` | wraps `"nlp.pipe(..., batch_size<=0) yields zero documents without raising, which "` in `XX...XX` |
| `iter_ner_spans_batch__mutmut_5` | uppercases that same fragment |
| `iter_ner_spans_batch__mutmut_6` | wraps `"would silently drop every NER span in the batch (fail-open PII leak)."` in `XX...XX` |
| `iter_ner_spans_batch__mutmut_7` | lowercases that same fragment |
| `iter_ner_spans_batch__mutmut_8` | uppercases that same fragment |

## Regenerate

```
# Point [tool.mutmut] at the helper (temporary -- revert after grading):
#   only_mutate = ["src/decoy_engine/storm/ner.py"]
#   pytest_add_cli_args_test_selection = ["tests/unit/storm/test_ner_batch_fake_pipeline.py"]
uv run --frozen --extra dev --extra lint --extra mutation \
    python scripts/tq_mutate.py --run --timeout 60
cat mutants/tq_mutate_report.json
```
