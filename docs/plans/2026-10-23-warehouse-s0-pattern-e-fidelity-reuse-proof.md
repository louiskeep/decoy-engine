# Warehouse sources — S0: Pattern-E export fidelity reuse proof

**Status:** implementation plan, 2026-10-23. First slice of the V2 warehouse-sources work. Engine-side, **tests + fixtures only, no src change**. Plan-gate ready (Opus plan → Codex plan-gate → Sonnet build tests-first → dennis → Codex final → ci-mirror).

**Parent framing:** `decoy-platform/docs/plans/2026-10-23-warehouse-sources-cloud-output-framing.md` (read/write envelope: read Redshift/Snowflake/MSSQL, write cloud-bucket files only).

**Rules consulted:** 00-universal, development-loop, reuse-first, scope-discipline, security, use-established-methodology, documentation.

---

## 1. Why this is the first slice

The whole Pattern-E premise is: *a warehouse's native Parquet export, read through Decoy's existing S3 input, masks correctly through the unchanged engine seam* — so "warehouse support" for Redshift/Snowflake is mostly platform binding/UX, not new engine code. That premise is **unverified**. If a warehouse's export encodings (decimal128, int96 timestamps, semi-structured-as-JSON-string) do not round-trip losslessly through the engine's Parquet reader + Arrow typing + masking, the reuse win shrinks and we need preserve-or-reject guards.

S0 answers that question **before** any platform work, with zero production-code risk and zero open Cam decision. It mirrors the roadmap's proven pattern (Phase D's D0 and Phase F's F0 were tests-only slices that froze a parity contract + baseline before any kernel landed).

This slice does **not** build a connector, touch `CloudProvider`, or write production code. It establishes a frozen **fidelity matrix** and a passing **reuse proof**, and it pins (with coded reasons) any warehouse export type that does not round-trip.

## 2. Grounding in current code (verified 2026-10-23)

- `src/decoy_engine/connectors/s3.py` (443 LOC) — `S3FileSource` / `S3FileSink`; the Pattern-E read path. Reads Parquet from object storage; this is what a warehouse export lands in.
- `src/decoy_engine/connectors/postgres_snapshot.py` (529 LOC) — the v1 Pattern-L precedent. **Reuse its type-fidelity knowledge directly:** its docstring + `_classify`/`_guard_values` enumerate exactly the losses DuckDB inflicts (unbounded/>38-digit numeric → DOUBLE, numeric NaN → NULL, far timestamps wrapping int64, infinity → sentinel, `character(n)` blank-trim, array lower-bound drop). A warehouse export that was itself produced by a DuckDB/warehouse `COPY` can carry the analogous encodings; the S0 matrix must probe the same hazard classes on the *read* side.
- The engine I/O contract is file-shaped: once a Parquet set is read + profiled, the entire masking seam (profile → plan → route → mask → validate → sink) is source-agnostic. S0 verifies the *read + profile + mask* leg on warehouse-shaped Parquet.
- Fixture/test precedents to mirror: `tests/connectors/` (postgres snapshot tests, `conftest.py`), `tests/integration/golden/test_execution_e2e.py` (canonical caller shape), `tests/parity/SEMANTIC_DIFFERENCES.md` (how pinned differences are recorded).

## 3. Scope

### In scope
1. **Warehouse-export Parquet fixtures.** Synthetic `.parquet` files (built with pyarrow) that reproduce the physical encodings Redshift `UNLOAD ... FORMAT PARQUET` and Snowflake `COPY INTO <stage> ... TYPE=PARQUET` actually emit, across a representative type set:
   - integers (int16/32/64), float/double, boolean
   - `DECIMAL`/`NUMBER` → Parquet `decimal128` at several precision/scale points **including ≤38 and the >38 / unbounded boundary**
   - strings / `VARCHAR` (incl. unicode, empty, long)
   - timestamps: `TIMESTAMP_NTZ` (int64 micros, tz-naive), `TIMESTAMP_TZ`/`TIMESTAMPTZ` (tz-aware), and the int96 legacy encoding Redshift/Spark exports can carry
   - dates
   - semi-structured: Redshift `SUPER` and Snowflake `VARIANT`/`OBJECT`/`ARRAY` exported as **JSON strings** (the normal Parquet export form), plus the nested-logical-type form where applicable
   - `bytea`/binary, `uuid`-as-string
   - NULLs in every column; sentinel/edge values (NaN decimal, ±inf, far-future timestamp)
