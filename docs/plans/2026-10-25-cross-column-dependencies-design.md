# Cross-column dependencies in Decoy masking: the problem and how to handle it

Status: discussion (Phase 1). Purpose: understand the problem space before planning a build. This doc
does not pick an implementation; it frames the situations, maps each to how it should be handled, and
names the cross-cutting mechanics and open questions for the build-plan gate that follows.

## 1. The problem

Decoy masks each column independently. Columns that are logically linked lose their mutual consistency
after masking. A masked row should stay internally valid and plausible the way the source row was:

- AGE should match BIRTHDAY (if the birthday moves, age must follow).
- STATE/CITY/ZIP should stay a real co-located triple (not "Boston, TX, 90210").
- RACE/NATIONALITY (or make/model/year, zip3/state) should stay a plausible or valid combination.
- start_date should still precede stop_date; a derived total should still equal its parts.

The invariant is not "keep the original values" (de-identification deliberately changes them). It is
"keep the relationships between the columns true in the output."

## 2. Why independent masking breaks it (the structural facts)

From the engine today:

- Each column is transformed on its own; there is no general notion of one column depending on another.
- The execution ordering graph (`execution/_runner.py` `order_work`) builds dependency edges ONLY from
  the FK relationship graph (parent-before-child) plus composite-to-FK-child edges. It builds NO edge
  from a `derived` expression's column refs or a `when:` predicate's column refs. Non-FK columns in a
  table are ordered by a lexicographic tie-break, not by data dependency.
- `derived`/`case_when` and `group_key` do NOT reliably read source values. Their docstrings say source,
  but at runtime they evaluate over the CURRENT MUTABLE FRAME (`_pandas_adapter.py:386`, `_derived.py:63`,
  `_group_key.py:67`): a sibling masked by an EARLIER node is read already-masked. Which value they get is
  therefore ORDER-DEPENDENT, and the order between non-FK columns is a lexicographic tie-break
  (`order_work`), so it is semantically accidental: stable across runs (the tie-break is byte-stable), but
  governed by lexicographic column-name order rather than declared data dependency. Codex verified this at
  runtime (a derived column read the masked driver when the driver sorted first, and the source value when
  it sorted last). This is the key reframing for this feature AND a latent wrong-value (silent-semantics)
  bug in the engine today, not a reproducibility bug.
- `DeriveOp` is a different mechanism: a pre-mask table transform (`config/_transforms.py`) over source
  values, because transforms run before the mask loop. Do not conflate it with the `derived` strategy.
- The derivation and joint primitives (`derived`, `case_when`, `joint_mask`, `formula`, `geo_generalize`)
  run in the full-frame mode only. In AUTO mode the chunked/OOC routes do not admit them and the job FALLS
  BACK to sequential/full-frame (not an error); only an explicit `execution_mode` hard-rejects. So joint
  masking and derivation do not run at the streaming (100M) scale today.
- FK key propagation (`_pandas_adapter.py` parent source-to-masked map) is the one SPECIAL, deterministic
  masked-sibling read (same-key-to-same-token under an ordering edge). It is the model to follow, not the
  only masked read.

So the real gap is not "add a masked-sibling read": that already happens, accidentally. It is to make the
cross-column dependency EXPLICIT and DETERMINISTIC: an ordering edge from a dependent column to its drivers
(reusing the existing derived existence/cycle validation at `plan/_checks.py:652`, not a new one) so the
masked read is intentional and reproducible rather than a lexicographic accident; and, for scale, carrying
the derived path onto the OOC route (the chunked path already passes sibling context via the table chunk;
only the OOC per-column kernel lacks that channel).

## 3. A taxonomy of dependency situations

Each situation is a different shape with a different correct handling. Listing them separately is the
point: a single mechanism will not fit all of them, and some are already solved.

### 3.1 Referential identity (FK / join keys) — SOLVED
A child key must equal its masked parent key. Handled by `from_parent` / shared `namespace`
(same-source-key-to-same-token), ordered parent-before-child. This is the baseline and the one
DETERMINISTIC masked-sibling read (`derived`/`group_key` also read masked siblings, but only by
lexicographic accident, see section 2). Nothing to build; it is the proof that the engine CAN carry a
masked value across columns when it is ordered to.

