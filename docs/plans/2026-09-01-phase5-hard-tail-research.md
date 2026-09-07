Status: proposal

# Phase 5: The Hard Tail

## 1. Executive summary

Phase 5 is the residue of the engine-efficiency program. Earlier phases move
row-local, keyed, and bounded-state strategies off the pandas full-frame oracle.
Phase 5 contains strategies whose current semantics depend on global order,
whole-dataset counts, data-dependent output types, nested child dispatch, an ML
runtime, or customer Python whose behavior the engine cannot inspect. The master
plan deliberately keeps this set on the oracle and does not promise full native
cutover (`docs/plans/2026-08-26-engine-efficiency-plan.md:35-49`,
`docs/plans/2026-08-26-engine-efficiency-plan.md:686-690`).

This report applies the stricter Phase 4 acceptance rule: a migrated strategy must
reproduce the current pandas implementation's exact seeded values, row order, null
placement, warnings, and row errors. It must not substitute a different deterministic
algorithm merely because that algorithm streams
(`docs/plans/2026-08-31-part2-phase4-plan.md:13-21`). The master plan separately
allows enumerated physical Arrow or Parquet representation differences
(`docs/plans/2026-08-26-engine-efficiency-plan.md:40-43`). "Exact" below means the
observable strategy result, not identical Parquet metadata.

The overall verdict is selective, not a full-port program. Text with NER is already
bounded on the out-of-core route and is a small native-port and inference-batching
job. `derived` is tractable once the execution plan can project sibling columns and
resolve an output schema. `geo_generalize`, `formula`, `grouped_series`, and exact
`shuffle` need real external-memory machinery. `nested` needs a second execution
stack over shredded JSON leaves. Unrestricted Python providers cannot be made
provably bounded or deterministic by the engine. For a self-hosted, single-org
product capped near 100 million rows, it is worth building shared prepass and spill
primitives, then activating only strategies with measured customer demand. Several
rare strategies may never justify leaving the permanent oracle.

### Difficulty ranking

Sizes include correctness work, route integration, and exact-parity evidence. They
assume Phase 4 delivers its durable global row number and external sorter where
noted.

| Rank | Strategy | Size | Verdict in one line |
|---:|---|:---:|---|
| 1 | `text` / NER | S | Already batch-local and present on the OOC route; batch model inference and port the route. |
| 2 | `derived` (Lark) | M | Pure per-row evaluation; needs sibling projection and a declared or discovered output type. |
| 3 | `geo_generalize` | L | Counts are a clean two-pass aggregation, but exact per-row warning evidence is itself unbounded. |
| 4 | `formula` | L | A sequential RNG can cross chunks, but output typing and exact replay require a rewindable two-pass route. |
| 5 | `grouped_series` walk | L | Needs an exact pandas-compatible external order, per-group sequential state, and order restoration. |
| 6 | deterministic `shuffle` | XL | Exact NumPy permutation needs O(n) disk plus external gather and restore; keyed sort is not equivalent. |
| 7 | `nested` JSON | XL | Requires parse, shred, child-route dispatch, diagnostic remap, and exact JSON reassembly. |
| 8 | arbitrary Python providers | XL | No generic mechanism can prove bounded state, schema, or determinism for arbitrary code. |

The ranking is not a suggested build order. Usage evidence and shared dependencies
control sequencing in section 4.

## 2. Per-strategy deep dive

### `formula`

#### The blocker

The mask-side handler passes the entire Series to `FormulaStrategy.apply`
(`src/decoy_engine/execution/_strategies/_formula.py:28-38`). That transform creates
one `random.Random` from `sha256(column_name + "|" + formula_text)` and exposes the
same instance as `randint`, `choice`, and `random` to every non-null row
(`src/decoy_engine/transforms/formula.py:39-57`). A row consumes a variable number of
draws according to its expression and value. Null rows consume none. The draw-site
catalog therefore marks the stream order-dependent and `partitionable=False`
(`src/decoy_engine/execution/native/_determinism_protocol.py:861-884`). The result
type is also arbitrary and data-dependent, so the native compiler cannot produce a
fixed Arrow schema (`src/decoy_engine/execution/native/_capabilities.py:294-300`,
`src/decoy_engine/execution/native/_requirements.py:267-278`).

The exact contract is:

- seed with the current formula and column hash, independently of the mask key;
- visit rows in original positional order;
- skip every value for which pandas `isna` is true without advancing the RNG;
- expose the same CPython `Random` instance to every evaluated row;
- preserve Python `safe_eval` results and pandas' final whole-column dtype inference;
- raise the same first evaluation error at the same row.

Generation formula is a different draw site. The inline generation path reseeds per
row and should be considered with streaming generation, not used as a replacement
for the mask formula contract (`src/decoy_engine/generation/synthesize.py:528-562`).

#### Existing methods/prior art

