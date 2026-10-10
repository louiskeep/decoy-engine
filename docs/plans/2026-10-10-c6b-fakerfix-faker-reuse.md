Status: plan (rev 1, pending Codex plan gate)

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, documentation; engine CLAUDE.md "use established methodology" (Faker is the established library; reuse is its documented performance pattern).

# C6b-fakerfix: reuse the Faker instance in text_mask span synthesis

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C (text_mask track). Branch `feat/c6b-fakerfix` off engine main `35db29c6` (post C6b-i #233). Risk: **MEDIUM** — tiny surface, but on the KEYED masking determinism path, where a subtle reuse bug would silently change masked output (a correctness/privacy defect). Byte-parity is the whole contract.

## 1. Goal and scope
`transforms/text_mask.py::_mask_faker` constructs a fresh `Faker()` on every faker-strategy span. The 2026-10-10 throughput measurement found this is ~58% of the realistic mixed-PII text_mask cell (886us/span; reusing one seeded instance is ~93us, ~8-10x cheaper), making it the single highest-leverage speedup for text_mask and a prerequisite for any later text_mask Rust work (Amdahl: faker spans decline to Python in any Rust design, so this Python cost is the floor).

**In scope:** replace the per-span `Faker()` construction in `_mask_faker` with a reused, lazily-created, thread-local default-locale `Faker()` that is re-seeded per span. Output must be byte-identical. Nothing else changes: the seed derivation (`int.from_bytes(span_key[:4], "big")`), the `_FAKER_METHOD` mapping, the method-raises -> `fake.name()` fallback, the raw-value isolation (matched_text consumed only via span_key), and every other strategy/path are untouched.

**Out of scope:** any Rust kernel (the separate, now-narrowed C6b-ii decision, pending Cam with post-fix numbers); the pooled-Faker masking path used by C5a/C5b (a different code path — this is only the free-text per-span faker inside text_mask); any change to detectors, keying, or output values.

## 2. Established facts (2026-10-10; file:line)
- `_mask_faker` (`transforms/text_mask.py:473-494`): `method_name = _FAKER_METHOD.get(detector_id, "name")`; `seed = int.from_bytes(span_key[:4], "big")`; `fake = Faker(); fake.seed_instance(seed)`; `method = getattr(fake, method_name, None)`; if callable, `return str(method())` else (or on any `Exception`) `return str(fake.name())`. `from faker import Faker` at `:114`.
- Called only from `_mask_span` (`:569`) for faker-strategy spans, inside the per-cell `mask_cell` splice loop (`:606+`), which runs per cell over a Python list (ARROW_PYTHON: `native_text_mask` iterates `array.to_pylist()`; the oracle `TextMaskHandler.run` iterates per cell). Column-level parallelism (`native_threads`) can run different columns on different threads, so the reused instance must not be shared across threads.
- Faker semantics (established library, pinned `faker==40.23.0`, see [[decoy-faker-lock-regression]]): `seed_instance(seed)` reseeds the instance's `Generator` Mersenne Twister, which every provider draws from; output after `seed_instance` is fully determined by (seed, the method call sequence), independent of any prior instance state. A default `Faker()` (no locale arg) and a reused default `Faker()` share the identical provider set + locale, so reseed-then-draw is byte-identical. This is Faker's own documented reuse pattern.
- Measurement harness + numbers: session scratchpad `text_mask_timing.py` (886us fresh vs 93us reused; 57.9% of the realistic-mix cell).

## 3. Decisions / method
3a. Add a module-level thread-local holder and a `_shared_faker()` accessor in `transforms/text_mask.py`:
```
_FAKER_TLS = threading.local()
def _shared_faker() -> Faker:
    fake = getattr(_FAKER_TLS, "instance", None)
    if fake is None:
        fake = Faker()              # default locale, identical to the old per-span Faker()
        _FAKER_TLS.instance = fake
    return fake
```
3b. In `_mask_faker`, replace `fake = Faker()` with `fake = _shared_faker()`; keep `fake.seed_instance(seed)` and everything after IDENTICAL (same method lookup, same `try/except Exception -> fake.name()` fallback). Because `seed_instance` precedes every draw, reuse is byte-identical to fresh construction, including the fallback path (a failed `method()` advances the generator identically under the same seed, so the subsequent `fake.name()` is identical).
3c. Thread-local (not module-global) so concurrent columns on different `native_threads` threads never share a generator mid-reseed; within a thread the instance is reused across all spans/cells/columns. No API/signature change; no change to callers.
3d. No change to seeds, mappings, fallbacks, keying, or raw-value isolation. Comment updated to explain the reuse + reseed-per-span invariant (why, not what).

## 4. Acceptance tests (written first; byte-parity is the contract, never weakened)
1. **Exhaustive byte-parity vs a fresh-per-span reference.** A test-local reference reproduces the PRE-FIX body (`Faker(); seed_instance(seed); method()/name()`). For every `detector_id` in `_FAKER_METHOD` plus an unmapped id (-> `name`), across many span texts and the full seed space sampled widely (incl. seed 0 and 0xffffffff boundaries), assert production `_mask_faker` (reused) == the fresh reference, char-for-char.
2. **Fallback-path parity.** A detector mapped to a method that raises / is unavailable falls back to `fake.name()` byte-identically under reuse (monkeypatch a mapped method to raise; compare reused vs fresh reference).
3. **Reseed isolation / order independence.** Masking span B after span A on the SAME reused instance yields the identical result as masking B first (same span_key -> same output regardless of intervening draws), proving `seed_instance` isolates.
4. **Thread safety.** Run `_mask_faker` for a fixed set of (span_key, detector_id) concurrently across several threads; every result equals the single-threaded reference (no cross-thread generator bleed changes output).
5. **Golden snapshots unchanged.** The committed text_mask goldens that exercise faker spans (person_name/address, default and per-detector faker overrides) are byte-identical before/after; `tests/native/test_c6b_i_text_mask_*` parity matrices stay green (native == oracle, since both call the same `mask_cell`).
6. **Testflight fingerprints** unchanged (faker-bearing fixtures): STOP if any moves.
7. **Throughput (informational, not a hard gate):** record faker-span ns/cell and realistic-mix ns/cell before vs after with the measurement harness, to confirm the ~8-10x faker-path / ~2x mix win and to feed the Cam Rust-decision. No perf budget assertion added (avoids a flaky gate).
8. ruff + mypy clean; module-size census (file stays well under cap); no new log lines (raw-value isolation unchanged).

## 5. Failure modes
| Risk | Closed by |
|---|---|
| Reuse changes synthetic output | 3b seed_instance-resets-PRNG + test 1 exhaustive byte-parity across all methods + test 5 goldens |
| Fallback path (method raises) diverges | 3b identical post-seed body + test 2 |
| Cross-thread generator race under native_threads | 3c thread-local + test 4 |
| Intervening spans leak state into a later span | 3b reseed-per-span + test 3 |
| A Faker-version provider-state quirk (fresh vs reused) | pinned faker 40.23.0 + test 1 exhaustive + test 5 goldens; if any mismatch appears, STOP (do not ship) |
| Scope creep into pooled-Faker / Rust | 1 scope fence; Rust is the separate C6b-ii Cam decision |

Rollback: revert the one-function change; `_mask_faker` returns to per-span construction.

## 6. Review log
- rev 1 (DRAFT): Opus-authored from the 2026-10-10 text_mask throughput measurement (faker construction = 58% of the realistic mix; reuse ~8-10x, byte-identical). Pending Codex plan gate.