### 3.2 Joint membership / valid-tuple consistency — MOSTLY SOLVED
A set of columns must form a valid combination: STATE/CITY/ZIP, RACE/NATIONALITY, make/model/year,
MCC/description. Correct handling: substitute the whole tuple with a valid one, drawn together, rather
than masking each column independently. Decoy already has:
- `coherent_with: [cols]` + composite generators. `composite_address` (city/state/zip) and
  reference-backed `joint_mask` emit a REAL reference-table row, so the tuple is relationally valid.
  `composite_custom` (1 to 4 column bundles) does NOT guarantee validity: its slots draw independently from
  separate pools, so a shared identity makes the bundle repeatable, not a valid combination
  (`generation/composite/_custom.py:47`). Use the reference-backed path when validity matters.
- `joint_mask` (columns + reference + key_by), which replaces a coupled set with a consistent reference
  row selected by the `key_by` column.
- Shipped reference tables (us_zip5_city_state, zip3-population, vehicle, MCC, NDC).
What it does NOT do: preserve the ORIGINAL combination (it substitutes a fresh valid tuple, normally fine
for de-id). And it does NOT guarantee selection off the source key: `joint_mask` reads `key_by` from the
CURRENT mutable frame, so if `key_by` was masked by an earlier node it selects on the MASKED key. That is
the same order-dependent read as `derived`/`group_key` (section 2), not a guaranteed source key. Scale caveat: `joint_mask` and the composite
generators run on the full-frame route only (audit), so joint consistency works at in-memory sizes but
not at the chunked/streaming scale the rest of the engine targets. The gaps here are: one clear
user-facing way to declare "these columns are a consistent group" that routes to the right primitive,
reference tables for the pairings a customer needs, and, for scale, a streaming-route path for the joint
primitives.

### 3.3 Functional derivation (computed column) — THE REAL GAP
A dependent column is a deterministic function of one or more driver columns, and must be computed from
the MASKED drivers: AGE = years_between(masked BIRTHDAY, as_of); a ZIP3 that must sit inside the masked
STATE; BMI = f(masked weight, masked height). Correct handling: mask the drivers first, then compute the
dependent from their masked values. Decoy has the expression engine for this (`derived` + `case_when`:
arithmetic, `days_between`, ternary, `concat`, `slice`), but it reads source values and is not ordered
after its drivers. It is also full-frame only (audit): `derived` is row-local in principle, so the
blocker to running it at scale is the fixed-schema streaming admission, not the computation. This
situation therefore needs three things: the ordering edge and the masked-driver read from section 2,
plus carrying `derived`/`case_when` onto the chunked and out-of-core routes with a determinable output
schema.

### 3.4 Conditional assignment / rules — a special case of 3.3
"B := value when A meets a condition", evaluated on the MASKED A. Example: ethnicity := "Hispanic" when
masked(state) == "MX". This is `case_when(driver == literal, value, default)` with a masked-driver read.
It is the same mechanism as 3.3; worth calling out because users think of it as a rule, not a formula.

### 3.5 Intra-row constraints / invariants — partly 3.3, partly new
Ordering (start_date <= stop_date after masking), bounds (a masked quantity within a plausible range),
and format-bearing links (a check digit that must match a masked account number). Some reduce to a
derived column (a clamp, or an ordering swap). A numeric checksum like mod-97 IBAN IS expressible in the
derived grammar: `//`, `*`, and `-` compute a remainder (`x - (x // 97) * 97`, verified to return 26 for
12345), and fixed-width digit calculations can be unrolled. General string-format checksums lack
convenient primitives. So excluding checksums from V1 is a SCOPE CHOICE, not an impossibility.
Uniqueness-within-a-group and hard ordering are the true constraints a per-column strategy cannot express
and that need a dedicated constrained step.

### 3.6 Aggregate / cross-row consistency — OUT OF SCOPE here
Totals that must equal their parts across rows, or distribution-level consistency. This is a different
problem (it is statistical and spans rows), overlaps the distribution-integrity program, and should be
named only to exclude it from this feature.

## 4. Cross-cutting mechanics the feature needs

These are shared by 3.3 and 3.4 (and the derivable part of 3.5):

