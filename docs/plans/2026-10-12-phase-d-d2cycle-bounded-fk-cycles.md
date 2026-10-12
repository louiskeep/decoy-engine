# Phase D / D2-cycle: cross-table FK cycles native on the bounded (SEQ) route

Status: DEFERRED TO V2 (2026-10-12, Cam's pre-authorized "defer if too hard" rule, invoked by the
rev-5 Codex plan-gate NO-GO). The memory-bound defect class defeated FOUR iterations (plan rev-3 HIGH-2,
rev-4 MED, implementation 4 BLOCKERs, rev-5 narrowing NO-GO). STOP POINT: the rev-5 narrowing closed
BLOCKER-1 (composite-FK writer closure), BLOCKER-3/4 (nested/pool payloads on CYCLIC tables), and the
BLOCKER-2 hashed-bool output-width case, but a NEW BLOCKER remained: the pool/corpus/NER/variable-width
strategy-state exclusion is COMPONENT-LOCAL while the memory bound needs to be JOB-WIDE - a pool payload on
a DISCONNECTED acyclic table (person_email pool_size=1e8 builds an 800MB pool before the cache cap), a
spaCy NER pipeline (text_redact/text_mask), or variable-width passthrough/FPE/truncate-with-mask_char
ANYWHERE in the job still allocates unbounded state the bound does not cover. V2 PATH (Codex-validated,
so V2 RESUMES not restarts): define ONE job-wide, default-deny MEMORY-ELIGIBILITY rule covering every
executed strategy + dispatch dependency across incoming/outgoing/disconnected tables - admit only configs
with explicit bounds for input, output, scratch, AND retained strategy state; decline everything else
before execution. Codex confirmed a useful non-empty subset exists (fixed-width inputs, bounded scalar
outputs, pure key writers, lazy sources, draining sink) with NO parity defect. VALIDATED + REUSABLE for V2:
the two-pass column-order effective-map core (byte-parity proven by dennis + Codex), the composite-FK-writer
decline, the transitive nested-strategy resolution (`nested_recursive_nested_rejected` already rejects deep
nesting), the whole-job output-width accounting. BRANCH feat/phase-d-d2cycle-bounded-fk-cycles is PARKED
(build commit d3526f90 + plan rev5 7b0c6c2b) - NOT merged, NOT deleted; resume here for V2. See
decoy-platform/docs/backlog/v2-candidate-plan.md.