2. **A relational (parent/child FK) warehouse-export fixture** so the proof covers FK preservation, not just scalar fidelity.
3. **Reuse-proof tests.** For each fixture: read via the S3/file source path, run `profile_source`, and run the canonical masking pipeline (`run_pipeline`) with a representative per-column strategy set over the FK dataset. Assert:
   - untouched (passthrough) columns are byte-faithful in value, type, and nullability
   - masked columns produce correct, type-stable output
   - FK integrity holds parent→child after masking
   - no silent coercion (dtype/precision/tz preserved where the engine claims to preserve it)
4. **Frozen fidelity matrix doc** (`tests/` or `docs/records/`): rows = (warehouse, source type, export encoding), columns = engine disposition (`faithful` / `faithful-with-note` / `preserve-or-reject gap`), each row backed by a test. This is the S0 contract artifact.
5. **Coded preserve-or-reject gaps.** Any export type that does NOT round-trip is pinned as a known gap with a stable reason token (to be handled by a later slice / platform admission), not silently accepted.

### Out of scope (this slice)
- Any production/src change (connector, `CloudProvider`, admission).
- A live warehouse connector (Pattern L) — demand-gated research, separate slice.
- MSSQL (its export story + community-extension decision is a later slice; S0 is Redshift/Snowflake Pattern-E).
- Orchestrating the `UNLOAD`/`COPY` (that's a platform/product decision, §6 of the framing doc).
- The actual S3 network path (fixtures are local Parquet read through the same reader; object-storage transport is already covered by existing S3 connector tests).

## 4. Deliverables

- `tests/connectors/warehouse_exports/` (or similar): a fixture builder (`conftest.py` / a `_fixtures.py`) that mints the warehouse-shaped Parquet deterministically, and the test modules asserting the reuse proof.
- A checked-in fidelity-matrix record (markdown + the per-row assertions).
- CHANGELOG entry under the warehouse-sources work.
- No change under `src/`.

## 5. Exit criteria

- Every fixture type either masks through the unchanged seam with proven fidelity **or** is pinned as a coded preserve-or-reject gap with a test documenting the exact loss.
- FK integrity proven on the relational fixture.
- The fidelity matrix is frozen and cited by the tests (a later change to engine Parquet/Arrow handling that breaks a `faithful` row fails this suite).
- Full gate chain green (Codex plan-gate → dennis → Codex final → ci-mirror). No src change means no companion/perf matrix.

## 6. Risks / honest notes

- **Fixtures must match real warehouse output, not our assumption of it.** The highest-value fixtures are the ones that encode what Redshift/Snowflake *actually* write (e.g. Snowflake `NUMBER(38,0)` → `decimal128(38,0)`; `VARIANT` → JSON UTF-8 string; `TIMESTAMP_NTZ` → int64 micros). Cite the warehouse export docs in the fixture-builder docstring (use-established-methodology). Where we cannot confirm an encoding without a live warehouse, mark that fixture row "assumed encoding — confirm against a real export in S2" rather than claiming certainty.
- **>38-digit / unbounded decimals and far timestamps are the known loss classes** (per the postgres_snapshot precedent). Expect these to land as preserve-or-reject gaps, which is a *correct* S0 outcome, not a failure.
- **Semi-structured-as-JSON-string is the subtle one:** it round-trips as an opaque string (fine for passthrough/redact/hash), but masking *inside* the JSON is a separate capability (`text_mask`/nested) and out of S0 scope — the matrix must say so.
- This slice's value is the matrix: it converts "Pattern E should just work" into evidence, and scopes the real platform work precisely.

## 7. Handoff to S1

S0's matrix tells S1 (platform SOURCE binding) exactly which warehouse export types are safe to admit and which must be rejected at submission — so the platform admission rule is derived from proven engine behavior, not assumed.
