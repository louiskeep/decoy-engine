# C5c-ii: deterministic Faker over non-string sources on the native routes

Status: plan (rev 1, awaiting Codex plan gate). Phase C, Rust-engine program.
Branch `feat/c5c-ii-deterministic-nonstring-faker` off engine main `69a7741f`.
Follows C5c-i (#220, positional Faker over non-string) and C5c-a (#221, oracle
sampling-boundary fix for deterministic Faker over nullable ints).

Rules consulted: 00-universal, risk-and-exceptions, development-loop, feature-dev, testing,
architecture, api-and-compatibility, performance, documentation.

## 1. Why / scope

The native routes (unified full-frame + chunked native) today DECLINE a deterministic Faker
column over a non-string source, so such a column forces its table off the native path. C5c-i
opened the POSITIONAL (non-deterministic REUSE) Faker over bool/int/uint/float; C5c-a fixed the
ORACLE so deterministic Faker over nullable ints works (exact integers supplied at the Faker
sampling boundary, frame untouched). C5c-ii opens the NATIVE routes to **deterministic** Faker
over the non-string families C5c-a made correct: **bool, signed/unsigned integers, and tz-aware
timestamps** (string-output provider only). Later families (date/time/duration, decimal, binary,
nested, float16, null, dictionary) stay out, as in C5c-i.

The one design problem that kept deterministic Faker out (C5c-i Codex round 1, HIGH 5): the
**degenerate output type**. An all-null deterministic-Faker chunk resolves to Arrow `null` on the
oracle and an empty chunk to `double`, while the native route emits `string`; the chunk joiners
reject `double`/`null` beside `string`. Positional Faker avoids this because its chunked output is
config-pinned to `string` (`native/_chunked_schema_rule.py` + `faker_positional_pinned_columns`).

## 2. Decision (Cam 2026-10-09): option A — pin the degenerate output to `string` on both routes

Pin a deterministic Faker column's degenerate (all-null / empty) output to `string` on BOTH the
oracle and the native routes, exactly as positional Faker and the C1 categorical slice already
do. This is a **pre-GA output-type change** for the empty/all-null case only (an empty or all-null
deterministic-Faker column over a non-string source becomes `string` instead of `double`/`null`);
value-bearing columns are unchanged. The string-output provider guarantees every produced value is
a string, so `string` is the honest type and the pandas `double`/`null` were inference artifacts of
an empty/all-null column. Rationale and precedent are the same as C5c-i §3 / `_chunked_schema_rule`.

## 3. Established methodology (cite in the implementing docstring)

Reuse the existing machinery, do not add a parallel one:
- The string pin: extend `faker_positional_pinned_columns` (or add a sibling classifier in the
  same module) so the deterministic-Faker admitted set is pinned by the SAME config-only rule at
  BOTH `_chunked_schema_rule` construction sites. One classifier, two sites, as today.
- The sampling boundary: deterministic selection already goes through the C5c-a exact-integer
  supply and `native/_index_ext.py` (compiled index kernel + pure-Python oracle). C5c-ii changes
  admission + output typing, NOT the draw: the drawn values are identical to C5c-a's oracle.
- Registry types: list the admitted datatype instances explicitly (`_operator_registry.py`,
  `_unified_slice_resident_types.py`), not a predicate over arbitrary types (C5c-i precedent).

## 4. Design

### 4.1 Admission (both routes)

Add a deterministic-Faker admission path mirroring `positional_faker_config_of_entry`
(`native/_faker_positional_admission.py`), gated on: `deterministic` true, a string-output
provider on the C1 allowlist, a non-string source in the admitted families (bool, int8-int64,
uint8-uint64, tz-aware timestamp), no `when:` (deferred, as positional defers it). The chunked
route admits the C5c-a nullable-int case (exact integers at the sampling boundary). 

The UNIFIED full-frame route keeps its existing round-trip guard
(`_unified_slice_admission.py:375-399`): a column whose pandas round trip is not type- and
value-identical to the source still declines there. So on the unified route only null-free
int/uint, bool, and (if round-trip-identical) tz-aware timestamp reach the binder; an int column
WITH nulls keeps declining on the unified route and runs on the chunked route or the oracle. This
plan does NOT change that guard (C5c-a / C5c-i precedent); it only adds the deterministic families
to what the guard is allowed to pass.

