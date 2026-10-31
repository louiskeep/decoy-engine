---
Status: current
---
# blissful-goldberg harvest: what was parked (2026-10-31)

Source branch `claude/blissful-goldberg-mkwpdk` at `e1f96acb` (11 commits past
its merge-base `195fab12`, 66 behind `main` at harvest time). The branch was
not rebased, modified or merged. The three stand-alone correctness commits
were cherry-picked onto `fix/ooc-correctness-harvest` (off `origin/main` at
`a6e5c25`); everything else is the out-of-core channel work the parked
route-coverage plan owns and stays on the source branch for that plan to pick
up. Read the source commits there; nothing below was re-implemented.

## Disposition of the 11 source commits

| Source commit | Subject | Disposition | Reason |
| --- | --- | --- | --- |
| `a9c4d256` | F6: dedupe text_redact NER config/version, window the OOC NER apply | LANDED | Stands alone. Ported onto main's layout: `storm/ner.py` is `detection/ner.py` (STORM split), the mismatch code is `reasons.runtime.NER_MODEL_VERSION_MISMATCH` (F29 registry), and the new `_strategies/_ner_config.py` is allowlisted in the physical-seam sentry. |
| `7d736a6b` | F1: correct top_code capability to row-local (was falsely is_global) | LANDED | Stands alone; applied clean. Native route still rejects `top_code`, the reason moves from `requires_global_execution` to `no_native_kernel`. |
| `4e801b6e` | F4/F5: make out-of-core strategy declines actionable and honest | LANDED | Stands alone; applied clean. Its docs describe the pre-F2/F3 decline set (`bucketize`/`date_shift`/`top_code` declined, cross-row set declined), which is main's truth today. A message-shape pin test was added alongside it. |
| `fb0e3ec0` | F2: out-of-core row-error channel; admit bucketize and top_code | PARKED | The row-error bridge itself (`_row_error_bridge.py`, `_emit.py`, `_stream_driver.py`, `_runner.py` wiring) and the compat admission of `bucketize`/`top_code` on the OOC route. Channel work; route-coverage plan owns it. |
| `79baae35` | F3: out-of-core row-offset channel; admit windowed_date | PARKED | The offset bridge (`_offset_bridge.py`) and the compat admission of `windowed_date`. Channel work. |
| `3b9ee12c` | F2/F3: reconcile docs and support-surface pins to the admitted strategies | PARKED | Docs and the D-audit matrix / route-seam parity pins describe behaviour only F2/F3 provide. Coupled whole. |
| `5fb89d15` | fix(ooc): clear the two CI-red blockers from the strategy-coverage gate | PARKED | Allowlists the F2 bridge modules in the physical-seam sentry and patches `_runner.py` for the bridge; meaningless without F2. |
| `548c3a79` | fix(ooc): correct windowed_date FK anchor and top_code batch dtype parity | PARKED | Remediation of the F2/F3 bridges (offset-bridge FK anchor, row-error-bridge dtype parity). Coupled whole. |
| `93b65cae` | fix(ooc): derive top_code batch dtype from source pandas metadata | PARKED | Edits `_row_error_bridge.py` only. Coupled whole. |
| `cd772c4f` | fix(ooc): decline non-batch-stable row-error columns instead of forcing | PARKED | Row-error-bridge admission policy across `_compat.py`, `_preflight.py`, `_pipeline_routing_signals.py` and the bridge. Coupled whole. |
| `e1f96acb` | fix(ooc): decline string/large_string for bucketize on the row-error bridge | PARKED | Row-error-bridge dtype admission. Coupled whole. |

Nothing was DROPPED: none of the three landed fixes was already on main
(`top_code` was still `is_global=True`, the cross-row decline was still the
bare phrase, and `_ner_config.py` did not exist).

## For route-coverage planning

The eight parked commits are one unit: F2 introduces the row-error channel
and F3 the row-offset channel; the remaining six only exist to make those two
bridges correct and CI-green. When the plan picks them up, note that they
were written against a `main` that predates the H-1 layout (#324/#348), the
F29 reasons registry and the current physical-seam allowlist, so they will
need the same kind of port F6 needed here, and that F1 (landed) already
de-claims the "is_global mirror" comments in `physical/_shadow_full_frame.py`
that F2's admission of `top_code` relied on being wrong.

cam