- **A dependency DAG beyond FK.** Add edges from a dependent column to its declared drivers, fold them
  into `order_work` alongside the FK edges, and detect cycles (A depends on B depends on A) with a clear
  coded error. The FK ordering must still dominate where both apply.
- **Reading masked siblings, deterministically.** A dependent column's expression must bind to the
  driver's intended value under an EXPLICIT dependency order, not by lexicographic luck (section 2). The
  engine already has TWO deterministic precedents to reuse rather than fork: FK key propagation (a pre-mask
  source snapshot plus a source-to-masked map), and `date_shift` with group_by, which threads an immutable
  per-entity anchor through `group_anchor_snapshots` (the same FK-snapshot machinery) and fails closed on
  index mismatch (`_date_shift.py:19-45`). `date_shift` is the closest analog: a declared driver read
  through a snapshot under an ordering guarantee. On top of that, the build plan weighs two shapes: (a)
  order the driver first and read its masked value through such a snapshot, or (b) a distinct post-mask
  recompute stage. (a) reuses the existing pattern and avoids a second pass; (b) is easier when a driver is
  itself conditionally masked.
- **Determinism and seeding across the group.** The derived value must be reproducible; the group shares
  the job seed, as the composite and FK paths already do.
- **Route coverage is the hard constraint, not an afterthought.** The strategy-vs-route audit shows the
  primitives this feature reuses (`derived`, `case_when`, `joint_mask`, `formula`, `geo_generalize`) run
  on the full-frame route ONLY; none are admitted to the chunked or out-of-core routes, and only a fixed
  set of strategies have native kernels. So at in-memory sizes the feature is close; at the 100M streaming
  scale it does not run at all today. The functional and conditional cases (3.3, 3.4) are row-local in
  principle (they read same-row siblings), so the blocker is the fixed-schema streaming admission, not the
  computation: the core scale work is carrying `derived`/`case_when` onto the chunked and out-of-core
  routes with a determinable output schema, with the masked-driver read happening within the same chunk
  (safe, because driver and dependent share the row and the driver is ordered first). The joint case (3.2)
  is harder on the streaming routes because `joint_mask` carries reference-table state; it may stay
  full-frame for V1 or need a per-row keyed streaming variant. A native kernel that reads a sibling's
  masked column almost certainly does not exist; the honest fallback is to run the derived step in the
  Arrow/pandas layer and decline-with-reason on the native path rather than silently read a source value.
- **Interaction with the existing primitives (reuse, do not fork).** `coherent_with` forbids `when:`;
  `joint_mask`/composites select off their key column read from the CURRENT frame (so order-dependent like
  `derived`, not a guaranteed source read); FK propagation carries a masked value DETERMINISTICALLY under an
  ordering edge. The new "dependency declaration" should map onto these rather than introduce a parallel
  system: the joint case routes to `coherent_with`/`joint_mask`; the functional/conditional case reuses
  the `derived`/`case_when` expression engine plus the new ordering edge and masked-driver binding; geo
  links reuse the reference-table loader.

## 5. The shape of the user-facing feature (for discussion, not yet designed)

One declaration of a dependency group, which the engine compiles to the right existing primitive:
- a coherence group of columns with an optional reference table (routes to `coherent_with`/`joint_mask`);
- a derived/conditional column with an expression over named drivers, flagged to read the drivers' masked
  values (routes to `derived`/`case_when` + the new ordering edge).
The only genuinely new engine capability is section 4's ordering edge plus masked-driver read. Everything
else is wiring onto primitives that already exist.

## 6. Open questions for the build-plan gate and for Cam