### 4.2 Degenerate output pin (the core change)

- **Native chunked:** add the admitted deterministic-Faker columns to the string-pinned set at
  both `_chunked_schema_rule` sites, so all-null/empty native chunks emit `string`.
- **Oracle:** pin the oracle's deterministic-Faker output for an admitted column to `string` on
  the degenerate (all-null/empty) case too, so the chunked oracle leg matches the native leg and
  the joiners never see `double`/`null` beside `string`. This is the pre-GA output change (§2);
  it is applied ONLY to the admitted deterministic-Faker families, nothing else.
- **Unified full-frame:** the full-frame assembly (`physical/_shadow_assembly.py`) resolves an
  empty/all-null admitted column to `string` as well, consistent with the chunked legs and the
  positional precedent. The route-dependent difference that remains (if any) is pinned explicitly
  per `docs/compatibility-contract.md` ROUTE-OUTPUT-CONTRACT, not asserted equal across routes.

### 4.3 What does NOT change (C5c-a lesson)

The shared frame, `when:`/derived/case_when readers, group_by siblings, pandas metadata, the
positional-Faker outcomes (C5c-i), and the deterministic draw values (C5c-a) are all untouched.
The fix stays at admission + output typing + the Faker sampling boundary, never the shared frame.

## 5. Build order (tests-first)

1. Acceptance tests first (§7), red where they need the new admission/pin, green where they pin
   existing behavior.
2. Admission: deterministic-Faker config classifier + registry datatype instances + the two
   route gates.
3. Output pin: extend the string-pin classifier + the oracle degenerate pin + the unified
   assembly.
4. Gates: full faker/native/parity suite green; the pre-GA degenerate-output change recorded in
   CHANGELOG + compatibility-contract; re-run C5c-i/C5c-a suites unchanged.
5. Docs: plan -> reference, build record, roadmap row, the CHANGELOG pre-GA note with a migration
   line (empty/all-null deterministic-Faker non-string column: `double`/`null` -> `string`).

## 6. Failure modes

- A non-string-output provider on a deterministic Faker column: fails closed before any chunk
  (as positional does), never silently typed.
- Int-with-nulls on the UNIFIED route: keeps declining (round-trip guard), runs chunked/oracle.
- A source family outside the admitted set (date/time/duration/decimal/...): declines on both
  routes, unchanged.
- Companion absent: deterministic selection has its pure-Python oracle (`_index_ext.py`), so the
  column still runs (byte-identical), its table just not accelerated.

## 7. Acceptance tests (byte-identical within a route; pre-GA degenerate change pinned)

- **Value parity:** deterministic Faker over bool, int8-int64, uint8-uint64, and tz-aware
  timestamp, string-output provider: native (chunked + unified) == oracle, value-for-value,
  including the C5c-a nullable-int case on the chunked route.
- **Degenerate output (the §2 change):** all-null and empty admitted columns emit `string` on the
  oracle, the native chunked leg, and the unified leg; a test pins the NEW type and a migration
  note. A chunk-count-invariance test: data split 1 vs N ways gives identical Arrow type.
- **Chunked stability:** nulls in some chunks and not others gives the same output as one pass
  (chunked oracle vs whole-frame), reusing the C5c-a characterization.
- **Unified round-trip guard intact:** an int-with-nulls column still declines on the unified
  route (not newly admitted); proven not-vacuous.
- **Unchanged:** C5c-i positional outcomes and C5c-a oracle outcomes are byte-identical; the
  shared frame / other readers unaffected (a `when:`/derived reader over the same column is
  unchanged).
- **Mutation** on the admission classifier, the string-pin set membership, and the degenerate
  retype. **Perf**: a record at 1M rows that the admitted table now runs native (not a budget
  gate; deterministic Faker speed itself is unchanged, the win is the table staying native).

## 8. Open points for the Codex plan gate

- Confirm tz-aware timestamp is round-trip-safe enough to admit on the unified route, or restrict
  it to the chunked route in rev 1.
- Confirm the oracle degenerate pin is scoped strictly to the admitted deterministic-Faker
  families (no bleed into other strategies' empty/all-null typing).
- Confirm the single string-pin classifier cleanly covers positional AND deterministic without a
  precedence bug when a column could match both shapes.
