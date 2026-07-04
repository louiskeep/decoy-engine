# Sprint G implementation guide: FK-aware subsetting engine core (SS1-SS5)

Status: implementation-ready design. Companion to
`docs/plans/2026-07-03-sprint-g-fk-subsetting.md` (the GATE-1-resolved plan, AUTHORITATIVE for
scope and the six GATE-1 decisions). This guide pre-makes every algorithm and interface decision
so the builder implements slice-by-slice with no design judgment. Where this guide and the plan
disagree, the plan wins; flag the conflict instead of improvising.

All file paths are relative to the engine repo root (worktree
`/home/cam/vscode/sprint-g-fk-subsetting`, branch `sprint-g/fk-subsetting`). Engine source lives
under `src/decoy_engine/`.

---

## 0. Verified primitive inventory (what this design is grounded on)

Everything below was verified against this worktree on 2026-07-03 (polars 1.42.1 in `.venv`).
The builder can rely on these facts without re-deriving them.

| # | Primitive | Location | Verified fact |
|---|-----------|----------|---------------|
| P1 | `PlanRelationship` / `PlanRelationshipEnd` | `src/decoy_engine/plan/_types.py:167-207` | Frozen dataclasses. `parent: PlanRelationshipEnd`, `children: tuple[PlanRelationshipEnd, ...]` (one-or-MANY child ends per relationship: expand to per-pair edges), `orphan_policy: OrphanPolicy` literal, `namespace: str | None`. `__post_init__` enforces non-empty children, non-empty parent columns, and equal column-tuple length parent-vs-child ("composite_columns_length_match"). It does NOT check column existence or types: that is exactly SS1's job. |
| P2 | Relationship graph | `src/decoy_engine/relationships/_graph.py` | `RelationshipEdge` (parent_table/parent_columns/child_table/child_columns/namespace/orphan_policy), `RelationshipGraph.edges` + `.ordering` (Kahn, heapq, byte-stable), `parents_of`/`children_of` O(edges) lookups. CRITICAL verified nuance: Kahn nodes are `(table, column_tuple)` pairs, NOT tables. A self-referencing table (`employee.manager_id -> employee.id`) and a mutual table cycle (A.id -> B.a_id, B.id -> A.b_id) both build fine (verified by running `build_relationship_graph` on exactly those shapes: 3 edges, valid ordering). Only a column-tuple-level cycle raises `PlanCompileError(code="fk_cycle")` (also verified). Consequence: cyclic fixtures for SS3 compile through the existing plan path; SS3 must do its own TABLE-level fixpoint and must NOT use `RelationshipGraph.ordering` as a traversal order (it is a mask order over column nodes, not a closure order over tables). |
| P3 | `run_fk_validity` | `src/decoy_engine/validation/post/_checks/_fk_validity.py` | Signature `run_fk_validity(ctx: ScanContext) -> ScanOutcome`. `ScanContext` (`validation/post/_scan.py:34-51`) requires `plan: Plan`, `outputs: dict[str, pa.Table]` (MASKED data), `sources`, `profile`, `registry: ProviderRegistry`, `relationship_graph`, `namespace_registry`. It is NOT directly callable pre-selection (no Plan/outputs/registry exist yet), so SS1 reuses its SEMANTICS, not the function: per edge, non-null child key with no parent match = orphan; null child key = neither match nor orphan; composite key with any null component = null (see `_row_keys`, line 88-97); FAIL policy -> hard fail, WARN -> warning, PRESERVE/REMAP -> pass. `FkValidityReport` fields (`validation/post/_types.py:36-45`): relationship, namespace, orphan_policy, child_row_count, parent_match_count, orphan_count, invalid_count. `FkPreflightReport` mirrors these (section 5). |
| P4 | Polars semi/anti join + lazy Parquet | verified by script | `pl.scan_parquet(path).collect_schema()` is schema-only (no row I/O). `.select([key_cols]).collect()` reads only key columns (projection pushdown). `join(..., how="semi")` works on composite keys via `left_on=[...], right_on=[...]`. Null join keys DO NOT match by default (join_nulls=False), matching P3's null semantics exactly. `with_row_index("__ri")` yields stable 0-based indices in file order; `filter(pl.col("__ri").is_in(sorted_list))` selects rows and preserves original order. Int32 parent vs Int64 child joins fine (supertype cast); String vs Int64 raises `SchemaError` at join time - hence the SS1 type-compat gate. NOTE: no production semi-join usage exists in the engine yet (grep confirms); the engine reads via `read_source_polars` (`execution/polars/_source_reader.py`) using `scan_parquet` + `collect`. The subset package introduces the first semi-joins; the polars API is verified above, so this is new-but-safe. |
| P5 | Determinism | `src/decoy_engine/determinism/_derive.py` | `derive(seed, namespace, source) -> bytes` (32-byte HMAC; recomputes HKDF per call - too slow per-row). `DeriveContext.for_column(seed, namespace)` + `ctx.derive_source(namespace, source)` precomputes the HKDF key: use this per-row (in-engine import from the private module is established practice, cf. `_fk_validity.py` importing `relationships._graph`). `job_seed_for_config(config) -> bytes` is public (`plan/_seed.py:111`, exported from `decoy_engine.plan`). Job seed is exactly 8 bytes. |
| P6 | Value canonicalization | `src/decoy_engine/generation/pool/_canonicalize.py` | `_canonicalize_source(value) -> bytes`: NFC-UTF8 for str, DER-style length-prefixed two's complement for int (incl. numpy scalars), ISO for date/UTC datetime, HARD ERROR (`GenerationError`) on float, tz-naive datetime, and None. LOAD-BEARING for the determinism envelope (SEED_PROTOCOL_VERSION). SS2 sampling reuses it verbatim (never reimplement). Consequence: float seed-key columns are unsupported -> SS1 fails closed on float key dtypes. |
| P7 | Config layer | `src/decoy_engine/config/` | `PipelineConfig` (pydantic, `extra="forbid"` everywhere) with `relationships: list[RelationshipConfig]`; `RelationshipConfig` = parent end + children ends + required `orphan_policy` + optional `namespace` (`config/_relationships.py`) - maps 1:1 onto `PlanRelationship`. `FileSource` has `format: Literal["csv","parquet"]` + `path` (`config/_sources.py`); `FileTarget` mirrors it. Cross-field enforcement pattern: `@model_validator(mode="after")` on `PipelineConfig` raising `ValueError` (see `_per_table_kind_consistency`). |
| P8 | Errors | `src/decoy_engine/errors.py`, `execution/_errors.py` | `DecoyError` base; typed errors carry `code=` + `message=` kwargs (cf. `ExecutionError`, `PlanCompileError(code=, path=, message=)`). Subset errors follow the same shape. |
| P9 | Chunked path rejects relationships | `src/decoy_engine/execution/_chunked.py:157` | `code="chunked_relationships_unsupported"`: FK jobs are full-frame today. GATE-1 #4: build against this; `_sequential.py` (Option 2 eviction) is NOT on this branch (verified absent) - design note in section 7.4 marks the plug-in point. |
| P10 | In-repo cycle-walk precedent | `src/decoy_engine/walks/hazards.py:_detect_cycles`, `config/_pipeline.py:_reference_graph_valid` | Iterative (explicit-stack) DFS is the repo's established recursion-safety pattern. The closure in this guide is round-based (no recursion at all), which satisfies the same concern by construction. |
| P11 | CI gate | `.github/workflows/ci.yml`, `docs.yml`, `pyproject.toml` | ruff==0.15.14 `check` + `format --check` over `src tests testflight scripts`; mypy==2.1.0 over `src/decoy_engine testflight`; `pytest tests -m "not benchmark"` with a routing guard over fixed suite dirs (new dirs are fine, do not rename existing ones); `sphinx-build -b html -W --keep-going docs` (myst_parser; a new .md under docs/ NOT referenced from a toctree fails -W - so do not add doc pages in SS1-SS5; barry's docs step owns that). Line length 100. RUF002/RUF003 enforce the no-em-dash/no-smart-quote rule in docstrings and comments. |

---

## 1. Package and module layout

New subpackage `src/decoy_engine/subset/` (GATE-1 #6: do NOT extend `plan/`). One config-layer
file is added under `config/` (SS5 only). Nothing else in the engine is modified except
`config/_pipeline.py` (one field + one validator) and `config/__init__.py` (export).

```
src/decoy_engine/subset/
    __init__.py        public surface ONLY: run_subset_preflight, plan_subset, run_subset,
                       relationships_from_config, subset_inputs_from_config, the public types,
                       and the error classes. Nothing else. Do NOT add subset exports to the
                       top-level decoy_engine/__init__.py in this sprint (SS6 CLI imports
                       decoy_engine.subset directly; keeps the frozen surface small).
    _types.py          frozen dataclasses: SubsetSource, SeedSpec, Predicate, EdgeDirection,
                       FanOutBudget, FanOutPolicy, SubsetEdge, EdgeStats, RoundTrace,
                       ClosureResult, TableEstimate, SubsetPlan, SubsetManifest, SubsetResult,
                       FkPreflightEdgeReport, PreflightFailure, FkPreflightReport.
                       (~250 LOC. Pure types + tiny __post_init__ guards; no I/O, no polars.)
    _errors.py         SubsetError(DecoyError) base with code/message; subclasses
                       SubsetPreflightError (carries .report), SubsetBudgetExceededError
                       (carries structured fields, section 4.4), SubsetConfigError,
                       SubsetInternalError. (~80 LOC.)
    _edges.py          PlanRelationship -> tuple[SubsetEdge, ...] normalization (expand child
                       ends, dedupe, sort, mint edge_id); relationships_from_config().
                       (~90 LOC. No I/O.)
    _preflight.py      SS1. Schema-level checks (scan_parquet().collect_schema(), zero row I/O)
                       + key-level orphan pre-scan over KeyFrames. Emits FkPreflightReport.
                       (~220 LOC.)
    _keys.py           the ONE key-column I/O boundary before SS5: load_key_frames() builds,
                       per table, a polars DataFrame of __ri + every key column any edge end or
                       seed spec needs (projection pushdown; never loads non-key columns).
                       (~90 LOC.)
    _seed.py           SS2. Seed selection: deterministic bottom-k HMAC sample, structured
                       predicate filter, explicit key list. Input: KeyFrames + SeedSpec tuple +
                       job_seed. Output: dict[table, set[row_index]] + per-table seed stats.
                       (~200 LOC.)
    _closure.py        SS3. THE NOVEL CORE. compute_closure() + downward_step() + upward_step()
                       + verify_closure(). Pure computation over in-memory key frames and
                       row-index sets; imports polars but performs NO file I/O; raises nothing
                       except SubsetInternalError (budget lives in _policy). (~250 LOC.)
    _policy.py         SS4. resolve_edge_directions(), check_budget() (round-level + final),
                       build_estimate() -> SubsetPlan. (~160 LOC.)
    _materialize.py    SS5. materialize_subset(): row-index filter per table -> Parquet writes.
                       (~120 LOC.)
    _manifest.py       SubsetManifest assembly + to_json_dict() (sorted, JSON-safe, NO raw
                       keys). (~120 LOC.)
    _api.py            orchestration: plan_subset() = preflight -> keys -> seed -> closure ->
                       budget -> estimate; run_subset() = plan_subset() innards + verify_closure
                       + materialize + manifest. subset_inputs_from_config() adapter. (~200
                       LOC. This is the only "orchestration" module; well under the 600-LOC
                       orchestration cap.)

src/decoy_engine/config/_subset.py     SS5. Pydantic SubsetConfig block (section 7.3).
tests/unit/subset/                     per-slice unit tests (section 8).
tests/integration/subset/              acceptance tests 1, 3, 5, 6, 7 (section 8).
```

Import discipline: `subset/*` may import `plan._types` (PlanRelationship),
`relationships._graph` (OrphanPolicy enum), `determinism._derive` (DeriveContext),
`generation.pool._canonicalize` (`_canonicalize_source`), `errors` (DecoyError), and polars.
It must NOT import `execution/*`, `validation/*`, or `profile/*` (library code does not know
its callers; the preflight reimplements P3's semantics rather than dragging in ScanContext).

### 1.1 Public entrypoints (exact signatures)

```python
# decoy_engine/subset/_api.py  (re-exported from decoy_engine/subset/__init__.py)

def run_subset_preflight(
    *,
    sources: Mapping[str, SubsetSource],
    relationships: tuple[PlanRelationship, ...],
) -> FkPreflightReport: ...
    # Never raises on a FAILED check: returns the report with failures recorded.
    # Raises SubsetConfigError only on malformed inputs (unknown table in a
    # relationship end is a preflight FAILURE, not a config error).

def plan_subset(
    *,
    sources: Mapping[str, SubsetSource],
    relationships: tuple[PlanRelationship, ...],
    seeds: tuple[SeedSpec, ...],
    policy: FanOutPolicy,
    job_seed: bytes,
    engine_version: str,
) -> SubsetPlan: ...
    # The dry-run. Runs preflight (raises SubsetPreflightError on failure), loads key
    # frames, runs seed selection + closure + budget evaluation. NEVER reads non-key
    # columns, NEVER writes anything. Budget violation -> SubsetBudgetExceededError.

def run_subset(
    *,
    sources: Mapping[str, SubsetSource],
    relationships: tuple[PlanRelationship, ...],
    seeds: tuple[SeedSpec, ...],
    policy: FanOutPolicy,
    job_seed: bytes,
    engine_version: str,
    output_dir: str | Path,
) -> SubsetResult: ...
    # = the plan_subset computation, then verify_closure (defensive integrity
    # assertion), THEN materialization + manifest. The budget gate sits strictly
    # before the first Parquet write (GATE-1 #3 hard-fail contract). Mandatory
    # dry-run-before-real-run at the engine level is satisfied by construction:
    # run_subset always computes the full estimate and budget-gates on it before
    # touching output_dir. The "user must SEE the estimate" flow is SS6/SS7 UX.

def relationships_from_config(config: dict[str, Any]) -> tuple[PlanRelationship, ...]: ...
    # Builds PlanRelationship tuples straight from a validated PipelineConfig dump's
    # `relationships` block (GATE-1 #5: the SAME declaration surface as masking; no new
    # surface). Wraps PlanRelationship __post_init__ ValueErrors into a
    # PreflightFailure-shaped SubsetPreflightError (code subset_relationship_composite_length)
    # so a half-declared composite reports cleanly instead of stack-tracing (section 5.2).

def subset_inputs_from_config(
    config: dict[str, Any],
) -> tuple[dict[str, SubsetSource], tuple[PlanRelationship, ...], tuple[SeedSpec, ...], FanOutPolicy]: ...
    # Adapter from a validated PipelineConfig dump (with the SS5 `subset:` block) to the
    # run_subset kwargs. This is what SS6's CLI and the platform runner call.
```

### 1.2 Data types (exact fields)

All frozen dataclasses, tuples not lists (matches `plan/_types.py` house style). Only the
non-obvious invariants are annotated here; the builder should copy the docstring discipline of
`plan/_types.py`.

```python
EdgeDirection = Literal["both", "downward", "upward", "none"]
SeedMode = Literal["sample", "filter", "keys"]
PredicateOp = Literal["eq", "ne", "lt", "le", "gt", "ge", "in", "is_null", "is_not_null"]

@dataclass(frozen=True)
class SubsetSource:
    path: str
    format: str          # must be "parquet"; preflight enforces (reject-with-guidance)

@dataclass(frozen=True)
class Predicate:
    column: str
    op: PredicateOp
    value: Any = None    # scalar for eq..ge; tuple for "in"; None for is_null/is_not_null

@dataclass(frozen=True)
class SeedSpec:
    table: str
    mode: SeedMode
    key_columns: tuple[str, ...] = ()          # required for sample + keys; () for filter
    fraction: float | None = None              # sample: 0 < fraction <= 1
    count: int | None = None                   # sample alternative; exactly one of the two
    predicates: tuple[Predicate, ...] = ()     # filter mode: AND-ed
    keys: tuple[tuple[Any, ...], ...] = ()     # keys mode: raw key tuples. NEVER serialized.
    # __post_init__: mode-field consistency (sample -> key_columns non-empty and exactly one
    # of fraction/count; filter -> predicates non-empty; keys -> key_columns and keys
    # non-empty, every tuple len == len(key_columns)). Raise ValueError with the field name.

@dataclass(frozen=True)
class FanOutBudget:
    max_total_rows: int | None = None           # cap on sum of surviving rows across tables
    max_table_seed_multiple: float | None = None
    # Per-table cap = ceil(max_table_seed_multiple * total_seed_rows) where total_seed_rows
    # is the SS2 grand total across all seeded tables (GATE-1 #3 wording: "per-table
    # multiple-of-seed-size cap"; global seed size, so unseeded tables get a well-defined
    # cap). Both None = uncapped (budget check no-ops but is still invoked).

@dataclass(frozen=True)
class FanOutPolicy:
    budget: FanOutBudget = FanOutBudget()
    edge_directions: tuple[tuple[str, EdgeDirection], ...] = ()  # keyed by edge_id; missing
                                                                 # edges default "both"
    allow_dangling: bool = False
    # GATE-1 #3: upward parent-completeness ON by default -> default direction "both".
    # Disabling upward on any edge can produce dangling FKs; _policy.resolve_edge_directions
    # raises SubsetConfigError(code="subset_dangling_not_acknowledged") if any edge resolves
    # to "downward" or "none" while allow_dangling is False. Unknown edge_id in
    # edge_directions -> SubsetConfigError(code="subset_unknown_edge") naming the id and
    # listing valid ids.

@dataclass(frozen=True)
class SubsetEdge:                  # normalized (parent, child) PAIR; built by _edges.py
    edge_id: str                   # f"{p.table}.{','.join(p.columns)} -> {c.table}.{','.join(c.columns)}"
                                   # (same format as run_fk_validity's `relationship` string)
    parent_table: str
    parent_columns: tuple[str, ...]
    child_table: str
    child_columns: tuple[str, ...]
    orphan_policy: str             # the OrphanPolicy literal value
    namespace: str | None

@dataclass(frozen=True)
class EdgeStats:
    edge_id: str
    direction: EdgeDirection               # resolved direction this run
    rows_added_downward: int               # cumulative child rows added via this edge
    rows_added_upward: int                 # cumulative parent rows added via this edge

@dataclass(frozen=True)
class RoundTrace:
    round_index: int                       # 1-based
    rows_added: int                        # total additions this round (0 only in the last)
    per_table_added: tuple[tuple[str, int], ...]   # sorted by table

@dataclass(frozen=True)
class ClosureResult:
    survivors: Mapping[str, frozenset[int]]   # table -> surviving row indices (0-based file order)
    rounds: int
    terminated_by: Literal["fixpoint"]        # always "fixpoint"; the field exists so tests
                                              # assert the exit path, not a timeout
    edge_stats: tuple[EdgeStats, ...]         # sorted by edge_id
    trace: tuple[RoundTrace, ...]

@dataclass(frozen=True)
class TableEstimate:
    table: str
    input_rows: int
    seed_rows: int                 # rows selected by SS2 (0 for unseeded tables)
    surviving_rows: int            # == len(survivors[table]); EXACT, not statistical
    seed_null_excluded: int        # sample-mode rows skipped for null key components

@dataclass(frozen=True)
class SubsetPlan:                  # the dry-run artifact (SS4 deliverable)
    engine_version: str
    seed_specs_public: tuple[Mapping[str, Any], ...]   # SeedSpec minus raw values: for keys
                                                       # mode only {table, mode, key_columns,
                                                       # key_count}; sample/filter serialized
                                                       # in full (predicates hold config
                                                       # values, not data, so they may appear)
    tables: tuple[TableEstimate, ...]                  # sorted by table
    edges: tuple[EdgeStats, ...]                       # sorted by edge_id
    closure_rounds: int
    budget: FanOutBudget
    budget_outcome: Literal["pass"]                    # a failed budget RAISES; a SubsetPlan
                                                       # only exists for passing runs
    total_surviving_rows: int
    preflight: FkPreflightReport
    warnings: tuple[str, ...]                          # e.g. zero-survivor tables

@dataclass(frozen=True)
class SubsetManifest:              # evidence artifact written by SS5 (counts/ids ONLY)
    manifest_version: int          # 1
    engine_version: str
    seed_specs_public: tuple[Mapping[str, Any], ...]   # same shape as SubsetPlan
    tables: tuple[TableEstimate, ...]
    edges: tuple[EdgeStats, ...]
    closure_rounds: int
    budget: FanOutBudget
    budget_outcome: Literal["pass"]
    preflight_summary: tuple[FkPreflightEdgeReport, ...]
    # to_json_dict() in _manifest.py: deterministic (sorted tuples already), JSON-safe
    # scalars only. INVARIANT (acceptance test 6): no raw key value from SeedSpec.keys or
    # from any table can appear anywhere in the serialized form.

@dataclass(frozen=True)
class SubsetResult:
    plan: SubsetPlan
    manifest: SubsetManifest
    output_paths: tuple[tuple[str, str], ...]   # (table, written parquet path), sorted

@dataclass(frozen=True)
class PreflightFailure:
    code: str                      # section 5 codes
    relationship: str              # edge_id, or "<table>" for table-level failures
    message: str                   # names the exact table/column/dtype problem

@dataclass(frozen=True)
class FkPreflightEdgeReport:       # parity with FkValidityReport (P3), pre-selection flavor
    relationship: str              # == edge_id
    namespace: str | None
    orphan_policy: str
    child_row_count: int
    non_null_child_key_count: int
    parent_match_count: int
    source_orphan_count: int       # non-null child keys absent from the FULL parent key set
    invalid_count: int             # == source_orphan_count when policy == "fail" else 0

@dataclass(frozen=True)
class FkPreflightReport:
    passed: bool
    failures: tuple[PreflightFailure, ...]
    warnings: tuple[str, ...]                       # WARN-policy orphan messages
    edges: tuple[FkPreflightEdgeReport, ...]        # sorted by relationship; empty when a
                                                    # schema-level failure prevented key scans
```

---

## 2. Edge normalization (`_edges.py`)

```python
def build_subset_edges(relationships: tuple[PlanRelationship, ...]) -> tuple[SubsetEdge, ...]:
    # 1. Expand: one SubsetEdge per (parent, child-end) pair (P1: children is a tuple).
    # 2. Dedupe on the full (parent_table, parent_columns, child_table, child_columns)
    #    identity, mirroring build_relationship_graph's dict.fromkeys dedupe (audit M1).
    # 3. Sort by edge_id. Deterministic forever after; every later loop iterates this order.
```

`relationships_from_config(config)` reads `config["relationships"]` (the validated
`PipelineConfig.model_dump()` shape, P7) and constructs `PlanRelationship` per entry:
`parent=PlanRelationshipEnd(table, tuple(columns))`, `children=tuple(...)`,
`orphan_policy=entry["orphan_policy"]`, `namespace=entry.get("namespace")`. Catch `ValueError`
from `PlanRelationship.__post_init__` and re-raise as
`SubsetPreflightError(code="subset_relationship_composite_length", report=...)` with a
single-failure report naming both ends and both tuple lengths (section 5.2).

Multi-parent children (same child columns referencing several parent tables) are legal (P2
module docstring, WS5). Subsetting semantics, DECIDED HERE: each edge is traversed
independently, so an upward pull adds the matching parent rows in EVERY parent table that
contains the key. This diverges from masking's declared-order-first-hit-wins lookup, on
purpose: pulling from all parents can never dangle and over-pulls at most the rows sharing
that key (bounded, visible in the estimate). Record this in `_closure.py`'s module docstring.

---

## 3. Key frames (`_keys.py`): the pre-materialization I/O boundary

```python
RI = "__subset_ri"   # module constant; reserved column name. Preflight fails
                     # (code subset_reserved_column) if any table already has this column.

def key_columns_needed(
    table: str, edges: tuple[SubsetEdge, ...], seeds: tuple[SeedSpec, ...],
) -> tuple[str, ...]:
    # union of: parent_columns of edges where parent_table == table,
    #           child_columns of edges where child_table == table,
    #           seed key_columns / predicate columns for seeds on this table. Sorted, deduped.

def load_key_frames(
    sources: Mapping[str, SubsetSource],
    edges: tuple[SubsetEdge, ...],
    seeds: tuple[SeedSpec, ...],
) -> dict[str, pl.DataFrame]:
    # per table (sorted):
    #   cols = key_columns_needed(...)
    #   frame = pl.scan_parquet(src.path).with_row_index(RI).select([RI, *cols]).collect()
    # Tables with no needed columns (isolated, unseeded) still get a frame of just RI
    # (select([RI])) so input_rows is known and the estimate can report them.
```

Verified: `with_row_index` before `select`/`collect` yields 0-based indices in file order and
projection pushdown keeps non-key columns unread. Every downstream stage (seed, closure,
budget, estimate) operates ONLY on these frames; `_materialize.py` is the only module that
re-touches source files.

---

## 4. SS3 + SS4 core: the closure algorithm and the fan-out gate

### 4.1 Established pattern (cite in `_closure.py`'s module docstring)

The closure is the classic monotone fixpoint: **semi-naive Datalog evaluation / Kleene
fixed-point iteration over a finite powerset lattice** (Ullman, "Principles of Database and
Knowledge-Base Systems", ch. 3; equivalently the worklist algorithm of monotone dataflow
frameworks). Each rule application is a relational **semi-join**; termination follows from
monotonicity + finiteness (Knaster-Tarski / Kleene), NOT from graph acyclicity - which is why
cycles need no special casing beyond the no-growth exit. Tonic/Redgate subsetters describe the
same two rules as "downstream/upstream traversal". Cite all of this; the CLAUDE.md
established-methodology rule requires the docstring citation.

### 4.2 The two rules (pure key-set semantics)

State: `survivors: dict[str, set[int]]` (row indices per table). Monotone: rows are ONLY ever
added, never removed. For edge `e` (parent P with columns pc, child C with columns cc):

- **Downward (cascade)**: any C-row whose FK key tuple equals the key tuple of a surviving
  P-row survives. `new = { r in rows(C) : key_cc(r) in keys_pc(survivors[P]) } - survivors[C]`.
- **Upward (parent completeness)**: any P-row whose key tuple equals the NON-NULL FK key tuple
  of a surviving C-row survives.
  `new = { r in rows(P) : key_pc(r) in nonnull_keys_cc(survivors[C]) } - survivors[P]`.

Null semantics (MUST match P3 exactly): a child key tuple with ANY null component is null; a
null key never matches downward (polars join default, verified) and never demands an upward
pull (explicit `drop_nulls()` before the upward semi-join). A non-null child key absent from
the ENTIRE parent table is a source orphan: upward cannot add anything, the child row keeps its
source-level orphanhood, and preflight already counted/gated it (section 5.4).

### 4.3 `compute_closure` (exact pseudocode - implement this, do not redesign)

```python
def compute_closure(
    *,
    edges: tuple[SubsetEdge, ...],                 # sorted by edge_id (from _edges.py)
    directions: Mapping[str, EdgeDirection],       # resolved by _policy for EVERY edge_id
    key_frames: Mapping[str, pl.DataFrame],        # from _keys.py; includes RI column
    seed_rows: Mapping[str, frozenset[int]],       # SS2 output
    budget_check: Callable[[Mapping[str, set[int]], tuple[EdgeStats, ...]], None],
                                                   # from _policy; raises
                                                   # SubsetBudgetExceededError; pass a no-op
                                                   # in pure unit tests
) -> ClosureResult:
    tables = sorted(key_frames)
    survivors: dict[str, set[int]] = {t: set(seed_rows.get(t, frozenset())) for t in tables}
    down_added = {e.edge_id: 0 for e in edges}
    up_added = {e.edge_id: 0 for e in edges}
    trace: list[RoundTrace] = []

    max_rounds = sum(kf.height for kf in key_frames.values()) + 2   # defensive only;
                                                                    # unreachable (sec 4.5)
    rounds = 0
    while True:
        rounds += 1
        if rounds > max_rounds:
            raise SubsetInternalError(code="subset_closure_nontermination",
                                      message="closure exceeded its monotone growth bound; "
                                              "this is an engine bug, report it")
        round_added_per_table: dict[str, int] = {}

        for e in edges:                                   # FIXED sorted order: determinism
            d = directions[e.edge_id]
            if d in ("both", "downward"):
                new = _downward_step(e, survivors, key_frames)
                if new:
                    survivors[e.child_table] |= new
                    down_added[e.edge_id] += len(new)
                    round_added_per_table[e.child_table] = (
                        round_added_per_table.get(e.child_table, 0) + len(new))
            if d in ("both", "upward"):
                new = _upward_step(e, survivors, key_frames)
                if new:
                    survivors[e.parent_table] |= new
                    up_added[e.edge_id] += len(new)
                    round_added_per_table[e.parent_table] = (
                        round_added_per_table.get(e.parent_table, 0) + len(new))

        total_added = sum(round_added_per_table.values())
        trace.append(RoundTrace(rounds, total_added,
                                tuple(sorted(round_added_per_table.items()))))
        edge_stats = _stats(edges, directions, down_added, up_added)
        budget_check(survivors, edge_stats)              # may raise mid-closure: early,
                                                         # exact-attribution abort; nothing
                                                         # has been written anywhere
        if total_added == 0:                             # THE no-growth exit. The ONLY exit.
            break

    return ClosureResult(
        survivors={t: frozenset(s) for t, s in survivors.items()},
        rounds=rounds, terminated_by="fixpoint",
        edge_stats=_stats(edges, directions, down_added, up_added), trace=tuple(trace))


def _downward_step(e, survivors, key_frames) -> set[int]:
    pkf, ckf = key_frames[e.parent_table], key_frames[e.child_table]
    if not survivors[e.parent_table]:
        return set()
    surv_keys = (pkf
                 .filter(pl.col(RI).is_in(sorted(survivors[e.parent_table])))
                 .select(list(e.parent_columns))
                 .unique())
    matched = ckf.join(surv_keys,
                       left_on=list(e.child_columns), right_on=list(e.parent_columns),
                       how="semi")                       # null child keys never match: OK
    return set(matched[RI].to_list()) - survivors[e.child_table]


def _upward_step(e, survivors, key_frames) -> set[int]:
    pkf, ckf = key_frames[e.parent_table], key_frames[e.child_table]
    if not survivors[e.child_table]:
        return set()
    needed = (ckf
              .filter(pl.col(RI).is_in(sorted(survivors[e.child_table])))
              .select(list(e.child_columns))
              .drop_nulls()                              # any-null component = null key (P3)
              .unique())
    matched = pkf.join(needed,
                       left_on=list(e.parent_columns), right_on=list(e.child_columns),
                       how="semi")
    return set(matched[RI].to_list()) - survivors[e.parent_table]
```

Implementation notes the builder must follow verbatim:

- `sorted(...)` before `is_in` and the fixed edge order make every run byte-deterministic.
- Self-referencing edges (parent_table == child_table) need NO special code path: `pkf` and
  `ckf` are the same frame, survivors is the same set, and the set-difference at the end of
  each step keeps additions monotone.
- When `e.child_columns == e.parent_columns` (same names), the semi-join is still fine: semi
  keeps only left columns. No renames needed.
- Column-name note: `left_on`/`right_on` lists must preserve the DECLARED tuple order
  (positional pairing), not sorted order.
- Do NOT implement the delta/semi-naive optimization (joining only against last round's
  additions) in this sprint. Full recomputation per round is O(rounds x edges x N) with
  hash-join constants and is fine at v1 full-frame scale; the delta version changes no test
  and can land later behind the same signatures. Note this in a comment.

### 4.4 SS4: directions, budget, estimate (`_policy.py`)

```python
def resolve_edge_directions(
    edges: tuple[SubsetEdge, ...], policy: FanOutPolicy,
) -> dict[str, EdgeDirection]:
    # start {e.edge_id: "both"}; overlay policy.edge_directions;
    # unknown id -> SubsetConfigError(code="subset_unknown_edge", message names it + lists
    #   the valid edge_ids);
    # any resolved value in ("downward", "none") while not policy.allow_dangling ->
    #   SubsetConfigError(code="subset_dangling_not_acknowledged", message explains that
    #   disabling upward traversal can orphan child FKs and requires allow_dangling=True).
    # ("upward"-only is safe: it can only shrink the downward pull, never dangle.)

def make_budget_check(
    budget: FanOutBudget, total_seed_rows: int,
) -> Callable[[Mapping[str, set[int]], tuple[EdgeStats, ...]], None]:
    # returns the closure-injected checker:
    #   total = sum(len(s) for s in survivors.values())
    #   if budget.max_total_rows is not None and total > budget.max_total_rows:
    #       raise SubsetBudgetExceededError(scope="total", table=None, cap=..., actual=total,
    #             edge_id=<edge with max rows_added_downward+rows_added_upward>, ...)
    #   if budget.max_table_seed_multiple is not None:
    #       cap = ceil(budget.max_table_seed_multiple * total_seed_rows)
    #       for t sorted: if len(survivors[t]) > cap:
    #           offending edge = argmax over edges touching t of rows added INTO t
    #             (rows_added_downward if e.child_table == t, rows_added_upward if
    #              e.parent_table == t); tie-break by edge_id
    #           raise SubsetBudgetExceededError(scope="table", table=t, cap=cap,
    #                 actual=len(survivors[t]), edge_id=..., seed_total=total_seed_rows)
```

```python
class SubsetBudgetExceededError(SubsetError):
    # code is ALWAYS "subset_budget_exceeded"
    # structured fields: scope: Literal["total","table"], table: str | None, cap: int,
    #   actual: int, seed_total: int, edge_id: str (the top contributor)
    # message template (exact, for the test to match on):
    #   f"fan-out budget exceeded: {scope_desc} {actual} rows > cap {cap} "
    #   f"(seed total {seed_total}); top contributing edge: {edge_id}. "
    #   "No output was written. Raise the budget, disable traversal on the offending "
    #   "edge, or shrink the seed."
    # where scope_desc = f"table {table!r} has" for table scope, "subset output has" for total.
```

The check runs at the END of every round (early abort, exact attribution, and no cheaper
correct alternative: a mid-round check would attribute a table's overshoot to whichever edge
happened to run last) and is therefore also the final pre-materialization gate. HARD-FAIL
CONTRACT: the exception propagates out of `plan_subset`/`run_subset` before `output_dir` is
created; there is nothing to clean up. NEVER catch it to truncate (truncation re-introduces
orphans; GATE-1 #3).

`build_estimate(...) -> SubsetPlan` assembles `TableEstimate` rows from
`key_frames[t].height` (input_rows), seed stats, and `len(survivors[t])`. The estimate is
EXACT by construction: it is computed from the same survivor sets SS5 will materialize, which
is what makes acceptance test 5 (dry-run == materialized) hold with equality, not tolerance.
Append a warning string per table with `surviving_rows == 0` ("table {t} has no surviving
rows; it is disconnected from every seed under the configured traversal directions").

### 4.5 Termination and correctness argument (reviewers check THIS)

**Termination.** `survivors` only grows: both steps compute `new - survivors[...]` and union
it in; nothing is ever removed (monotonicity). The state space is the finite product of
powersets of each table's row set, so the strictly-increasing chain
`survivors_after_round_1 < survivors_after_round_2 < ...` has length at most
`N = sum(rows(t))`. The loop exits the first round that adds zero rows, so it runs at most
`N + 1` rounds regardless of graph shape - self-loops, mutual cycles, and multi-edge diamonds
included, because NO step in the argument mentions acyclicity. The `max_rounds = N + 2` guard
is a defensive assertion for an engine bug (e.g. someone later makes a step non-monotone), not
a load-bearing mechanism; tests assert it is never the exit path.

Why the "visited-set" framing from the plan maps to this: a per-ROW visited set is exactly
what `survivors` is - a row already in `survivors` can never be re-added (`new - survivors`),
so a cycle cannot re-enqueue work; the no-growth exit is the fixpoint detection. There is no
separate per-node visited structure to build, and the builder must not add one.

- Self-ref (`employee.manager_id -> employee.id`): upward walks the manager chain adding each
  ancestor once; when the chain closes into a row-level cycle (a manages b, b manages a) the
  second traversal finds both rows already in `survivors` and adds nothing; next round adds
  zero; exit. Rounds = (longest new-ancestor chain) + 1.
- Mutual (A -> B -> A): each round alternately grows A then B along the reference chain; the
  chain is finite; the round after the last addition adds zero; exit.

**Partial correctness (the invariant a reviewer checks).** At exit, for every edge `e` with
upward enabled: every surviving child row's non-null FK key is EITHER present among surviving
parent rows' keys OR absent from the entire parent table (source orphan, already counted at
preflight). Proof sketch: if the key exists in the parent table but its rows were not all in
`survivors`, the last upward step over `e` would have added them (semi-join adds every
matching parent row), contradicting zero growth. Symmetrically for downward completeness: every
child row of a surviving parent key is in `survivors` for downward-enabled edges. And
no-over-pull: rows enter `survivors` only via seed membership or one of the two rules, so every
survivor has a justification chain back to the seed (tests pin exact expected sets to catch
over-pull).

```python
def verify_closure(
    *, edges, directions, key_frames, result: ClosureResult,
) -> None:
    # Pure re-check of the upward invariant above (anti-join per upward-enabled edge:
    # surviving-child non-null keys ANTI-JOIN surviving-parent keys must be a subset of
    # the FULL-parent-table-absent key set). Raises
    # SubsetInternalError(code="subset_closure_invariant_violated") naming the edge.
    # Called by run_subset between closure and materialization: this is the last line of
    # defense for the sprint's core risk (a missed upward pull = dangling FK) and is cheap
    # (key frames are already in memory).
```

---

## 5. SS1 preflight (`_preflight.py`)

Order of checks (stop classifying an edge after its first schema-level failure, but keep
checking OTHER edges so the report is complete; `passed = not failures`):

### 5.0 Parquet-only gate (GATE-1 #1)

For every table referenced by any edge or seed: `sources[table].format == "parquet"` else
`PreflightFailure(code="subset_requires_parquet", relationship=f"<{table}>", message=
f"table {table!r} source is {fmt!r}; FK-aware subsetting operates on Parquet datasets - "
"convert to Parquet for subsetting")`. Exact phrase "convert to Parquet for subsetting" is
part of the contract (plan GATE-1 #1). Also fail `subset_unknown_table` when a relationship
end names a table absent from `sources`.

### 5.1 Dangling target column

Per edge end: `schema = pl.scan_parquet(path).collect_schema()` (schema-only; verified). Every
declared column must be present:
`code="subset_relationship_column_missing"`, message
`f"{edge_id}: column {col!r} not found in table {table!r} (available: {sorted(schema.names())})"`.
Also fail duplicate columns within one end (`subset_relationship_duplicate_column`) and the
reserved `__subset_ri` name (`subset_reserved_column`).

### 5.2 Half-declared composite

Tuple-length mismatch is structurally unrepresentable once a `PlanRelationship` exists
(P1 `__post_init__`). The gap is the CONSTRUCTION path: `relationships_from_config` catches
the `ValueError` and converts it to
`PreflightFailure(code="subset_relationship_composite_length", relationship=<edge_id built
from the raw entry>, message=<the __post_init__ text, which already names both tuples>)`
wrapped in `SubsetPreflightError`. Callers passing already-built `PlanRelationship` tuples
cannot express this failure; document that in the docstring. (A second, subtler half-declared
shape - the user listed only SOME of a real composite key's columns - is undetectable without
schema metadata the engine does not have; the source-orphan scan in 5.4 is what catches it in
practice, since a truncated key rarely resolves cleanly. Note this limit in the docstring.)

### 5.3 Column-type mismatch

Positional per column pair `(pdt, cdt)`:

```python
def _key_dtypes_compatible(pdt: pl.DataType, cdt: pl.DataType) -> bool:
    if pdt == cdt:
        return True
    if pdt.is_integer() and cdt.is_integer():   # verified: polars supertypes int widths in joins
        return True
    return False
```

Failure `code="subset_relationship_type_mismatch"`, message
`f"{edge_id}: parent {ptable}.{pcol} is {pdt} but child {ctable}.{ccol} is {cdt}; "
"cast the columns to a common type upstream (e.g. '007' vs 7 never joins)"`. Additionally any
FLOAT dtype on either side fails with `code="subset_relationship_key_float_unsupported"`
(engine posture: float determinism is PO-locked hard-error, P6; and float join keys are a
correctness hazard). Datetime unit mismatches (`Datetime("us")` vs `Datetime("ns")`) fail the
plain mismatch check by design - fail closed, tell the user to cast.

### 5.4 Key-level source-orphan pre-scan (the `run_fk_validity` reuse)

Runs only if 5.0-5.3 produced no failures (schemas are sane, so key frames are loadable).
This is the adapter over P3's semantics - `run_fk_validity` itself cannot run pre-selection
(its `ScanContext` needs a compiled Plan, masked outputs, a provider registry; none exist
yet), so `_preflight.py` re-implements its per-edge classification on key frames with the
same counting rules, and the docstring cites `_fk_validity.py` as the semantic source:

```python
kf_c, kf_p = key_frames[e.child_table], key_frames[e.parent_table]
child_keys = kf_c.select(list(e.child_columns))
child_row_count = child_keys.height
non_null = child_keys.drop_nulls()               # any-null component = null key, per P3
parent_keys = kf_p.select(list(e.parent_columns)).drop_nulls().unique()
orphans = non_null.join(parent_keys, left_on=list(e.child_columns),
                        right_on=list(e.parent_columns), how="anti")
source_orphan_count = orphans.height             # per-ROW count incl. duplicates, like P3
parent_match_count = non_null.height - source_orphan_count
```

Policy handling mirrors P3: `orphan_policy == "fail"` and `source_orphan_count > 0` ->
`PreflightFailure(code="subset_source_orphans", ...)` (fail-closed); `"warn"` -> a warning
string; `"preserve"`/`"remap"` -> recorded in the edge report only. `invalid_count =
source_orphan_count if policy == "fail" else 0`.

`run_subset_preflight` RETURNS the report (never raises on check failures);
`plan_subset`/`run_subset` raise
`SubsetPreflightError(code=<first failure's code>, report=report)` when `not report.passed`,
which is what guarantees SS2/SS3 never run on a bad declaration (acceptance test 4).

---

## 6. SS2 seed selection (`_seed.py`)

```python
def select_seed_rows(
    *,
    seeds: tuple[SeedSpec, ...],
    key_frames: Mapping[str, pl.DataFrame],
    job_seed: bytes,
) -> tuple[dict[str, frozenset[int]], dict[str, int], dict[str, int]]:
    # returns (seed_rows, seed_counts, seed_null_excluded), all keyed by table.
    # Multiple specs may target different tables; two specs on ONE table ->
    # SubsetConfigError(code="subset_duplicate_seed_table"). Union semantics are a follow-on.
```

### 6.1 `sample` mode: deterministic bottom-k over HMAC digests

Established pattern (cite in the docstring): bottom-k / KMV consistent sampling - selection by
smallest keyed-hash values is stable under reruns and independent of RNG library state (same
family as MinHash bottom-k sketches). REJECTED alternative, and why (record in docstring):
`pl.DataFrame.sample(seed=...)` is only guaranteed reproducible within a polars version; the
engine's determinism envelope (P5/P6) requires cross-version stability, which HMAC-over-
canonical-bytes provides and the engine already uses for every masked value.

```python
ns = f"subset/sample/{spec.table}"
ctx = DeriveContext.for_column(job_seed, ns)              # P5; one HKDF, then per-row HMAC
kf = key_frames[spec.table].select([RI, *spec.key_columns])
non_null = kf.drop_nulls(subset=list(spec.key_columns))
seed_null_excluded = kf.height - non_null.height          # null-key rows are never seeds
n = non_null.height
k = spec.count if spec.count is not None else max(1, floor(spec.fraction * n))
k = min(k, n)
digests: list[tuple[bytes, int]] = []
for row in non_null.iter_rows():                          # (ri, key0, key1, ...)
    ri, key = row[0], row[1:]
    source = b"".join(
        len(part := _canonicalize_source(component)).to_bytes(4, "big") + part
        for component in key)                             # length-prefix each component so
                                                          # composite encodings are injective
                                                          # (same framing derive() itself uses)
    digests.append((ctx.derive_source(ns, source), ri))
digests.sort()                                            # (digest, ri): ri tie-breaks dup keys
selected = frozenset(ri for _, ri in digests[:k])
```

Contract: same file + same job_seed + same spec -> identical selection, across processes and
polars versions. When key_columns values are unique, selection is also row-order independent
(digest depends only on key bytes). Duplicate keys tie-break by row index (file order) -
document. `_canonicalize_source`'s float/naive-datetime hard errors surface as
`SubsetConfigError(code="subset_seed_key_uncanonicalizable")` wrapping the message (preflight
5.3 already blocks float FK columns; this guards seed keys that are not FK columns). The
per-row Python loop is acceptable at v1 full-frame scale (root tables only; one HMAC per row);
note the future vectorization point.

### 6.2 `filter` mode: structured predicates

Map each `Predicate` to a polars expression and AND-reduce; NO string-eval surface (the lark
`expressions` package is pandas-row oriented; structured comparisons map 1:1 to `pl.Expr`,
cite this choice):

```python
_OPS = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "le": operator.le,
        "gt": operator.gt, "ge": operator.ge}
def _expr(p: Predicate) -> pl.Expr:
    c = pl.col(p.column)
    if p.op == "in":          return c.is_in(list(p.value))
    if p.op == "is_null":     return c.is_null()
    if p.op == "is_not_null": return c.is_not_null()
    return _OPS[p.op](c, pl.lit(p.value))
selected = frozenset(kf.filter(reduce(operator.and_, map(_expr, spec.predicates)))[RI].to_list())
```

Predicate columns ride into `key_columns_needed` (section 3). Unknown column -> preflight-time
failure `subset_seed_column_missing` (check in 5.1 alongside relationship columns).

### 6.3 `keys` mode: explicit key list

Build `pl.DataFrame(spec.keys, schema=..., orient="row")` with columns named after
`spec.key_columns`, cast each column to the key frame's dtype
(`.cast(kf.schema[col])`, wrapping a cast failure as
`SubsetConfigError(code="subset_seed_key_type")`), then semi-join the key frame against it and
take `RI`. The raw values live only in the `SeedSpec`; `seed_specs_public` serializes keys mode
as `{table, mode, key_columns, key_count}` ONLY (acceptance test 6).

An empty selection (filter matched nothing / empty keys) is legal: the estimate reports zero
survivors everywhere reachable, with the zero-survivor warning making it visible.

---

## 7. SS5 materialization, manifest, config enforcement

### 7.1 `materialize_subset` (`_materialize.py`)

```python
def materialize_subset(
    *,
    sources: Mapping[str, SubsetSource],
    survivors: Mapping[str, frozenset[int]],
    output_dir: Path,
) -> tuple[tuple[str, str], ...]:
    # Precondition (enforced by _api ordering, assert here defensively): budget passed and
    # verify_closure passed. Only NOW is output_dir created (mkdir parents=True,
    # exist_ok=False -> SubsetConfigError(code="subset_output_dir_exists") if present and
    # non-empty; refusing reuse is what makes "no partial Parquet" testable).
    for table in sorted(survivors):
        out = output_dir / f"{table}.parquet"
        idx = sorted(survivors[table])
        (pl.scan_parquet(sources[table].path)
           .with_row_index(RI)
           .filter(pl.col(RI).is_in(idx))
           .drop(RI)
           .collect()
           .write_parquet(out))
```

Design decisions locked here:

- **Filter by row index, not by key re-join.** The survivor sets ARE the subset; re-deriving
  membership by key semi-join at write time would re-open duplicate-key and null-key edge
  cases and could drift from the estimate. Row-index filtering makes acceptance test 5
  (`dry-run == materialized`) true BY CONSTRUCTION and preserves original row order
  (verified: `filter(is_in)` is order-preserving), giving deterministic, order-independent
  output given the key sets.
- Tables with zero survivors still get a (schema-preserving, zero-row) Parquet file: schemas
  stay complete for the downstream mask stage.
- After each write, defensively assert written height == len(idx); mismatch ->
  `SubsetInternalError(code="subset_materialize_count_mismatch")`.
- Full-frame note (GATE-1 #4): `collect()` holds one table at a time (tables are processed
  sequentially and dropped); peak memory = largest single table + key frames. **Eviction
  plug-in point**: when `feat/fk-ri-memory-scaling`'s `_sequential.py` (Option 2 per-table
  load/mask/evict) merges, (a) this loop is already per-table-sequential so it composes as-is,
  and (b) the `collect().write_parquet(...)` pair can become `sink_parquet` (streaming) behind
  the same function signature. Do not build either now; leave this note as a comment at the
  loop site.

### 7.2 Manifest (`_manifest.py`)

`build_manifest(plan: SubsetPlan, engine_version: str) -> SubsetManifest` copies the plan's
tables/edges/rounds/budget plus `preflight_summary = plan.preflight.edges`, stamps
`manifest_version=1`, and `to_json_dict()` emits nested dicts/lists of str/int/float/bool/None
only. `run_subset` writes `output_dir / "subset-manifest.json"` (sorted keys, indent 2) AFTER
all table writes succeed, and returns it in `SubsetResult`. Established contract to cite: the
counts-and-identifiers-only rule mirrors the alerts/evidence no-raw-data convention
(platform `docs/reference/alerts.md`) and the frozen-report discipline of
`validation/post/_types.py`. NO raw key values, ever: the only fields that could leak them are
`seed_specs_public` (keys mode is count-only, section 6.3) and error messages (budget/orphan
messages name tables/edges/counts, never values).

### 7.3 Config-level subset-then-mask enforcement (`config/_subset.py` + `_pipeline.py` edit)

```python
# config/_subset.py (pydantic, extra="forbid" everywhere, mirroring _relationships.py)
class SubsetPredicateConfig(BaseModel):
    column: str; op: Literal[...PredicateOp values...]; value: Any | None = None
class SubsetSeedConfig(BaseModel):
    table: str
    mode: Literal["sample", "filter", "keys"]
    key_columns: list[str] = []
    fraction: float | None = Field(default=None, gt=0, le=1)
    count: int | None = Field(default=None, ge=1)
    predicates: list[SubsetPredicateConfig] = []
    keys: list[list[Any]] = []
    # model_validator: same mode-consistency rules as SeedSpec.__post_init__
class SubsetBudgetConfig(BaseModel):
    max_total_rows: int | None = Field(default=None, ge=1)
    max_table_seed_multiple: float | None = Field(default=None, gt=0)
class SubsetConfig(BaseModel):
    seeds: list[SubsetSeedConfig] = Field(min_length=1)
    budget: SubsetBudgetConfig = SubsetBudgetConfig()
    edge_directions: dict[str, Literal["both", "downward", "upward", "none"]] = {}
    allow_dangling: bool = False
```

`PipelineConfig` gains `subset: SubsetConfig | None = None` plus one
`@model_validator(mode="after") def _subset_stage_constraints(self)`:

1. Every `subset.seeds[].table` must be a declared mask-kind table with a source.
2. When `subset` is set, every source referenced by a subsetted or relationship-bearing table
   must be `format == "parquet"` (submit-time mirror of preflight 5.0, so the operator sees
   the reject at validation, not at run).
3. **Subset-after-mask rejection**: within one config the ordering is structural (there is no
   syntax that runs subsetting after masking; the run path is subset stage -> mask stage,
   period). The one config shape that would EXPRESS mask-then-subset is pointing the subset
   job's sources at the same config's mask targets: reject when `subset` is set and any
   `sources[*].path` equals any `targets[*].path` (resolve via `os.path.normpath`;
   like-for-like local `file` descriptors only), message
   `"subset input {path!r} is also a mask target of this pipeline; subsetting runs BEFORE "
   "masking (subset-then-mask), it cannot consume masked output"` - raise `ValueError` per
   the P7 validator pattern (pydantic wraps it).

`subset_inputs_from_config` (in `_api.py`) converts the validated dump: sources ->
`SubsetSource(path, format)` from the `sources` block; relationships via
`relationships_from_config`; seeds/budget/directions/allow_dangling 1:1 into the frozen types.
Composition contract (documented in `_api.py` docstring): callers (SS6 CLI, platform runner)
run `run_subset(...)` first, then feed the written subset Parquet files as the `sources` of
the existing mask path (`run_pipeline` / adapters) - masking is UNCHANGED, which is what makes
acceptance test 7 pass with the existing propagation machinery.

---

## 8. Build order, per-slice files, and THE TESTS

General: TDD per slice (tests first), pytest under `tests/unit/subset/` +
`tests/integration/subset/` (new dirs are fine; the CI routing guard only pins existing ones).
Fixtures build small `pl.DataFrame`s and `write_parquet` to `tmp_path`; a shared
`tests/unit/subset/conftest.py` provides:

```python
def make_parquet(tmp_path, name, data: dict, schema=None) -> str          # writes + returns path
def rel(pt, pc, ct, cc, policy="preserve") -> PlanRelationship            # 1-child helper
JOB_SEED = b"\x01\x02\x03\x04\x05\x06\x07\x08"
```

### Slice SS1 - preflight  [mostly safe-mechanical; 5.3's compat matrix needs care]

Create: `subset/__init__.py`, `_types.py` (report + source + error-adjacent types only),
`_errors.py`, `_edges.py`, `_keys.py` (needed by 5.4), `_preflight.py`.
Tests `tests/unit/subset/test_preflight.py` - includes **acceptance test 4**:

- Fixture T (type mismatch): `customers.parquet` with `id = ["007", "008"]` (String);
  `orders.parquet` with `customer_id = [7, 8]` (Int64), edge
  `customers.id -> orders.customer_id`. Assert: `run_subset_preflight` returns
  `passed is False`; exactly one failure with
  `code == "subset_relationship_type_mismatch"` and
  `failure.relationship == "customers.id -> orders.customer_id"`; message contains both dtype
  names. Then assert `plan_subset(...)` with the same inputs raises `SubsetPreflightError`
  and (fail-closed proof) monkeypatch `decoy_engine.subset._closure.compute_closure` with a
  `pytest.fail` stub to prove SS3 never ran.
- Fixture C (half-declared composite): config dict with parent
  `{"table": "a", "columns": ["k1", "k2"]}` child `{"table": "b", "columns": ["k1"]}`;
  assert `relationships_from_config` raises `SubsetPreflightError` with
  `code == "subset_relationship_composite_length"` and the report failure message containing
  both tuple lengths (the P1 `__post_init__` text).
- Fixture D (dangling column): edge child columns `("customer_ref",)` absent from
  orders.parquet; assert `code == "subset_relationship_column_missing"`, message names
  `customer_ref`, `orders`, and lists available columns.
- Compat positives: Int32 parent / Int64 child passes; identical String/String passes.
- Float key: Float64 parent id -> `subset_relationship_key_float_unsupported`.
- CSV source: format "csv" -> `subset_requires_parquet`, message contains
  `"convert to Parquet for subsetting"`.
- Orphan pre-scan: parent {1,2}, child FKs [1, 1, 3, None] -> edge report has
  `child_row_count == 4`, `non_null_child_key_count == 3`, `parent_match_count == 2`,
  `source_orphan_count == 1`; with `orphan_policy="fail"` the report gains a
  `subset_source_orphans` failure; with `"warn"` it passes with one warning; with
  `"preserve"` it passes clean. (This pins P3-parity semantics: nulls neither match nor
  orphan; per-row counting.)
- `test_edges.py`: expansion of a 2-children PlanRelationship into 2 sorted SubsetEdges;
  dedupe of a duplicate declaration; edge_id format string equality.

### Slice SS2 - seed selection  [needs care: canonical byte framing; rest mechanical]

Create: `_seed.py`. Tests `tests/unit/subset/test_seed.py`:

- Determinism: 1000-row key frame, `fraction=0.02, key_columns=("id",)` -> exactly 20 rows;
  two calls identical; same spec with a DIFFERENT job_seed differs (assert unequal sets).
- Row-order independence for unique keys: shuffle the frame rows, rebuild RI, same fraction ->
  the SELECTED KEY VALUES are identical (not the row indices).
- `count=5` wins over fraction; `k` clamps to n; `max(1, ...)` floor (fraction so small floor
  gives 0 -> 1 row selected).
- Null key components excluded and counted in `seed_null_excluded`.
- Float key column raises `SubsetConfigError(code="subset_seed_key_uncanonicalizable")`.
- filter mode: predicates `[("region","eq","EU"), ("active","eq",True)]` -> exact expected
  index set; `in` and `is_null` ops covered.
- keys mode: explicit tuples select exactly matching rows (duplicates in the table: ALL rows
  with that key are selected); wrong-type value -> `subset_seed_key_type`.
- Composite framing injectivity: keys `("ab","c")` vs `("a","bc")` produce different digests
  (asserts the length-prefix framing; this is the load-bearing anti-collision test).

### Slice SS3 - closure  [NEEDS CARE: the novel core; follow section 4.3 verbatim]

Create: `_closure.py`. Tests `tests/unit/subset/test_closure.py` (pure: key frames built
in-memory with `pl.DataFrame`, `RI` added via `with_row_index(RI)`; `budget_check=lambda
*a: None`) - includes **acceptance test 2**:

- Chain fixture (sanity): customers(3 rows)/orders/order_items linear; seed one customer;
  assert exact survivor sets per table, `terminated_by == "fixpoint"`,
  `trace[-1].rows_added == 0`, and `rounds == expected` (compute by hand for the fixture;
  e.g. seed -> round 1 adds orders+items, round 2 adds nothing -> rounds == 2).
- **Self-ref**: `employee(id, manager_id)` rows
  `[(1, 2), (2, 1), (3, 2), (4, 3), (5, None), (6, 5)]`, edge
  `employee.id -> employee.manager_id`, direction "both", seed `{row of id 4}`.
  Expected survivors: upward chain 4 -> 3 -> 2 -> 1 -> (2, visited) plus downward from each
  pulled manager (children of 1,2,3: rows 1,2,3,4 already; id 3's report 4 present) = rows of
  ids {1, 2, 3, 4}. Assert the EXACT set; assert `rounds` equals the hand-computed value;
  assert monotone trace (`per-round survivors never shrink`: reconstruct cumulative counts
  from trace and assert non-decreasing); **no-growth exercised, not a timeout**: call
  `_upward_step` and `_downward_step` once more on the returned survivors and assert both
  return `set()` (the fixpoint is a genuine fixpoint), and assert
  `result.trace[-1].rows_added == 0` while `result.trace[-2].rows_added > 0`.
- **Mutual cycle**: tables a(id, b_ref), b(id, a_ref) with row-level cycle
  a1 -> b1 -> a1 and a tail a2 -> b2 -> a3; edges `a.id -> b.a_ref` and `b.id -> a.b_ref`,
  both directions "both"; seed `{a1}`. Assert exact fixpoint sets (a: {a1}, b: {b1} for the
  pure cycle; extend the fixture so the tail is only pulled when seeded from a2, proving no
  over-pull), same no-growth assertions as above.
- Null FK: child row with null FK survives via downward from its OTHER parent edge but
  triggers no upward pull (parent set unchanged).
- Direction toggles: upward-only edge never adds children; downward-only never adds parents;
  "none" adds nothing; `resolve_edge_directions` raises `subset_dangling_not_acknowledged`
  without `allow_dangling`, and `subset_unknown_edge` for a bogus id (these two live in
  `test_policy.py` but are listed here because SS3 tests consume resolved directions).
- Multi-parent: child C(k) with edges P1.k -> C.k and P2.k -> C.k, key present in both ->
  upward adds matching rows in BOTH parents (pins the section-2 decision).
- `verify_closure`: passes on every fixture above; corrupt a `ClosureResult` (remove one
  parent survivor) and assert `SubsetInternalError(code="subset_closure_invariant_violated")`
  naming the edge.
- Determinism: run the mutual-cycle fixture twice; assert identical `ClosureResult`
  (survivors, rounds, trace, stats).

### Slice SS4 - policy + estimate  [safe-mechanical given SS3's stats]

Create: `_policy.py`, `_api.py::plan_subset`. Tests `tests/unit/subset/test_policy.py` +
`tests/integration/subset/test_budget.py` - includes **acceptance test 3**:

- Budget fixture: customers 1 row, orders 1000 rows all FK -> that customer; seed the
  customer; `FanOutBudget(max_total_rows=100)`. Assert `run_subset(...)` (pointing at a
  FRESH `tmp_path / "out"`) raises `SubsetBudgetExceededError`; `e.code ==
  "subset_budget_exceeded"`, `e.scope == "total"`, `e.actual > 100`,
  `e.edge_id == "customers.id -> orders.customer_id"`; message contains the edge_id and
  `"No output was written"`; **and** `not (tmp_path / "out").exists()` plus
  `list(tmp_path.rglob("*.parquet")) == <fixture inputs only>` (no partial Parquet - the
  output dir was never created).
- Per-table cap: same fixture, `max_table_seed_multiple=10.0` (seed_total=1 -> cap 10);
  assert `scope == "table"`, `table == "orders"`, `cap == 10`.
- Both caps None: no raise; `SubsetPlan.budget_outcome == "pass"`.
- Estimate shape: `plan_subset` on the chain fixture returns exact `TableEstimate`s
  (input_rows/seed_rows/surviving_rows) and a zero-survivor warning for a disconnected table.

### Slice SS5 - materialization + manifest + config  [needs care: config validator + write ordering]

Create: `_materialize.py`, `_manifest.py`, `_api.py::run_subset`, `config/_subset.py`,
`PipelineConfig` field + validator, `subset_inputs_from_config`. Tests
`tests/integration/subset/test_subset_e2e.py`, `test_manifest.py`,
`tests/unit/config/test_subset_config.py` - includes **acceptance tests 1, 5, 6, 7**:

- **Acceptance 1 (referential completeness)**: customers 100 rows (id 1..100), orders 200
  (id 1..200, customer_id = (i % 100) + 1), order_items 400 (id, order_id = (i % 200) + 1);
  edges `customers.id -> orders.customer_id` (preserve),
  `orders.id -> order_items.order_id` (preserve); seed
  `SeedSpec(table="customers", mode="sample", key_columns=("id",), fraction=0.02)` -> 2
  customers. Run `run_subset`. Read the three output files. Assert: customers height == 2;
  `set(orders_out["customer_id"]) <= set(customers_out["id"])` (no orphan orders);
  `set(items_out["order_id"]) <= set(orders_out["id"])` (no orphan items); AND downward
  completeness: orders_out height == source rows whose customer_id is in the surviving
  customer set (exact equality, catches over-pull and under-pull symmetrically); same for
  items.
- **Acceptance 5 (dry-run == materialized)**: same fixture; `p = plan_subset(...)`;
  `r = run_subset(...)` (same args + output_dir); for every table:
  `p.tables[t].surviving_rows == pl.read_parquet(out).height ==
  r.manifest.tables[t].surviving_rows`. Exact equality, zero tolerance.
- **Acceptance 6 (manifest)**: run with a keys-mode seed whose value is the sentinel string
  `"SENTINEL_KEY_93217"` (a customers.id value present in the fixture). Load
  `subset-manifest.json`; assert `"SENTINEL" not in raw_json_text`; assert presence and
  types of: `seed_specs_public[0] == {"table": ..., "mode": "keys", "key_columns": [...],
  "key_count": 1}`, every table's `input_rows`/`surviving_rows`/`seed_rows`, every edge's
  `edge_id`/`direction`/`rows_added_downward`/`rows_added_upward`, `closure_rounds`,
  `budget` caps, `budget_outcome == "pass"`, `engine_version`, and the preflight summary
  counts.
- **Acceptance 7 (subset-then-mask)**: reuse the shape of the golden relational fixture
  (`tests/fixtures/golden/relational_parent_child`) with a hash-strategy masked parent PK +
  child FK declared in the `relationships` block. Steps: (1) `run_subset` -> subset dir;
  (2) load subset Parquet as `sources` (via `read_source_polars` or `pl.read_parquet(...)
  .to_arrow()`) and call the EXISTING `run_pipeline(config, sources, engine_version=...)`;
  (3) run the same mask config over the FULL source (no subset) with the same job seed.
  Assert: masked output heights == subset heights (not full-source heights); FK
  propagation: masked child FK values ⊆ masked parent PK values within the subset job; and
  determinism across jobs: for every surviving original parent key, the masked value in the
  subset job equals the masked value in the full job (P5: per-value derive is independent of
  which rows are present). Config-level: `PipelineConfig.model_validate` accepts a valid
  subset block; rejects a subset config whose source path equals a target path with a message
  containing `"subsetting runs BEFORE masking"`; rejects a subset seed naming an undeclared
  table; rejects `fraction=0` / `fraction>1` / both fraction and count.
- Mechanical extras: zero-survivor table writes a zero-row schema-preserving Parquet;
  existing non-empty output_dir -> `subset_output_dir_exists`; written-count defensive
  assertion untested path left alone (internal).

Slice dependency order: SS1 -> SS2 -> SS3 -> SS4 -> SS5 exactly; each slice's tests green +
CI-gate mirror clean before the next.

---

## 9. Engine CI-gate expectations (mirror locally before handing to review)

Run from the worktree root, venv active (per P11 and the standing decoy-ci-gate-mirror rule):

```
ruff check src tests testflight scripts
ruff format --check src tests testflight scripts
mypy src/decoy_engine testflight
pytest tests -m "not benchmark" --tb=short
sphinx-build -b html -W --keep-going docs docs/_build/html
```

Norms the builder must hit:

- Line length 100 (ruff format owns it). NO em-dashes / en-dashes / smart quotes anywhere
  (RUF002/RUF003 gate docstrings + comments; the repo rule is global).
- Comments explain WHY, one line unless a real invariant needs more; no references to this
  task/PR (repo CLAUDE.md).
- Every non-trivial module docstring cites its source pattern (section 4.1's Datalog/Kleene
  citation for `_closure.py`; bottom-k/KMV for `_seed.py`; `_fk_validity.py` parity for
  `_preflight.py`; the alerts no-raw-data convention for `_manifest.py`).
- All new dataclasses frozen, collections as tuples; validation never mutates; reports are
  frozen (house rules in `plan/_types.py` + CLAUDE.md).
- Module size: keep each `subset/*` file within the estimates in section 1 (`_api.py` is the
  only orchestrator; the 600-LOC orchestration cap applies to it with huge margin).
- mypy: the package is auto-covered by `mypy src/decoy_engine`; polars/pyarrow overrides
  already exist in pyproject. Type everything; no new `[[tool.mypy.overrides]]` entries.
- Do NOT add pages under `docs/` (sphinx -W fails on non-toctree docs; barry's docs step owns
  documentation). Do NOT touch `decoy_engine/__init__.py`.
- New test dirs `tests/unit/subset` + `tests/integration/subset` need no marker changes; do
  not rename any routing-guard directory.

---

## 10. Open risks and unverified items

1. **`ScanContext` reuse is impossible pre-selection (RESOLVED by design, flagged per task).**
   `run_fk_validity` needs a compiled `Plan`, masked `outputs`, a `ProviderRegistry`, and both
   registries - none exist before selection. The guide's resolution is the section-5.4
   minimal adapter: reimplement the per-edge classification (identical null/orphan/policy
   semantics, anti-join instead of Python loops) with a docstring citation back to
   `_fk_validity.py`, plus `FkPreflightReport` field parity. Reviewers should diff the
   semantics table in P3 against `_preflight.py`, not look for a shared function.
2. **`RelationshipGraph.ordering` is NOT a closure traversal order (verified, load-bearing).**
   It is a column-node mask order; table-level cycles never appear in it as cycles. SS3
   correctly ignores it. If a future builder "optimizes" the closure by walking `ordering`,
   that is a correctness regression on cyclic schemas - the fixpoint loop is the design.
3. **Polars behaviors pinned to 1.42.1.** Null-keys-don't-join (default `join_nulls=False`),
   int-width supertyping in joins, `with_row_index` stability, and order-preserving
   `filter(is_in)` were all verified by script on 1.42.1 (pyproject floor `polars>=1.0,<2.0`).
   These are documented, stable polars semantics, but the SS3/SS5 unit tests above assert all
   four indirectly, so a polars bump that changes any of them fails loudly. No action needed
   beyond keeping those tests.
4. **Per-row HMAC sampling cost (accepted).** SS2's Python loop does one HMAC per root-table
   row. At v1 full-frame scale (root tables, key columns only) this is seconds, not minutes;
   it is the same per-row derive cost masking already pays. Vectorizing (e.g. batching via
   Arrow) is a later optimization behind the same function; do not use `pl.Expr.hash`
   (explicitly NOT stable across polars versions).
5. **Multi-file/glob sources unverified.** Row-index stability was verified for single-file
   `scan_parquet` only. `FileSource.path` is a single path today; if a glob/dataset source
   ever lands, the RI-based identity needs re-verification. Guard: nothing to build now;
   the `SubsetSource` docstring must state "single Parquet file per table".
6. **`max_table_seed_multiple` semantics interpretation.** GATE-1 #3 says "per-table
   multiple-of-seed-size cap" without defining seed size for unseeded tables; this guide
   locks it to the GLOBAL seed row total (section 1.2) so every table has a well-defined cap.
   If Cam intended per-table-own-seed or per-table-fraction semantics, only
   `make_budget_check` and its two tests change. Flag at review; do not silently reinterpret.
7. **"Half-declared composite" beyond tuple-length.** A composite key declared with matching
   but INCOMPLETE column tuples (user forgot a key column on both sides) is undetectable
   without external schema metadata; the source-orphan pre-scan catches the practical cases
   (truncated keys rarely resolve). Documented as a stated limit in `_preflight.py` (5.2).
8. **`DeriveContext` is a private import.** `determinism/__init__.py` does not export it;
   in-engine private imports are established practice (P5). If review prefers, adding
   `DeriveContext` to the determinism `__all__` is a one-line, additive change - builder
   should NOT do it unprompted (frozen-surface discipline).
9. **Platform-side evidence wiring is out of engine scope.** The manifest reuses the
   contract's SHAPE (counts/ids only); actually attaching it to the platform job record
   (`api/evidence/`) is SS6/SS7 territory. The engine deliverable is the JSON artifact +
   `SubsetResult`.