1. Joint consistency: emit a fresh valid tuple (today's behavior) vs preserve the original combination via
   a consistent mapping. De-id usually wants the former; confirm.
2. Functional derivation: exact recompute (age exact from masked birthday) vs generalized (age bucket).
   Exact recompute can re-identify if the masked birthday is weak; note the privacy interaction.
3. Masked-read contract vs post-mask recompute stage (section 4) given OOC and native.
4. V1 vs V2 scope: 3.2 (joint) largely works and is a candidate to polish for V1; 3.3/3.4 (masked-driver
   derivation) is the new engine work and is the V2 candidate unless pulled forward.
5. Cycle and conflict resolution policy (coded error, or a defined precedence).

## 7. Failure modes to design against

Dependency cycles; a driver that is itself conditionally masked (its masked value is row-dependent); a
driver that is suppressed or dropped (the dependent has nothing to read); missing reference data for a
joint group; a chunk boundary (not a risk for row-local derivation, which is the point); a native route
that cannot read a sibling masked column (decline-with-reason rather than a silent source read).

## 8. Checkpoint-1 (dennis) findings to carry into the build plan

dennis gave checkpoint-1 GO and verified the engine claims (1a-1e) hold. These corrections and additions
must be folded into the Phase-2 build plan:

- Add `group_key` to the primitive inventory and to 3.2/3.4. It is the engine's existing sibling-keyed
  coherence primitive (`transforms/group_key.py`, SP-10c): it derives one consistent value for every row
  that shares a sibling column's value, reads that sibling's SOURCE value, and is ALREADY admitted on the
  chunked route (`CHUNK_SIBLING_KEYED_STRATEGIES = {"group_key"}`). It is the one coherence primitive that
  already streams, so the build must reuse it rather than duplicate it.
- Add the missing shape: cross-table denormalized parent-attribute propagation (a non-key parent attribute
  copied onto a child, e.g. order.customer_state must equal masked customers.state). 3.1 covers only the
  FK key; this stays consistent only if both columns share a namespace and strategy, and diverges if the
  parent attribute is masked via composite/joint/faker-pool. Name it and route it (to the FK/relationship
  program) or exclude it like 3.6.
- Make "must functional derivation run at streaming scale in V1" an explicit OPEN QUESTION in section 6,
  not a settled premise. It is the single biggest cost driver; the joint case is already allowed to stay
  full-frame for V1, so derivation should get the same open treatment.
- Streaming the derived case needs TWO new things, not one: a determinable output schema AND a new
  sibling-context channel into the per-column OOC/chunked kernel (`out_of_core/_mask_group_c.py` states the
  kernel "needs same-row sibling-column context the per-column mask kernel does not receive"). Name both.
- Precision fixes for the build plan: "refused on chunked/OOC" is only true under an explicit
  `execution_mode`; in AUTO mode these strategies are not admitted and the job FALLS BACK to full-frame
  (not an error). `DeriveOp` (a pre-mask table transform over source values) is a DIFFERENT mechanism from
  the `derived` masking strategy; do not list them as one primitive. Reconcile section 5's "wiring" with
  section 4's "hard constraint" (carrying derived onto streaming routes is substantial work, not wiring).
  Use the engine's real route names in the build plan.

## 9. Checkpoint-2 (Codex) corrections and added shapes

Codex ran the engine and corrected several claims; where this section conflicts with earlier text, this
section wins. Corrections already folded into the body: the order-dependent masked read (section 2, 3.1),
`composite_custom` is repeatable not valid (3.2), checksums are expressible so their exclusion is a scope
choice (3.5), reuse `plan/_checks.py:652` and "chunked already has sibling context" (section 2). Remaining:

- **Route model (correction):** the layer-1 routes are `full_frame`, `sequential`, and `out_of_core`;
  `sequential` processes tables one at a time. An unsupported OOC strategy falls back to SEQUENTIAL, not
  necessarily to the full-frame job route (`_pipeline_routing.py:34`). Use these names in the build plan.
- **Added shape: coordinated / interval-preserving transformation.** Some links are preserved by applying
  the SAME transform to a group, not by substitution or recompute: shift every date for an entity by one
  offset so the intervals between them survive. `date_shift` with `group_by` + a shared namespace +
  matching settings already does this off immutable SOURCE anchors (`_date_shift.py:19`). Add it to the
  taxonomy; it is distinct from 3.2 (fresh tuple) and 3.3 (recompute).
- **Added shape: constrained reference SELECTION.** "A ZIP3 compatible with the (held or masked) STATE" is
  not a deterministic formula (3.3) and not a fresh-tuple substitution (3.2): a state admits MANY valid
  ZIP3s, so this is selecting a candidate consistent with the retained driver. `joint_mask` today picks a
  whole reference row by keyed lookup and does NOT filter candidates against retained drivers
  (`joint_mask.py:285`), so this shape is unserved. Name it as its own situation in the build plan.
- These corrections "change the Phase-2 problem framing" (Codex), so the build-plan gate must start from
  this revised understanding, not the first-draft framing.
