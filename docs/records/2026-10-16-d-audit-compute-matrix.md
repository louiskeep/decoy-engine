Status: record

# D-audit: compute-admission matrix and no-legacy classification

Plan: `docs/plans/2026-10-16-phase-d-d-audit-no-legacy.md`. Tests: `tests/parity/test_phase_d_audit_no_legacy.py`, matrix derivation in `tests/parity/_d_audit_matrix.py`.

The audit covers the masking compute backend, not the bounded orchestration around it. The shared `PandasExecutionAdapter._dispatch_mask_node`, the full-frame oracle, the compiled kernels and `_canonicalize_source` are unchanged. No path was deleted.

## How the matrix is derived

The cell set comes from the constants each route consults, not from observed dispositions: the SEQ native op table and key rule (`_sequential_native._OPS`, `_KEY_STRATEGIES`), the out-of-core support and deferral sets (`out_of_core/_compat.py`), and the native-chunked operator registry plus its FK reroute (`_operator_registry.OPERATORS`, `native/_dispatch.py`). The tests assert the derived matrix exactly, so a new operator or a changed admission rule fails until it is classified.

## Classes

| Class | Meaning |
|---|---|
| compiled Rust kernel | companion kernel owns the compute. text_redact and text_mask are Rust span detection with the splice in Python. |
| compiled index selection | faker admitted payload: values are selected through the compiled index kernel. Only pool construction is Python. |
| named-Arrow kernel | `redact_array`, `truncate_array` (`kernel/_scalar.py`) |
| no-op | passthrough |
| dedicated Python | a named handler or primitive with no Rust or Arrow kernel. Not the generic pandas masking fallback for an admitted kernel cell. |
| excluded | not admitted on that route; native-chunked FK cells and the full-frame oracle |

## Matrix

Sequential route (every scalar strategy runs through its shared handler):

| Strategy | Payload | FK key |
|---|---|---|
| hash, fpe | compiled Rust | compiled Rust |
| categorical, bucket_perturb, date_shift | compiled Rust | dedicated Python (key declined, reporting only) |
| faker | compiled index selection | dedicated Python (key declined) |
| text_redact, text_mask | compiled Rust spans, Python splice | dedicated Python (key declined) |
| redact, truncate | named-Arrow | named-Arrow |
| passthrough | no-op | no-op |
| code_set | dedicated Python | dedicated Python |
| bucketize, shuffle, top_code, formula, derived, derived_aggregate, nested, joint_mask, geo_generalize, grouped_series, windowed_date, group_key | dedicated Python (shared handler, no SEQ native admission) | dedicated Python |

Out-of-core route (payload; FK key surface is hash, redact, truncate, passthrough only):

| Strategy | Payload | FK key |
|---|---|---|
| hash | compiled Rust | compiled Rust |
| fpe, categorical, text_redact, bucket_perturb | compiled Rust | excluded |
| redact, truncate | named-Arrow | named-Arrow |
| passthrough | no-op | no-op |
| text_mask | dedicated Python (`mask_cell` per-cell loop) | excluded |
| code_set (mask mode) | dedicated Python | excluded |
| faker, date_shift, bucketize, geo_generalize, formula, derived, nested, and the rest | excluded (declined with a caller-visible reason) | excluded |

Native-chunked route: FK-participating tables on either side reroute to the oracle (`fk_relationship_not_native_route`), so no FK cell is admitted. Non-FK payload follows the operator registry: Rust kernels, faker as compiled index selection, redact and truncate as named-Arrow, passthrough as a no-op. Because this route admits no FK cell, its non-FK compiled cells carry no fresh witness in this audit; they are proven by native-chunked-route suites (each drives `run_mask_chunked` and imports nothing from `out_of_core`). `test_native_chunked_compiled_cells_cite_their_real_kernel_suite` asserts the cited strategy set equals the derived compiled and selection set (nine cells), so a new chunked compiled cell fails until it cites a suite: hash `tests/native/test_chunked_entry_parity_matrix.py`, categorical `test_chunked_categorical_parity.py`, bucket_perturb `test_chunked_bucket_perturb_parity.py`, date_shift `test_chunked_date_shift_parity.py`, fpe `test_c6a_fpe_chunked.py`, group_key `test_chunked_group_key_parity.py`, faker `test_chunked_nondet_faker_parity.py`, text_mask `test_c6b_i_text_mask_chunked.py`, text_redact `test_c6c_i_text_redact_chunked.py` (all under `tests/native/`). Hash has no dedicated chunked parity suite; its chunked coverage is the shared entry parity matrix.

## Dedicated-Python exceptions (the documented set)

- code_set: no kernel on any route. Backlog item: kernelize as a named-Arrow dict-map.
- NER (the `ner` config of text_redact and text_mask): Python by design (spaCy inference).
- faker cells admission declines (provider outside the allowlist, non-string source, whole-column cardinality modes, no companion) and faker FK-key cells.
- Non-injective native-capable strategies configured as an FK key on SEQ: declined with a reporting-only entry, the Python handler runs.
- out-of-core text_mask: the shared `mask_cell` per-cell loop, no Rust span path on that route. Found by this audit; the program did not list it. Backlog item: route it through the same span kernel the SEQ and native-chunked routes use. Byte-parity for the OOC text_mask and code_set dedicated-Python cells is proven by `tests/parity/test_out_of_core_group_c_parity.py`.
- The remaining shared SEQ handlers listed above run on the full-frame handler by design; none has native admission on any route.

## Qualification of program section 5

The program's "Rust/Arrow-only compute on admitted routes" holds for the strategies that have a kernel on that route (the compiled, selection, named-Arrow and no-op rows). It is not a claim that every strategy is kernelized. The dedicated-Python rows above are the exceptions, each classified and witnessed as running its own handler.

## Live fallbacks retained and fenced

- redact pandas fallback (`_strategies/_redact.py`): catches the `pa.array(col, from_pandas=True)` conversion failure that `redact_array`'s own reference path cannot. A test shows an admitted string column takes the kernel and only the conversion-failure column reaches the fallback.
- out-of-core bucket_perturb pandas bridge (`out_of_core/_mask_group_c.py`): witnessed as the legacy path only when the companion is absent; native runs consume the kernel result.
- Handler Python branches for declined, NER and companion-absent cells: unchanged and still covered by the per-operator suites.

## Reorder reachability

`tests/unit/execution/test_r3_ooc_reorder_reachability.py` and `tests/perf/test_d4a_reorder_reachability.py` run green. Baselines: `docs/records/2026-10-10-ooc-reorder-liveness.md`, `docs/records/2026-10-15-d4a-reorder-revalidation.md`. The live lane is P4-A external reorder; no OOC-B O(rows) path is reachable.