Stateful stream processors carry compact operator state across input batches. Python
itself exposes `Random.getstate()` and `setstate()` for exact PRNG checkpoints
([Python `random` documentation](https://docs.python.org/3/library/random.html)).
Iterator UDF systems such as Spark `mapInPandas` process bounded pandas batches but
require an explicit output schema
([Spark `mapInPandas`](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.DataFrame.mapInPandas.html)).
The state-carrying pattern is directly copyable. Spark's distributed execution is
not needed and its schema contract needs adaptation to Decoy's existing untyped
formula.

#### Implementation concept

Use a sequential Python operator inside the new prepass coordinator, not a SQL or
Arrow expression rewrite. Snapshot a rewindable projection of the source column.
On pass 1, initialize the current RNG, evaluate rows from global row zero, and build
a bounded type summary while recording the first error. On pass 2, reinitialize the
same RNG and evaluate from row zero again into fixed-schema output batches. Because
the source snapshot and evaluator are unchanged, the second pass repeats the exact
variable draw sequence. A crash can restart both passes from zero. Durable RNG
checkpoints are optional unless Phase 5 also adds mid-column resume.

The hard part is matching pandas' construction of a Series from the complete Python
result list. The safe initial admission should add an `output_type` rule field as a
validation assertion, not as a cast. The prepass verifies every value against the
declared type and fails closed on mismatch. Untyped formulas remain on the
oracle until a differential, bounded pandas-type reducer exists. A dense Arrow union
can represent mixed values in a spill file
([Arrow columnar union layout](https://arrow.apache.org/docs/format/Columnar.html)),
but it does not by itself define the final pandas dtype or a portable Parquet schema.

This belongs on a new prepass/OOC route. It should reuse `safe_eval` and the existing
draw-site provider, not lower formulas to DuckDB. SQL lowering could change Python
coercion, exception, and floating-point behavior.

#### Difficulty + steps

Size: **L**.

Primary risks are pandas dtype inference, `pd.NA` and `NaN` behavior, variable RNG
consumption, first-error ordering, Python and Faker version drift inside the formula
scope, and double-evaluation cost.

1. Define a typed-formula admission shape and reject untyped formulas on the bounded route.
2. Add formula referenced-column projection if the mask-side formula surface expands later.
3. Implement a rewindable sequential evaluator with the current RNG and null-skip rules.
4. Add pass-1 type validation and pass-2 fixed-schema emission.
5. Differentially test varying batch boundaries, null markers, branch-dependent draw counts, mixed outputs, and errors against the oracle.
6. Measure two-pass CPU cost and spill on a real large formula workload before activation.

#### Recommendation

**Defer until evidence.** Build only a typed subset after a customer has a large
formula workload. Keep existing untyped formulas on the permanent oracle. Do not
relax numerical parity. Reusing the Python evaluator makes exact floats achievable;
a SQL or vectorized lowering would need a separate Cam-approved numerical contract.

### `derived` (Lark expression)

#### The blocker

`derived` is not global. The handler iterates rows, creates a same-row context from
the entire DataFrame tuple, and calls a pure Lark evaluator
(`src/decoy_engine/execution/_strategies/_derived.py:52-68`). The output can be a
number, string, boolean, null, or list
(`src/decoy_engine/expressions/_lark_parser.py:217-226`). The current OOC kernel sees
one column at a time and does not project expression-referenced siblings, while its
fixed output schema cannot represent an analytically unknown result type
(`src/decoy_engine/execution/out_of_core/_compat.py:105-109`). The native requirements
resolver currently projects only named configuration keys such as `group_by` and
`order_by`, not Lark column references
(`src/decoy_engine/execution/native/_requirements.py:251-264`).

The determinism contract is simpler than formula: compile the same closed grammar,
provide the same source-row sibling values, apply the same null-propagation and bounds
rules, evaluate in input order for error attribution, and preserve the exact Python
result and final dtype. No RNG exists
(`src/decoy_engine/transforms/derived.py:249-293`).

#### Existing methods/prior art

Database and dataframe engines normally solve this with a typed expression plan:
project referenced columns, evaluate per record batch, and emit a declared schema.
Spark's iterator UDF interface is a representative bounded-batch design with an
explicit schema
([Spark `mapInPandas`](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.DataFrame.mapInPandas.html)).
Arrow supports primitive, nested, and union values under explicit schemas
([Arrow columnar format](https://arrow.apache.org/docs/format/Columnar.html)). The
batch and projection pattern is copyable. Lowering Lark expressions to a different
engine is not copyable under exact Python semantics.

#### Implementation concept

Teach native planning to extract `_get_column_refs` from the compiled expression and
add those columns to the input projection. Keep `apply_derived` as the only evaluator.
For the bounded route, support either:

1. an explicit `output_type` assertion checked against every result, or
2. a two-pass type reducer followed by deterministic reevaluation.

The first is substantially smaller and should be the initial slice. The second must
match pandas list-assignment inference across every allowed Lark result, including
lists and mixed `case_when` branches. It should not serialize arbitrary Python
objects with pickle. The route is chunked for the typed case and uses the prepass
coordinator only for inferred types.

No floating-point relaxation is necessary if the Python evaluator remains in use.
Any future Arrow or SQL lowering must prove exact arithmetic and exception parity or
request an explicit scoped relaxation similar to the `derived_aggregate` float bar.

#### Difficulty + steps

Size: **M** for a declared-type subset, **L** for transparent support of every current
untyped expression.

Risks are sibling projection order, pandas scalar conversion, `None` versus `NaN`,
mixed branch types, nested list values, integer-to-float promotion, and first-error
row numbering.

1. Extend native requirements with Lark-derived input projections.
2. Define and validate a closed `output_type` vocabulary.
3. Run `apply_derived` over record batches with the durable global row number for diagnostics.
4. Reconcile results to the asserted schema without changing scalar values.
5. Differentially test every grammar result family, mixed branches, null modes, bounds, and batch boundaries.
6. Consider inferred-type support only after typed usage demonstrates demand.

#### Recommendation

**Defer until evidence**, then build the declared-type subset. It is a clean bounded
port, but low traffic does not justify reproducing all pandas type inference in
advance. Leave untyped and mixed-list cases on the oracle.

### `nested` JSON

#### The blocker

The current handler parses every cell, runs a JSONPath, flattens every matched leaf
in the whole column into one Python list, calls a dynamically selected child handler
once on a temporary pandas DataFrame, remaps child errors to outer rows, writes the
masked leaves back, and serializes with `json.dumps`
(`src/decoy_engine/execution/_strategies/_nested.py:1-18`,
`src/decoy_engine/execution/_strategies/_nested.py:293-310`,
`src/decoy_engine/execution/_strategies/_nested.py:404-468`,
`src/decoy_engine/execution/_strategies/_nested.py:470-515`). The single child call
preserves child whole-column semantics. The wrapper also has a global fail-closed
rule: a path that matches no parseable cell in the whole column is an error
(`src/decoy_engine/execution/_strategies/_nested.py:380-402`).

The exact contract includes positional handling of duplicate pandas indexes, current
JSONPath match order with deepest-first overlap handling, malformed-cell row errors,
global all-zero-match rejection, child warning and row-error order, child RNG and
global semantics, and Python `json.dumps` output. The wrapper itself has no RNG, but
its effective determinism and partitionability are the selected child's properties
(`src/decoy_engine/execution/native/_capabilities.py:323-338`).

#### Existing methods/prior art

Analytical engines parse JSON into typed `LIST` and `STRUCT` values, unnest them to
rows, transform those rows, then aggregate or reassemble. DuckDB documents both
JSON-to-nested conversion and recursive `unnest`
([DuckDB JSON transformation](https://duckdb.org/docs/stable/data/json/json_functions.html),
[DuckDB unnest](https://duckdb.org/docs/stable/sql/query_syntax/unnest.html)). Arrow
defines stable nested array layouts and record-batch schemas
([Arrow columnar format](https://arrow.apache.org/docs/format/Columnar.html)). The
shred, transform, and reassemble pattern is copyable. DuckDB's schema-driven JSON
functions are not a drop-in replacement for arbitrary `jsonpath-ng` matches or exact
`json.dumps` behavior.

#### Implementation concept

Implement a bounded nested coordinator with two streams:

- a cell stream keyed by durable outer row number; and
- a leaf stream keyed by `(outer_row_number, leaf_ordinal)` with the original match order.

Pass 1 parses cells in chunks, evaluates the existing `jsonpath-ng` expression,
records the global matched-any flag, and writes leaf rows to Parquet. It can discard
parsed objects after each batch because pass 2 reparses the source snapshot. The
leaf stream then runs through the normal execution planner as a synthetic single
column. Admission must be the intersection of nested support and the child's bounded
capabilities. Pass 2 scans source cells and masked leaves in key order, repeats the
same match ordering, applies existing writeback guards, remaps diagnostics, and calls
the same `json.dumps`.

This does not make arbitrary nested children streamable. A child such as shuffle or
formula brings its own prepasses and ordering contract. The first nested slice should
allow only child strategies already proven batch-local with static output types.
The unrestricted wrapper remains on the oracle.

#### Difficulty + steps

Size: **XL**.

Risks are leaf cardinality explosion, JSON scalar type preservation, JSONPath
overlaps, duplicate outer indexes, malformed cells, exact global no-match behavior,
child dtype and RNG semantics, child diagnostics, ordering during reassembly, and
spill estimates for adversarial JSON.

1. Define a leaf-state schema and analytic disk bound from rows, cell bytes, and maximum matches.
2. Implement pass-1 parse and shred with existing JSONPath and overlap helpers.
3. Add child-capability composition and admit only static, bounded children.
4. Execute the leaf stream through the existing chunked/OOC dispatcher.
5. Implement ordered pass-2 reparse, error remap, writeback, and `json.dumps` reassembly.
6. Differentially test sparse paths, all-zero paths, malformed JSON, overlaps, root matches, duplicate indexes, and batch boundaries.

#### Recommendation

**Defer until evidence.** If activated, build only the bounded-child subset. Treat
unrestricted nested dispatch as **permanent-oracle** because its difficulty is the
union of every possible child strategy.

### `text` / NER

#### The blocker

The original full-frame handler loads a cached spaCy pipeline, then calls it once per
non-null cell (`src/decoy_engine/storm/ner.py:116-157`,
`src/decoy_engine/execution/_strategies/_text_mask.py:115-145`). The model package
version is stamped into the plan and a runtime mismatch fails closed
(`src/decoy_engine/execution/_strategies/_text_mask.py:78-113`). NER is contextual,
so the detected entity type can change the downstream Faker method, but the span mask
itself stays keyed by matched text
(`src/decoy_engine/transforms/text_mask.py:7-25`).

This is no longer a whole-frame algorithmic blocker. Both `text_mask` and
`text_redact` already run NER per cell on OOC record batches
(`src/decoy_engine/execution/out_of_core/_mask_group_c.py:15-25`,
`src/decoy_engine/execution/out_of_core/_mask_group_c.py:138-186`,
`src/decoy_engine/execution/out_of_core/_mask_group_b.py:186-214`). The remaining
gap is native non-FK routing and inefficient one-cell-at-a-time inference.

The exact contract is the same ordered span list for each cell under the same model,
model package version, spaCy runtime, entity filter, and text. It also includes the
same leftmost-then-longest merge with regex spans, null preservation, non-string
coercion, per-span keyed output, and original row order. Batch boundaries must not
change spans.

#### Existing methods/prior art

spaCy's `Language.pipe` consumes a stream, buffers a configurable batch, and yields
documents in input order; its documentation calls it more efficient than one-by-one
calls ([spaCy `Language.pipe`](https://spacy.io/api/language/)). Microsoft Presidio's
NLP engine exposes `process_batch`, and its analyzer and anonymizer have explicit
batch surfaces
([Presidio Analyzer](https://microsoft.github.io/presidio/analyzer/),
[Presidio Anonymizer](https://microsoft.github.io/presidio/anonymizer/)). GLiNER also
has `batch_predict_entities`, but adopting it would change the model and therefore the
span contract
([GLiNER repository](https://github.com/urchade/GLiNER)). Batched inference is
directly copyable. Replacing the current model is not.

#### Implementation concept

Add `iter_ner_spans_batch(texts, model, entities, batch_size)` that uses the cached
pipeline's `nlp.pipe(..., n_process=1)`. Return one span list per input cell in the
same order. Keep regex detection, overlap resolution, and masking in the existing
functions. Use the helper from the full-frame, OOC, and future native handlers so
there is one model path.

Bound inference by both row count and total UTF-8 bytes or token estimate, since a
fixed number of very large cells is not a fixed memory bound. Keep `n_process=1` in
the exact route because multiple worker processes duplicate model memory and add a
second ordering/runtime dimension. Pin the model package, spaCy version, execution
device, and relevant model configuration in route evidence. A differential gate must
prove `nlp.pipe` returns the same spans as `nlp(text)` for the supported model pack.

This is a chunked/native port. It does not need a global prepass or state table.

#### Difficulty + steps

Size: **S**.

Risks are model/runtime drift, batch-dependent padding or truncation, GPU numerical
variation, model `max_length`, cell byte spikes, row-order association, and overlap
parity after combining regex and NER spans.

1. Extract a shared batch NER helper around `Language.pipe`.
2. Add row-and-byte batch limits and keep ordered single-process inference.
3. Switch the existing OOC and oracle handlers to the shared helper without changing span consumers.
4. Port the same helper to native non-FK text handlers.
5. Differentially test span bytes, labels, masking output, nulls, and batch sizes against one-cell inference.

#### Recommendation

**Build**, after confirming a large non-FK NER workload exists. This is the lowest
risk Phase 5 slice and corrects an outdated master-plan classification. If all real
NER jobs already use the OOC FK route or stay small, defer the native port and keep
the batching helper as a performance-only opportunity.

### `geo_generalize`

#### The blocker

ZIP generalization computes complete ZIP5, ZIP3, and state count maps before deciding
any row (`src/decoy_engine/transforms/geo_generalize.py:327-374`). H3 generalization
materializes every row's cell at every configured resolution, then complete count maps
for each resolution (`src/decoy_engine/transforms/geo_generalize.py:504-558`). A chunk
cannot know whether a key meets `k_threshold` in the whole dataset.

There is a second, less obvious blocker. The handler creates one warning whose detail
contains a `row_i -> cascade decision` entry for every row generalized below the top
level (`src/decoy_engine/execution/_strategies/_geo_generalize.py:75-91`). That warning
can itself grow O(n). Exact output values can be bounded with a count prepass, but the
current in-memory warning contract cannot be both exact and memory-independent.

The exact contract includes current ZIP digit extraction, HHS restricted-prefix rule,
state lookup, H3 conversion at each configured level, cascade order, suppression of
parse failures, input order, and the per-row cascade evidence. There is no RNG.

#### Existing methods/prior art

The natural database method is a two-pass aggregate: derive the generalization key,
`GROUP BY key` to count equivalence classes, then join the count table during the
transform pass. DuckDB documents disk spilling for larger-than-memory `GROUP BY`,
sort, join, and window operators
([DuckDB larger-than-memory workloads](https://duckdb.org/docs/stable/guides/performance/how_to_tune_workloads)).
H3 explicitly defines unique parent cells at coarser resolutions
([H3 hierarchy API](https://h3geo.org/docs/api/hierarchy/)). Both patterns are
directly copyable.

The Mondrian literature frames k-anonymity as equivalence classes and proposes a
greedy multidimensional partitioning algorithm
([LeFevre, DeWitt, and Ramakrishnan, 2006](https://pages.cs.wisc.edu/~lefevre/MultiDim.pdf)).
It is useful product context if Decoy later generalizes multiple quasi-identifiers
together. It is not a drop-in implementation here because it would choose different
generalizations and violate current output parity.

#### Implementation concept

Add a `geo_count_cascade` prepass. For ZIP, stream `(zip5, zip3, state)` projections
into DuckDB and materialize one compact count table per level. For H3, compute cells
for configured resolutions per input batch and aggregate `(resolution, cell, count)`.
Then rescan the source, look up counts, and run the existing row cascade logic. Use
the existing `temp_directory`, `memory_limit`, and thread controls from
`connect_duckdb` (`src/decoy_engine/execution/out_of_core/_duckdb.py:27-59`).

The output pass belongs on the prepass/OOC route. It must not replace the cascade
with Mondrian. Restricted ZIP3 logic and the static ZIP-to-state table remain in
Python unless an exact differential test proves a SQL lowering.

The warning contract needs a product decision. The bounded design should write
per-row cascade evidence to a durable Parquet side table and return a bounded summary
plus artifact reference. That is observably different from the current nested warning
dict. Without permission to change this evidence surface, `geo_generalize` cannot be
strictly memory-bounded.

#### Difficulty + steps

Size: **L** under the current warning contract, **M** after an approved bounded
evidence contract.

Risks are null and string coercion, H3 version drift, state-map versioning, count-key
collation, restricted-prefix logic, duplicate resolution work, warning ordering, high
cardinality spill, and disk exhaustion.

1. Freeze ZIP and H3 key derivation vectors and reference-data versions.
2. Add rewindable prepass registration and count-table schemas.
3. Implement DuckDB `GROUP BY` materialization with explicit memory and disk budgets.
4. Stream the second-pass cascade using exact Python row logic.
5. Add a bounded evidence side table after Cam approves the warning contract change.
6. Prove exact output and evidence decisions across batch sizes, then measure 100M-scale spill.

#### Recommendation

**Defer until evidence and a warning-contract decision.** The algorithm is worth
building if a large geo workload exists because it is conventional two-pass work. It
must stay on the oracle if exact in-memory per-row warning detail remains mandatory.

### Arbitrary Python providers

#### The blocker

The provider protocol offers scalar `generate` and list-returning `generate_batch`
methods (`src/decoy_engine/providers_v2/_adapter.py:124-170`). A custom Faker provider
is an unconstrained callable invoked with a mutable Faker instance
(`src/decoy_engine/providers_v2/_faker_adapter.py:130-158`,
`src/decoy_engine/providers_v2/_faker_adapter.py:245-263`). Customers can also replace
a registry binding with a custom adapter (`src/decoy_engine/providers_v2/_registry.py:121-135`).
Such code can close over global state, allocate in proportion to all rows, perform
I/O, emit variable types, depend on call grouping, or ignore Decoy's seed.

The engine therefore classifies unknown or unrecognized providers as `reject_large`,
and only recognized poolable backends can be `pool_native`
(`src/decoy_engine/execution/native/_provider_class.py:68-105`). There is no generic
byte-identical contract beyond "call the same customer code in the same sequence."
Even that does not make output reproducible if the provider is nondeterministic.

#### Existing methods/prior art

Distributed dataframe systems do not infer safe streaming behavior from arbitrary
Python. They define a bounded iterator interface and require an output schema. Spark
`mapInPandas`, for example, maps an iterator of bounded pandas batches to an iterator
of batches under an explicit schema
([Spark `mapInPandas`](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.DataFrame.mapInPandas.html)).
Stateful processing systems require state to be declared and managed explicitly
([Apache Beam stateful processing](https://beam.apache.org/documentation/programming-guide/)).
These contracts are copyable. Their distributed runtime is not.

#### Implementation concept

Do not attempt to bound arbitrary callables. Define a new opt-in
`StreamingProviderAdapter` contract with:

- Arrow RecordBatch input and output plus a fixed schema;
- declared maximum retained state and temporary disk;
- declared partitionability and row-order sensitivity;
- an exact seed and null-consumption contract;
- optional explicit checkpoint/restore state;
- a batch-size-independent parity suite supplied by the provider author.

Run conforming providers in a bounded worker process so the memory governor can kill
an over-budget implementation without corrupting the parent process. This creates a
new, auditable provider class and an explicit OOC provider route. It does not change
the classification of arbitrary Python. A provider that cannot satisfy the contract
remains `reject_large` above the priced oracle limit and `python_only` below it.

#### Difficulty + steps

Size: **XL** for the SDK and isolation boundary. Unrestricted providers remain
unbounded by definition.

Risks are dishonest capability declarations, hidden process-global state, schema
drift, nondeterminism, side effects, serialization, exception identity, memory outside
the Python allocator, and compatibility obligations for third-party code.

1. Freeze the batch, schema, state, determinism, and error protocol.
2. Add a capability class distinct from existing arbitrary/custom providers.
3. Build a bounded worker-process bridge with Arrow IPC and hard resource accounting.
4. Provide a provider-author conformance kit with batch-boundary and fresh-process parity tests.
5. Keep admission fail-closed when any declaration or evidence is missing.
6. Activate only for a named provider with a real large workload.

#### Recommendation

**Permanent-oracle** for arbitrary Python, with `reject_large` enforcement. Build the
explicit streaming-provider SDK only for a demonstrated customer provider. Calling
the SDK result "arbitrary Python support" would be misleading because its value is
the restrictions it imposes.

### Deterministic `shuffle`

#### The blocker

The handler extracts non-null values in source order, computes one seeded NumPy
permutation of their complete length, writes permuted values back only to the original
non-null positions, and forces object dtype
(`src/decoy_engine/execution/_strategies/_shuffle.py:37-65`). The exact seed is
`derive(mask_key, namespace, column)[:8]`, interpreted big-endian, and the operation is
`np.random.default_rng(seed).permutation(non_null_count)`
(`src/decoy_engine/execution/native/_determinism_protocol.py:112-136`).

A partition cannot know the global non-null count or which source value its output
slot receives. Null positions must not move or consume permutation slots. The
nondeterministic mode uses an unseeded generator and has no cross-run exact-output
contract.

#### Existing methods/prior art

External merge sort generates sorted in-memory runs and k-way merges them with a
fixed memory buffer. GNU `sort` exposes the same controls through stable sort, merge
fan-in, buffer size, and temporary directories
([GNU Coreutils `sort`](https://www.gnu.org/software/coreutils/manual/html_node/sort-invocation.html));
the textbook reference is Knuth, *The Art of Computer Programming*, volume 3,
section 5.4. Keyed-random external sorting and format-preserving index permutations
are established bounded shuffle substitutes.

They are not exact replacements for Decoy. NumPy defines integer permutation as a
permuted `arange(n)` and the current generator uses PCG64
([NumPy permutation](https://numpy.org/doc/stable/reference/random/generated/numpy.random.permutation.html),
[NumPy random generator](https://numpy.org/doc/stable/reference/random/generator.html)).
Sorting rows by a keyed random value produces a different permutation. The Phase 4
research therefore correctly refines the master plan's earlier "external sort with a
deterministic key" shorthand: keyed sort is allowed only if Cam approves an output
contract re-freeze (`docs/plans/2026-08-31-part2-phase4-plan.md:108-121`).

#### Implementation concept

For exact deterministic shuffle:

1. Pass 1 assigns a non-null ordinal and writes `(source_row_nr, non_null_ordinal, value)` plus null positions to Parquet.
2. Allocate an int64 file-backed array of length `n_non_null`, initialize it to `arange(n)`, and apply the same pinned PCG64 permutation operation.
3. Convert the permutation to `(source_non_null_ordinal, destination_non_null_ordinal)` records.
4. External-sort those records by source ordinal, merge-scan source values to attach each value, then external-sort by destination ordinal.
5. Merge the shuffled non-null stream back into original row positions and emit object-equivalent values.

This uses O(n) disk and bounded RAM but performs substantial I/O. Before committing
to `shuffle` rather than in-place `shuffle` on a memmap, a golden must prove the exact
NumPy call consumes identical draws and swaps on the pinned NumPy version. The
external sorter must have stable binary keys and explicit tiebreakers.

This is an OOC-only route. A keyed-sort variant is much simpler, but it is a new
strategy contract, not an optimization of the existing strategy.

#### Difficulty + steps

Size: **XL**.

Risks are NumPy version drift, permutation-call parity, non-null ordinal drift,
object scalar serialization, null markers, duplicate values, order restoration,
temporary disk near multiple times column size, and pathological random I/O.

1. Freeze exact permutation vectors across supported NumPy versions or pin one version.
2. Prove the file-backed permutation call is identical to `rng.permutation(n)`.
3. Build non-null ordinal staging and the two external gathers.
4. Restore null positions and object-compatible output in original row order.
5. Add disk preflight, cancellation cleanup, and route evidence.
6. Run parity and resource gates at realistic width and null density.

#### Recommendation

**Defer until evidence.** The exact method is buildable but expensive. For a rare
strategy on a single-org product, full-frame oracle plus a priced row limit is likely
the right permanent answer. If customers need huge shuffles and accept a new
permutation, ask Cam to approve a versioned keyed-sort strategy instead of silently
changing `shuffle`.

### `grouped_series` walk

#### The blocker

The transform copies group and order columns, attaches original row position, sorts
by `(group_by, order_by)`, walks each group, and writes results back to original order
(`src/decoy_engine/transforms/grouped_series.py:165-202`). `cumcount` is arithmetic on
the within-group position (`src/decoy_engine/transforms/grouped_series.py:205-233`).
`monotone_walk` derives one PCG64 generator per stringified group label, emits `start`
for the first row, then advances the generator once per row and accumulates the next
step (`src/decoy_engine/transforms/grouped_series.py:236-281`). Splitting a group
across batches loses both its ordinal and cumulative RNG state. The draw catalog
marks it `partitionable=False`
(`src/decoy_engine/execution/native/_determinism_protocol.py:356-378`).

The exact contract is the pandas sort order, including ties and nulls; original row
position restoration; `str(group_label).encode(..., errors="replace")` seed material;
one per-group PCG64 stream; and the current first-row and post-row draw timing. The
current `sort_values(..., kind="stable")` call has a subtle risk: pandas documents
that `kind` is honored only for single-column DataFrame sorts
([pandas `sort_values`](https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.sort_values.html)).
Exact multi-key tie behavior must be observed, not assumed.

#### Existing methods/prior art

SQL expresses the durable ordinal as
`row_number() over (partition by group order by order_key, stable_tiebreak)`.
DuckDB supports that form and can spill blocking windows
([DuckDB window functions](https://duckdb.org/docs/stable/sql/functions/window_functions.html),
[DuckDB larger-than-memory workloads](https://duckdb.org/docs/stable/guides/performance/how_to_tune_workloads)).
The general exact mechanism is external stable sort followed by a single sequential
scan. Both are copyable patterns.

DuckDB ordering is not automatically copyable for Decoy. SQL's type comparison,
null placement, and peer ordering must match pandas exactly. Phase 4 therefore calls
for Decoy's comparator-controlled external sorter and treats DuckDB `ROW_NUMBER` as a
prototype only unless a differential proof admits a particular dtype/config shape
(`docs/plans/2026-08-31-part2-phase4-plan.md:105-107`,
`docs/plans/2026-08-31-part2-phase4-plan.md:122-125`).

#### Implementation concept

Use the Phase 4 external sorter to order rows by an exact canonical encoding of
`(group, order, original_row_nr)`. The original row number is the stable tiebreak.
Scan the sorted stream while holding only the current group, counter, cumulative
value, and current group RNG. Emit `(original_row_nr, result)`, then externally sort
by original row number for output.

Admission must be dtype- and null-policy-specific. Start with types whose external
comparison has been differentially proven equivalent to the pinned pandas version.
`cumcount` can be admitted separately because it has no RNG. `monotone_walk` then
adds the existing per-group draw provider. Mixed Python object columns and unproven
null-group cases stay on the oracle. A DuckDB `row_number` implementation may be used
only for a shape whose exact equivalence is proven.

This is a sorted OOC subroute behind the prepass coordinator. It is not a normal
chunked transform because the sorted scan and final order restoration are blocking.

#### Difficulty + steps

Size: **L**, or **M** after Phase 4 supplies the sorter and comparator catalog.

Risks are pandas multi-key tie behavior, null groups, mixed object comparison,
stringification of group labels, PCG64 version drift, draw timing, integer overflow,
and final order restoration.

1. Freeze differential fixtures for group/order dtype pairs, ties, nulls, and duplicate indexes.
2. Define admitted canonical sort encodings and reject everything else.
3. External-sort by group, order, and durable source row number.
4. Implement bounded `cumcount`, then exact per-group `monotone_walk` state.
5. External-sort results back by source row number and emit fixed integer output.
6. Prove parity across run sizes, merge fan-in, and batch boundaries.

#### Recommendation

**Build only if Phase 4 leaves it unfinished and a real workload needs it.** This is
the best second consumer of the Phase 4 external sorter. Keep unproven dtype and null
shapes on the oracle permanently rather than broadening the comparator spec without
evidence.

## 3. Cross-cutting infrastructure

### 3.1 Rewindable prepass coordinator

Build this first in Phase 5. It unlocks more strategies than any other new primitive:
`geo_generalize` count tables, `derived` and `formula` type discovery, nested's
matched-any decision and leaf shredding, plus future bounded diagnostic reducers.
The master plan already reserves `required_prepasses` and disk-backed state tables in
the native plan (`docs/plans/2026-08-26-engine-efficiency-plan.md:59-71`).

The coordinator should:

1. snapshot a projected source once when the connector is not safely rewindable;
2. attach the durable global row number before any pass;
3. execute a DAG of named prepasses with declared input columns, state schema, memory, and disk estimates;
4. publish immutable state tables to the transform pass;
5. restart the job from row zero after interruption unless a later phase explicitly adds checkpoints;
6. feed the existing transactional sink only after all prepasses succeed.

The first implementation should remain single-host and sequential where order is
observable. Decoy does not need a distributed planner for its 100M-row cap.

### 3.2 Durable spill and state-table layer

Use Arrow RecordBatch and Parquet as the durable interchange. `ParquetWriter` already
supports incremental `write_batch`
([PyArrow `ParquetWriter`](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetWriter.html)).
The repo's `MaskedKeyStager` already tees narrow, fixed-schema batches to Parquet and
reopens them as `LazySource`
(`src/decoy_engine/execution/out_of_core/_stage.py:33-68`). Generalize this pattern into
typed state tables with:

- a fixed schema and version;
- row count and byte accounting;
- source snapshot identity;
- automatic cleanup on abort;
- explicit sensitivity classification because spills contain raw or derived customer data;
- optional keyed indexes implemented as sorted Parquet, not in-memory dictionaries.

Use DuckDB for equality `GROUP BY` and joins where output order is restored explicitly.
Reuse `connect_duckdb` for private temp directories, memory limits, thread limits, and
spill settings (`src/decoy_engine/execution/out_of_core/_duckdb.py:27-59`). Do not
assume its `memory_limit` covers every allocation; its own documentation calls out
larger-than-memory limitations and blocking-operator interactions.

### 3.3 Deterministic external-sort primitive

Phase 4 should deliver this primitive. Phase 5 must reuse it for grouped ordering,
shuffle gather and restoration, nested leaf order, and any durable keyed lookup.
Required properties are:

- run generation under an explicit byte budget;
- k-way merge with bounded fan-in and file descriptors;
- stable ties through an explicit durable row-number tiebreak;
- comparator IDs tied to dtype, null, collation, and version;
- cancellation-safe temporary files and disk preflight;
- route evidence proving the sorter, not the oracle, ran.

DuckDB sort is acceptable for order-insensitive staging. It is not the default for a
pandas-order parity boundary unless a named shape has passed differential proof.

### 3.4 Batched-inference harness

Build one model harness shared by `text_mask`, `text_redact`, profiling, and future
detectors. It should cache one pinned pipeline per model, batch by rows and text bytes,
preserve input association, expose model/runtime/device provenance, and have a
single-process exact mode. A throughput mode using additional processes or devices
must be separately gated because it changes memory and possibly numerical behavior.

### 3.5 Bounded diagnostics

Phase 5 cannot claim bounded memory while returning O(n) warning dictionaries or
lists. Introduce a diagnostic reducer contract with three classes:

- bounded aggregate warnings returned inline;
- bounded samples plus exact counts returned inline; and
- exact per-row evidence written to a durable side table.

This is required for `geo_generalize` and useful for nested row errors. It is a public
observable change and needs Cam's approval before implementation.

### First primitive recommendation

Build the **rewindable prepass coordinator with the minimal typed state-table API
first**. It directly unlocks four hard-tail families and provides the control plane
for the sorter and inference harness. Reuse the OOC route's DuckDB and Parquet
staging instead of creating another spill backend. If Phase 4 has not delivered the
external sorter, finish that before `grouped_series` or exact `shuffle`, but do not
make every prepass depend on sorting.

## 4. Sequencing recommendation

Phase 5 should be evidence-gated strategy by strategy. Before activation, capture the
named customer recipe, row count, source width, current oracle peak RSS and time,
frequency, acceptable disk budget, and the business cost of leaving it capped.

### Recommended order

1. **Reconcile reality and measure usage.** Update eligibility documentation to note that OOC NER already streams. Collect strategy/config-shape telemetry without raw values.
2. **Text/NER batching and native port.** Activate only if large non-FK text jobs exist. It has no global dependency.
3. **Prepass coordinator plus typed spill tables.** Treat this as shared infrastructure with no promise that every consumer follows.
4. **`geo_generalize`, conditionally.** Activate after a real large geo job and approval for durable per-row evidence instead of an in-memory warning map.
5. **`derived` typed subset.** Activate for customers willing to declare output type. Keep transparent untyped inference held.
6. **`grouped_series`, if Phase 4 did not finish it.** Requires the external sorter and admitted comparator shapes.
7. **`formula` typed subset.** Activate only when two-pass Python evaluation is affordable for a demonstrated workload.
8. **Exact `shuffle`.** Activate only when the customer accepts its multi-pass O(n)-disk cost. Otherwise keep the oracle or define a new versioned keyed-sort strategy.
9. **`nested` bounded-child subset.** Build only if nested JSON is both common and too large for the oracle.
10. **Streaming-provider SDK.** Build for a named provider owner, never as speculative generic support.

### What can remain permanently on the oracle

- untyped, mixed-output formula and derived expressions;
- nested with a global or unknown child;
- nondeterministic shuffle configurations, which have no cross-run exact-output contract;
- mixed-object or unproven comparator shapes for grouped series;
- arbitrary Python callables and adapters without the new streaming contract;
- any strategy whose measured traffic fits the priced small-job oracle limit.

For a single-org deployment, this may be most of Phase 5. The oracle is an intended
permanent route, not technical debt that must be eliminated.

### Dependency graph

```text
Phase 4 DGRN --------------------+------------------> diagnostics / row attribution
                                 |
Phase 4 external sorter ---------+--> grouped_series
                                 +--> exact shuffle gather + order restore
                                 +--> nested leaf ordering

typed Parquet state tables ------+
                                 +--> rewindable prepass coordinator --> geo count cascade
DuckDB spill GROUP BY / joins ---+                                +--> derived type pass
                                                                  +--> formula type + replay
                                                                  +--> nested shred / reassemble

batched inference harness --------------------------------------------> text / NER

streaming-provider SDK is independent and evidence-gated per provider
```

## 5. Open questions for Cam

1. **Is Phase 4's strict exact-output rule still the Phase 5 gate?** If yes, keyed-sort shuffle is not an implementation of current `shuffle`; only the O(n)-disk exact permutation is eligible.
2. **May strategy configs add an `output_type` assertion?** This is the smallest bounded route for `derived` and `formula`. Without it, Decoy must reproduce pandas whole-column inference or keep them on the oracle.
3. **May exact per-row warning evidence move to a Parquet side artifact?** `geo_generalize` cannot be memory-independent while returning its current O(n) nested warning dict.
4. **Which Phase 5 strategies have real large-job usage?** Provide recipe, rows, width, frequency, current peak RSS/time, and business impact. No implementation should start without this proof.
5. **Is a new versioned keyed-sort shuffle acceptable as an opt-in strategy?** It is much cheaper than exact current shuffle but produces a different permutation.
6. **Which grouped dtype and null shapes are product-relevant?** A narrow admitted comparator catalog is safer than promising pandas equivalence for arbitrary object columns.
7. **Should typed and untyped formula/derived configs coexist?** Recommended answer: typed may stream; untyped stays oracle.
8. **Which nested child strategies must be supported?** Recommended first set: static-output, batch-local, no global diagnostics. Unrestricted child dispatch should remain oracle.
9. **Is changing the current retry model in scope?** The proposed coordinator restarts from row zero. Mid-pass resume adds durable RNG and operator checkpoints and should be a separate decision.
10. **Should arbitrary providers above the oracle limit be rejected or allowed to opt into a strict streaming SDK?** The engine cannot safely infer capabilities from arbitrary Python.
11. **What temporary-disk multiplier is acceptable per strategy?** Exact shuffle can require several column-width copies plus sort runs; nested can amplify one cell into many leaves.
12. **Which runtime dimensions must be pinned for ML exactness?** At minimum model package, spaCy version, execution device, and inference configuration should be part of the plan or route evidence.

The default recommendation is conservative: build the shared prepass/state layer,
take the small NER win when evidence supports it, and leave the rarest unrestricted
surfaces on the permanent oracle.