[SUPERSEDED] rev 5, re-scoped after the Codex FINAL-gate NO-GO - re-gating the delta before rebuild).
rev 4.1 built (commits f41603f9/baa3c77c/671cb251) + dennis-APPROVED + remediated (d3526f90), but the
Codex FINAL gate returned NO-GO with 4 BLOCKERs - all at the ADMISSION BOUNDARY, the core two-pass
parity is validated. (1) pass 1 silently skipped an incoming COMPOSITE-FK writer of a cyclic key ->
raw values / RI divergence on an "admitted" job; (2) memory pricing covered only cyclic-table source
bytes, ignoring the WHOLE-JOB working set + strategy OUTPUT-WIDTH expansion (a disconnected 1M-row
hashed bool: priced 758KB, real 68MB under a 67MB budget); (3) `nested(strategy="code_set")` bypassed
the unpriced decline (only outer strategies enumerated); (4) a faker pool is built to full `pool_size`
BEFORE the cache cap (person_email pool_size=1e8 admitted at 268MB, builds 800MB). rev 5 RADICALLY
NARROWS so the bound is trivially defensible (Cam's authorized "narrow further" path): DECLINE
incoming-composite-FK-writers-of-a-cyclic-key, DECLINE all pool/corpus/generator/nested payload
strategies on cyclic tables (resolving nested/transitive strategies), and price the WHOLE-JOB
bounded-width per-table working set from REAL decoded/output sizes (not synthetic arithmetic). The
admitted set shrinks to hash/passthrough/redact/truncate/fpe-keyed cyclic chains with bounded-width
payloads that fit memory; everything else keeps running on full-frame. DECISION: this is the memory-bound
defect class recurring (plan rev-3 HIGH-2, rev-4 MED, now impl) - resolved by declining, not by a cleverer
pricer; if the rev-5 delta plan-gate finds the narrowing still unsound, defer D2-cycle to V2.
[HISTORY] rev 1 NO-GO (wrong re-derivation premise); rev 2 NO-GO
(transform semantics + memory); rev 3 NO-GO (2 HIGH: the gate again VALIDATED the
effective-map core, canonicalization, inheritance, and confirmed transforms resolved +
projected reads buildable, but found two NEW holes). rev 4 narrows per the gate's own
root-cause fixes - this is the "narrow further (more declines)" path Cam authorized:
- HIGH-1: inheritance is strategy-agnostic on the CONSUMER (correct), but PRODUCING the
  parent's effective output from a key-only read requires the producer be a PURE
  FUNCTION OF THE KEY VALUE. A `derived` key reading payload siblings, or a positional
  strategy with duplicate keys, cannot be reproduced narrowly. rev 4 admits only
  pure-value-function key producers (the allowlist belongs on the PRODUCER, not the
  consumer); declines derived/sibling/positional/conditional producers.
- HIGH-2: the combined-memory formula omitted `quarantine_entries` (whole masked rows),
  cross-table error caches, and the shared `PoolCache`. rev 4 prices them and declines
  quarantine-producing orphan policies on cyclic tables so retained state stays narrow.
rev 4.1 (GO) folds the round-4 gate's 3 MED + 1 LOW build obligations as hard
constraints: (MED) price PoolCache/code_set/per-cell error caches/transition overlap or
decline - governing rule "price every live allocation or decline"; (MED) quarantine is
JOB-level (enabled+triggers) so decline the whole cyclic route when quarantine triggers
are on, FAIL is an allowed abort; (MED) eligibility checked against EVERY actual key
writer (joint_mask etc.) with a DEFAULT-DENY five-strategy allowlist; (LOW) build the
map in source order, execute standalone producer keys from originals (consumer map is
for FK consumers only). Cam 2026-10-12: keep in v1 and ATTEMPT it. STATUS: the branch is
on post-D2b main (HEAD d3526f90); rev-5 re-scope (above) re-gates the narrowing delta with
Codex, then the builder remediates the 4 BLOCKERs + the seam-sentry MED on the same branch,
then dennis + Codex final again before merge.

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md,
architecture.md, reuse-first.md, testing.md, documentation.md,
autonomous-operation.md. Engine CLAUDE.md, compatibility-contract.md.

Risk: R2. Adds masking for a shape the bounded routes decline today, with byte-parity
+ RI promises. Plan + independent review + Codex plan gate before build.

## FRAME

**Problem.** A cross-TABLE FK cycle (A->B->A) masks TODAY only on full-frame (all
tables resident, no table-level order needed); the bounded routes decline. A LARGE
cyclic job (too big for a full-frame box) has nowhere to run and stays on the old
path. Move it to the bounded SEQ route.

**The corrected key insight (rev-1 was wrong here).** The TABLE graph cycles, but the
COLUMN graph is ACYCLIC (that is exactly why full-frame succeeds: it masks columns in
column-topological order, which exists). So the cycle is maskable WITHOUT a
table-level order by working in COLUMN-dependency order. Crucially, a child FK key
does NOT re-derive from its own source value; it INHERITS the parent key column's
EFFECTIVE OUTPUT (the actual masked value, which may be a token, or RAW if the parent
is `when:`-gated off, or a preserved-raw orphan, or absent if the parent row errored,
with duplicate source keys resolving last-wins). Re-derivation reproduces none of
those and also breaks on FK-key canonicalization (FK lookup treats `True`==`1`, the
hash kernel does not). The fix is to carry the effective MAP, not to re-hash.

**Definition of done.** On the SEQ route, an admitted cross-table cycle masks
byte-identically to the full-frame oracle (strict D0 dimensions), with RI and
orphan/cascade/policy/duplicate semantics identical to full-frame, while keeping only
the narrow key-column state of the cycle resident across tables (not every wide
frame). Non-admitted cycles and all OOC cyclic jobs keep today's decline with a clear
reason.

**Boundaries / v1 narrower-first cut (admission gate).** SEQ only. Admit a cyclic
component only when ALL of:
- its WORK-NODE dependency graph is acyclic (`order_work`, `_runner.py:233/319`, which
  already orders parents before children and rejects unresolved cycles). Table
  cyclicity does NOT imply column/work acyclicity - decline if the work graph cycles.
  NOTE `order_work`'s edges are FK/composite edges, NOT arbitrary strategy sibling
  reads, so work-acyclicity is NECESSARY but NOT SUFFICIENT (rev-3 HIGH-1) - the
  producer-eligibility condition below is what closes the sibling-read gap.
- every FK KEY column in the component is produced by a PURE VALUE-FUNCTION strategy,
  and this is checked against EVERY ACTUAL WRITER of that key column, not only the
  strategy on the key's own declaration (rev-4 MED): a payload `joint_mask` or other
  multi-column writer can overwrite a key column it lists (`joint_mask.py:255`), so a
  nominally-passthrough key could be rewritten by a context-dependent handler. The
  allowlist is EXPLICIT and DEFAULT-DENY: ADMIT exactly hash / passthrough / redact /
  truncate / FPE (read-only probes confirmed these five have no hidden sibling/
  positional dependency, full-column == projected-column incl duplicates). DECLINE
  everything else (config-detectable by strategy type + declared outputs): `derived`/
  expression strategies that read payload siblings (`_derived.py:62`), any POSITIONAL /
  row-index strategy, `when:`-conditional KEYS, any overlapping/multi-column writer
  touching a cyclic key, and Faker/categorical (eligibility is config-dependent, not
  name-based) - no "and the like" default-admit. The allowlist is on the PRODUCER; the
  CONSUMER (matched-child inheritance) stays strategy-agnostic.
- NO cyclic table carries a table-level TRANSFORM (filter / sort / dedup / derived
  column, `_transforms_table.py`). Config-only detection over the whole nonempty
  `transforms` declaration (rev-2 HIGH-1, gate-confirmed sound): a transform can
  drop/reorder/dedup rows based on PAYLOAD columns, which a narrow key-only pass would
  not reproduce (a projected post-transform contract is the later, bigger alternative).
- the JOB does not enable row-error QUARANTINE (rev-3 HIGH-2 + rev-4 MED). Quarantine is
  a JOB-level control (`enabled` + `triggers`, `config/_validators.py:78`), NOT just an
  orphan-policy setting: payload errors and cascades populate quarantine under PRESERVE/
  REMAP/WARN too, and the entries hold whole masked rows incl payload that accumulate
  across ALL tables (`quarantine.py:184`, `_sequential.py:503`). So decline the cyclic
  route whenever the job enables any row-error quarantine trigger (config-only,
  conservative, covers surrounding acyclic tables too) - this is what makes omitting
  `quarantine_entries` from the memory formula valid job-wide. Orphan `FAIL` raises
  directly (`_fk_resolve.py:121`): it is an ABORT, not accumulation, so it is allowed.
- **EVERY WRITER of a retained cyclic key must be FULLY PROJECTED in pass 1 (rev-5
  BLOCKER-1).** Pass 1 projects columns from the COMPLETE dependency/writer closure of
  every retained key, INCLUDING incoming composite-FK groups. A composite-FK group
  (e.g. `P0.(p,q) -> X.(id,extra)`) writes the cyclic key `X.id` but its node columns
  extend beyond the internal-cycle projection; if pass 1 skips that writer, the key
  stays RAW and the cycle inherits raw -> masking + RI divergence from full-frame on an
  "admitted" job. v1 cut: DECLINE any cyclic component where a retained key is written
  (wholly or partly) by an incoming composite-FK group that is not fully projectable.
  NEVER silently skip a writer of a retained key.
- **NO unbounded/hard-to-price PAYLOAD strategy on ANY cyclic-component table (rev-5
  BLOCKER-3/4, radical narrowing).** DECLINE a cyclic component when any payload column
  on its tables uses a pool/corpus/generator-backed or nested strategy -
  `faker` (`_faker.py:89` builds the WHOLE pool to `pool_size` BEFORE the cache cap
  applies - a `person_email` `pool_size=1e8` is an 800MB array under a 512MiB "budget"),
  `code_set` (whole corpus resident), `composite` (config-sized bundle pool outside
  PoolCache), and ANY `nested(strategy=...)` wrapping one of these. Resolve NESTED /
  transitive dispatched strategies with the SAME child-resolution semantics as execution
  (rev-5 BLOCKER-3: `nested(strategy="code_set")` must be rejected, not priced as
  PoolCache-backed) - inspect the transitive strategy set, not just the outer name.
  This removes every unbounded-memory payload from the admitted set so the bound is
  trivially defensible; those configs keep running on full-frame.
- the WHOLE-JOB COMBINED memory estimate fits (see the memory contract); else decline.
Native-vs-Python stays the usual per-column type decision and is a speedup only (a
Python-fallback producer still emits a correct effective map). Also decline OOC cyclic
jobs. Self-reference is not a table cycle (handled by D2b drain-fold) - out of scope;
full-frame unchanged.

## Established solutions

- Column-topological masking of a table-cyclic FK graph is what full-frame already
  does; D2-cycle does the same ordering but retains only the narrow key columns across
  the component instead of all wide frames. No fixed-point iteration (the column graph
  is acyclic, so one pass in column order suffices).
- `_fk_resolve.resolve_fk_keys` is reused UNCHANGED, but must be supplied correct
  effective maps + error caches (not raw source-membership sets) - the gate confirmed
  it is reusable "once supplied correct maps". `_parent_map` is the reference for what
  an effective map contains (last-wins duplicates, error exclusion).
- The D1a hash kernel + the redact/truncate/passthrough handlers mask the parent key
  columns natively (that is the native speedup); the child inheritance is a map lookup
  (no kernel). D2b's kernel seam is reused for the native key masking; D2-cycle adds
  NO matched-key re-derivation (rev-1's mistake).
- Native vs Python for a parent key column is a per-column decision and a SPEEDUP only;
  a Python-fallback producer still emits a correct effective map. The native path
  applies to the strategies that have a kernel (hash via `decline_reason(column,
  input_type)` inside the hash dispatch, `_sequential_native.py:181`, plus the D2b
  redact/truncate/passthrough handlers); it is NOT a universal native gate for every
  admitted strategy (rev-3 LOW). The kernel WITNESS is scoped to the columns that
  actually ran native; an admitted pure-value-function producer with no kernel masks in
  Python and is proven by the ordinary oracle-parity assertion, not the kernel marker.

## Design (column-order effective-map, SEQ)

When routing detects a cyclic FK component (needs a real detector, see Routing):

1. **Pass 1 - effective key maps in COLUMN-dependency order (narrow).** Order the FK
   KEY columns of the cyclic component by the acyclic COLUMN graph. For each parent
   key column in that order, read ONLY that column's narrow source data, mask it with
   its strategy (native where admitted), and record the EFFECTIVE source->masked map:
   the actual emitted value per source key (token / raw-if-when-off /
   preserved-raw-orphan), with errored-parent rows excluded into the cascade cache and
   duplicate source keys resolved last-wins. Build the map in SOURCE ORDER so last-wins
   matches the oracle's retained mapping. The effective map reproduces the oracle's
   CONSUMER map (what a matched child inherits); it is NOT used to restore a standalone
   producer key column's own per-row output (rev-4 LOW): distinct source values can
   normalize to the same FK key (e.g. truncate `[-0.0, 0.0]` emits `["-0.0","0.0"]` but
   both normalize to key `0`, `_fk_keys.py:90`), so in pass 2 a STANDALONE producer key
   is EXECUTED from its original values at its normal work position - the consumer map
   feeds FK consumers only. Because admission restricts producers to pure value-function
   strategies, the key-only read in pass 1 carries every input the producer needs (no
   payload sibling, no row position), so the consumer map it builds equals what
   `_parent_map` produces on the full frame. A child key column that depends on an
   already-computed parent map consumes it in order.
2. **Pass 2 - mask each cyclic table.** Load each table, mask its payload columns
   normally (D2a/D2b per-column decisions), and resolve its FK child keys by INHERITING
   from the pass-1 effective maps via `resolve_fk_keys` (map lookup + the unchanged
   EXCLUDE-then-CASCADE-then-policy precedence, REMAP re-running the parent handler as
   today). Evict each table after masking.

Memory contract (rev-2 HIGH-2 + rev-3 HIGH-2 fix - peak over the WHOLE component
schedule, accounting for ALL run-retained state, not just the largest table). Existing
SEQ retains a parent's map until ALL its consumers finish (`_sequential.py:529`), and
the admitted mixed topology includes incoming acyclic parents feeding the cycle and
outgoing children consuming it. Peak at any point =
- (every live parent map + its error cache still retained), where "retained" means
  until the LAST consumer across BOTH passes AND outgoing children finishes - not merely
  pass-1 key work (rev-3 HIGH-2; `_sequential.py:301` consumer bookkeeping adapts); PLUS
- (the component's retained narrow key maps/error caches); PLUS
- (the cross-table error state `all_row_errors` + `key_error_rows`, `_sequential.py:443`
  - keyed by errored CELLS, worst-case every row across many columns, so priced from the
  error-trigger config, NOT assumed negligible, rev-4 MED); PLUS
- (the per-table WORKING SET of EVERY table in the JOB, not only the cyclic component -
  rev-5 BLOCKER-2. The whole job runs one table at a time but each table's masked output
  Arrow table + live pandas frame + conversion scratch must fit alongside the retained
  cyclic maps. A DISCONNECTED acyclic table is still part of the job: a 1M-row boolean
  column hashed produces a ~68MB Arrow table even though the cyclic tables are tiny, so
  pricing only the cyclic tables' source bytes (the rev-4 bug: 2-3x `table_bytes[cyclic]`)
  under-prices by orders of magnitude. Price the DECODED input width + the STRATEGY-
  DEPENDENT OUTPUT WIDTH (a hash/redact of a 1-byte bool emits a wide string; output can
  be far larger than source) + conversion/scratch overlap + the table-transition overlap
  (eviction deletes `frames[table]` but local `df`/`out` persist, `_sequential.py:518` -
  release explicitly or price) + variable-width retained keys, for every table; DECLINE
  any config whose output/working-set size has no defensible bound),
under lazy inputs + a draining sink. `quarantine_entries` (whole masked rows,
`quarantine.py:184`), `PoolCache`/generator pools (`pool/_cache.py:69`, `_faker.py:89`
builds the full pool BEFORE the cache cap), `code_set` corpora, `composite` bundle pools,
and `nested(...)` wrapping any of these are NOT in this formula because the admission
gate DECLINES every job that uses them (above, rev-5 BLOCKER-3/4) - so retained state is
trivially bounded (narrow key maps + bounded-width per-table working sets). GOVERNING
RULE (rev-4, reaffirmed rev-5): PRICE EVERY LIVE ALLOCATION OR DECLINE, and price from
real decoded/output allocation sizes NOT synthetic source-byte arithmetic - the bullet
list is the known set; any resident or phase-local allocation whose bound is unavailable
makes the job decline. The plan
defines explicit RELEASE points (drop a map/error-map pair only after its last consumer
across pass-1 work, pass-2 work, AND outgoing children - `_sequential.py:301` table-only
consumer sets need this adaptation) and a COMBINED-budget admission check that reserves
all the resident terms above and rejects when the estimate exceeds budget, with an
explicit decline when any term's bound is unavailable (decline, do not assume it fits).
The win is jobs whose AGGREGATE frames exceed RAM while this combined live state fits; a
single oversized cyclic table, or a combined estimate over budget, declines. Pass 1
needs a PROJECTED key-columns-only read; the current loader reads the whole file
(`_pipeline_sources.py:126`, `_readers.py:270`), but PyArrow `read_table`/
`ParquetFile.read` expose `columns=` (gate-confirmed), so a real projected read is a
buildable PREREQUISITE (build it, or decline opaque whole-table loaders - do not read
the whole frame and select after, which violates the narrow-pass contract).

Canonicalization (rev-1 H1 fix): pass 1 stores the parent's EFFECTIVE OUTPUT keyed by
the SAME normalized FK key the oracle uses (`_fk_keys.py:75`, which treats `True`==`1`),
and pass 2 INHERITS by that same normalized lookup. So canonicalization is inherited
from the oracle unconditionally - the `True`==`1` divergence cannot arise because there
is no re-derivation and no second normalization path. No per-edge canonicalization
admission test is needed (rev 3 drops rev-2's "value-keyed set" phrasing; it conflated
key normalization with masking strategy).

Routing (rev-1 H4 fix): add a real strongly-connected-component detector over the
table FK graph (the current detector returns only a bool), topologically order the
component graph (acyclic tables in topo order; each cyclic component via the two-pass),
and wire cycle admission through BOTH explicit-SEQ and the automatic bounded-routing
branch (lift the cycle decline for ADMITTED cycles on SEQ only). Preserve OOC's
independent cycle rejection and the self-edge exclusion.

## Pinned guarantee and non-goals

**Guarantee.** An admitted SEQ cross-table cycle masks byte-identically to full-frame
(strict dimensions); RI/orphan/cascade/duplicate/policy match full-frame exactly
(same `resolve_fk_keys` precedence, same effective-map semantics); peak memory is the
combined live state (retained narrow key maps + per-cell error caches + the WHOLE-JOB
per-table bounded-width working set), checked against budget at admission from REAL
decoded/output allocation sizes. Non-admitted cycles decline with a clear coded reason:
`when:`-conditional-key / non-pure-value-function-producer (derived-sibling-read,
positional) / overlapping-multi-column-key-writer / Faker-categorical-key /
incoming-composite-FK-writer-of-a-cyclic-key (rev-5) / unbounded-payload-strategy on a
cyclic table (faker/code_set/composite/nested-wrapping-those, rev-5) /
inherited-key-dependency / oversized-single-table / transform-bearing-cyclic-table /
quarantine-triggers-enabled / whole-job-working-set-over-budget / work-node-cyclic. OOC
cyclic jobs decline. Acyclic masking and full-frame unchanged.

**Non-goals.** No re-derivation of matched keys. No fixed-point iteration. No OOC
cyclic support. No projected post-transform contract for transform-bearing cyclic tables
(they decline this cut). No support for sibling-reading/positional key PRODUCERS in a
cycle (they decline; CONSUMER inheritance stays strategy-agnostic). No quarantine-row
accounting in the bound (quarantine jobs decline). No pool/corpus/generator-backed or
nested payload strategy on a cyclic table, and no PoolCache/code_set/composite pricing
(those configs decline, rev-5 - the bound covers only narrow key maps + bounded-width
per-table working sets). No incoming composite-FK writer of a cyclic key (rev-5). No
change to full-frame, `_fk_resolve` precedence, or acyclic routing.

## Acceptance tests (authored before impl; include the rev-1 counterexamples)

- **Parity:** 2-table and 3-table cycles, SEQ byte-identical to full-frame on strict
  dimensions; RI asserted both directions. Cover a RANGE of pure-value-function key
  PRODUCERS (hash native, redact/truncate/passthrough) to prove the producer set is not
  silently narrower than stated; AND a case where the matched CHILD's own configured
  strategy differs from the producer's, proving the child INHERITS the producer's
  effective value rather than running its own strategy (consumer stays
  strategy-agnostic, rev-3).
- **The rev-1 counterexamples MUST pass identically to full-frame:** (a) an INCOMING
  acyclic hash parent with `when:` false stays RAW and its matched child inherits that
  RAW value (admitted only if the key is unconditional; if the KEY itself is
  `when:`-gated it declines - test both, per rev-3 LOW); (b) the column chain
  `A.id->B.mid->A.back` where an orphan preserved at `B.mid` stays raw and `A.back`
  inherits the raw value (the column-acyclic table cycle supported today) - the
  effective map carries the preserved-raw value, NOT a re-hash.
- **The rev-3 counterexample MUST decline (not silently mis-mask):** the cycle
  `A.id->B.mid->A.back` with `A.id` using a `derived`/expression strategy that reads a
  payload sibling (e.g. `payload + 100`), no table transforms, work-graph acyclic - all
  the other admission conditions pass, but a key-only read of `A.id` cannot evaluate the
  expression, so the component DECLINES on the pure-value-function-producer condition
  (config-detectable). Also: a positional/row-index producer with duplicate source keys
  declines (a last-wins map would drop distinct per-row outputs).
- **Canonicalization:** a boolean parent `True` matched by an integer child `1`
  inherits the parent's effective token (map lookup), byte-identical to full-frame;
  numeric normalization + duplicate-normalized-key last-wins covered.
- **Orphan/cascade/policy:** preserve/remap/warn + errored-parent cascade through a
  cyclic edge, resolved from the pass-1 effective maps + error cache, identical to
  full-frame. Orphan `FAIL` RAISES (abort, no commit), identical to full-frame - it is
  allowed because it does not accumulate (rev-4 MED). A job that enables row-error
  QUARANTINE triggers DECLINES the cyclic route (not a successful case), per the
  admission gate.
- **Standalone producer vs consumer map (rev-4 LOW):** a producer key with distinct
  source values that normalize to the same FK key (e.g. truncate `[-0.0, 0.0]`) emits
  its own per-row values in pass 2 (executed from originals, byte-identical to
  full-frame), while the matched child inherits the last-wins consumer value - assert
  both, proving the consumer map is not used to restore the producer column.
- **Memory:** a cyclic job whose AGGREGATE frames exceed a set budget but whose
  COMBINED live state (all retained parent maps + error caches + component narrow maps +
  cross-table error state + PoolCache + the current table's working set) fits runs on
  SEQ within the bound; a single oversized cyclic table declines (true memory limit); a
  mixed topology (incoming acyclic parent + cyclic component + outgoing acyclic child)
  whose COMBINED estimate exceeds budget declines even though no single table is
  oversized (rev-2 HIGH-2); an admitted run asserts each parent map is RELEASED only
  after its last consumer ACROSS BOTH PASSES AND outgoing children (peak stays bounded,
  not monotonically growing, rev-3 HIGH-2). **Quarantine:** a multi-table job that
  enables row-error quarantine triggers DECLINES the cyclic route (it would accumulate
  whole masked rows across tables, rev-3 HIGH-2). **Payload-strategy state:** a cyclic job
  with ANY faker/code_set/composite/nested-wrapping-those payload column on a cyclic table
  DECLINES outright (rev-5 BLOCKER-3/4 - these build unbounded pools/corpora, not priced);
  an error-heavy config whose per-cell error caches exceed budget DECLINES.
- **rev-5 BLOCKER reproducers (the Codex final-gate counterexamples) MUST now be handled
  correctly - these are REQUIRED, with REAL allocation/output sizes not synthetic
  arithmetic:**
  - **BLOCKER-1 composite-FK writer (parity/decline):** the job `P0.(p,q)->X.(id,extra)`
    (incoming COMPOSITE FK writing `X.id`), cycle `X.id->Y.mid->X.back`, hash producers,
    PRESERVE. Full-frame hashes `X.id`/`Y.mid`/`X.back`. The admitted-today build emitted
    RAW `"x"` in `Y.mid`/`X.back` (pass 1 skipped the composite writer) - this MUST now
    either mask byte-identically to full-frame OR DECLINE (v1 cut declines a cyclic key
    written by a not-fully-projectable incoming composite-FK group). Assert through
    validated config + compilation + `run_pipeline`, not just a unit stub.
  - **BLOCKER-2 whole-job working set (decline):** a small admitted cycle PLUS a
    DISCONNECTED 1M-row boolean column hashed, under a ~67MB budget. The job produces a
    ~68MB Arrow table for the disconnected column alone; admission MUST DECLINE (the bound
    prices every table's real output width, not cyclic source bytes). A real
    allocation/output-size acceptance check, not size arithmetic.
  - **BLOCKER-3 nested code_set (decline):** a cyclic job with
    `nested(target="$.c", strategy="code_set", ...)` on a payload column MUST DECLINE
    (transitive strategy resolution), not run as PoolCache-priced.
  - **BLOCKER-4 faker pool build (decline):** a `person_email` payload with a large
    `pool_size` on a cyclic table MUST DECLINE BEFORE the pool is built (the pool is
    constructed to full size before the cache cap applies); assert the decline fires at
    admission, before generation.
- **Declines (fail closed, coded, no data in the message):** a `when:`-conditional
  KEY cycle; a non-pure-value-function PRODUCER cycle (derived/sibling-reading, and
  positional/row-index - rev-3 HIGH-1, config-detectable by strategy type + inputs); a
  cyclic key column written by an OVERLAPPING/MULTI-COLUMN writer, e.g. a payload
  `joint_mask` that lists it (rev-4 MED - eligibility checked against every actual
  writer, not just the key's own declared strategy); a Faker/categorical cyclic key
  (default-deny, config-dependent); an INCOMING COMPOSITE-FK group writing a cyclic key
  that is not fully projectable (rev-5 BLOCKER-1); a cyclic table with an unbounded
  PAYLOAD strategy - faker/code_set/composite/nested-wrapping-those, transitive
  resolution (rev-5 BLOCKER-3/4); an inherited-key-dependency cycle the pass can't
  resolve; a cyclic table carrying a table-level TRANSFORM (filter/sort/dedup/derived -
  rev-2 HIGH-1); a job that enables row-error QUARANTINE triggers (rev-3 HIGH-2 +
  rev-4 MED, job-wide); a whole-job working set over budget (rev-5 BLOCKER-2); a component
  whose WORK-NODE dependency graph cycles (not just the table graph); an OOC cyclic job
  (unchanged). Each decline is detectable at config/route time without inspecting row data.
- **Routing:** an incoming acyclic parent + a cyclic component + an outgoing acyclic
  child + a disconnected acyclic table all in one job order and mask correctly;
  self-reference still routes as today (not treated as a table cycle).
- **Witness / native evidence:** a KERNEL-backed pass-1 producer (e.g. hash) is proven
  on the kernel via the D2b marker witness; a no-kernel pure-value-function producer is
  proven by oracle parity instead (rev-3 LOW - the witness is scoped to columns that ran
  native, not asserted for every producer); inheritance is a map lookup (no kernel).
- **Acyclic unchanged:** every existing acyclic + full-frame-cycle test passes;
  full-frame still masks small cycles. **Module size** under caps (new
  `execution/_sequential_cycle.py`); **NFC** re-assert; **perf gate** on a sized cyclic
  workload (capability-first: it runs bounded at all; native speed on the key columns).

## Known context for the gate

- Revises the program plan's KEEP-CURRENT/V2 cycle disposition to "v1, SEQ bounded,
  unconditional pure-value-function key producers, no table transforms and no
  quarantine-producing policies on cyclic tables, full-retained-state combined-budget
  admission, column-order effective-map"; land the program-plan update with this slice.
  The admitted set the gate confirmed viable: unconditional, transform-free,
  non-quarantining hash/passthrough/redact/truncate chains forming table cycles.
- Depends on D2b; rebase onto post-D2b main before build. D2b provides a native kernel
  SEAM only; D2-cycle adds the component detector, the column-order effective-map pass,
  the projected read (or its prerequisite), and the routing wiring.
- "Attempt, defer if too hard" (Cam): if the effective-map-in-column-order pass or the
  projected read proves larger than a bounded slice, narrow further (more declines) or
  defer to V2 and record where it stopped.

## Docs to update

`docs/compatibility-contract.md` (SEQ masks admitted cross-table cycles; the decline
narrows), CODEMAP for the new module, the program-plan cycle disposition.
