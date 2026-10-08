# C8-iii-d-1 build record (full-table positional keying under `when:`)

Plan: `docs/plans/2026-10-08-c8-iii-d1-fulltable-positions.md` (rev 2, Codex plan gate GO round 2).
Built by Opus 4.8 as the builder (Sonnet rate-limited until 2026-10-10); dennis and Codex gate.

## What changed (production)
- `execution/_positional_keys.py`: new `row_positions(row_offset, n, gate_positions, *, code)`, the single owner of a positional draw's full-table row numbers. Without a gate it is the contiguous `row_offset..row_offset+n-1` (byte-unchanged); under a gate it is `row_offset + gate_positions`. `positional_key_array` delegates to it with an optional `gate_positions`.
- `execution/_exact_int_faker.py` `gated_context`: now sets `gate_positions` on EVERY gated call (was only exact-int Faker columns). `sampling_source` is unchanged (still reads them only for a registered exact source).
- `execution/_strategies/_categorical.py`, `_faker.py`, `_windowed_date.py`: the three positional handlers key from the full-table positions. Each reads `getattr(ctx, "gate_positions", None)` defensively (minimal ctx doubles). Categorical and windowed_date convert each scalar to a Python `int` before `encode_int` / `.to_bytes` (numpy uint64 lacks those); Faker keeps the uint64 array path.
- `transforms/windowed_date.py` `apply_windowed_date`: optional `positions` param; without it, contiguous from `row_offset` (byte-unchanged).
- `execution/_strategies/_nested.py`: the child dispatch clears `gate_positions` (save/restore), so a nested positional child keeps its leaf-ordinal keying and is unaffected by the outer gate.

## Property
Section 2d: a `when:`-selected row's output equals that row's output in an ungated run, where that result exists. Unselected rows stay raw. Nested children and stream strategies are out of scope.

## Tests
- Old pins updated (derived from the ungated baseline, not hand-edited):
  - `test_categorical_seeded_nondet.py`: the two `when`-gate pins now assert full-table-row keying; renamed from `..._match_ordinal...`.
  - `test_faker_positional_nondet.py`: the `when`-gate pin now draws at ordinals {1,3,4} (full-table rows), not {0,1,2}.
- New:
  - categorical `test_when_gate_selected_rows_equal_the_ungated_run` (property 2d directly against a baseline run);
  - categorical `test_when_gate_composes_with_a_nonzero_row_offset` (row_offset + gate positions);
  - windowed_date `TestC8IiiDFullTablePositions`: `positions` select full-table rows; the null-anchor split (a full run with a null anchor raises, a gate excluding it succeeds and matches the full-frame surviving rows).
- The categorical test `_Ctx` became a frozen dataclass so `gated_context`'s `dataclasses.replace` works on it (the real StrategyContext is already a frozen dataclass).
- Still-declined: native/unified/chunked declines for positional strategies under `when:` are unchanged (d-2 lifts them); the existing admission tests still pass.

## Verification
- ruff check + format: clean on all changed files.
- Positional suite (categorical + faker + windowed_date): 146 passed.
- Full 3.11 suite, sentries, testflight, mutation: see the gate section (run after this record).

## Out of scope (d-2, deferred)
Lifting the chunked rejections and native/unified declines so positional strategies run natively under `when:`, reusing this slice's keying as the parity target.
