# Per-column routing: a performance concept for the Decoy engine

Status: concept (not gated, not scheduled). North star: speed, efficiency, performance. This note
records the idea and the honest tradeoffs so it can be gate-planned later. It is a sequel to the
route-coverage program (`docs/plans/2026-10-26-substrate-route-coverage.md`) and the strategy by
substrate audit.

## The goal

Run every column by the fastest method it can use, so no single column slows the whole job. Today a
masking job picks ONE execution route for the entire job (full-frame, sequential, out-of-core, or
native-chunked). That is a memory-safety decision: the route is chosen so the whole job stays within
a memory bound. The cost is that one column with an awkward strategy or dtype can pull the whole job
onto a slower route, or force a decline to the full-frame oracle, taxing every other column for one
column's constraint.

## The idea

Two steps, in order:

1. **Enable every strategy on every route it can correctly run on** (the route-coverage program,
   phases 1 and 2). This is the prerequisite: a column can only be routed to a route whose kernels
   can run its strategy with byte-parity.
2. **Route per column, not per job.** Once a strategy can run on more than one route, each column
   picks its own fastest path, and the job runs them together. A column that can stream stays
   bounded; a column that needs whole-column state takes a resident lane; a column with a native
   kernel uses it. The slowest column no longer sets the pace for all the others.

Byte-parity with the full-frame pandas oracle across batch splits stays the hard law for every
column on every route. A column whose fastest path cannot be proven parity-safe keeps its current
(correct) route; speed never buys a wrong value.

## What already exists (so this is a smaller first step than it looks)

Within a chosen route, the engine ALREADY dispatches each column to the fastest available compute
backend: on the sequential route `hash` and `fpe` run on the Rust kernel while `code_set` runs
Python, in one job. So "each column uses its fastest compute" is partly real at the backend level.
The new capability is letting columns run on different ROUTES at once, which is specifically valuable
when columns want different MEMORY models (one resident, the rest streaming) in the same job.

## The resident-column lane (and why UNIQUE/MATCH/SCALE are in play)

A strategy that needs whole-column state (faker UNIQUE, MATCH, SCALE) was previously treated as
batch-local-impossible. It is not: the state lives in ONE column, not the whole dataset. So you can
materialize just that column resident, compute the whole-column result there (which is split
invariant by construction, so it is parity-safe), and splice it back into the streaming output keyed
by the durable pre-join source offset. The rest of the job stays bounded. This is a per-column
resident lane, a special case of per-column routing, not a full-frame fallback.

## The three hard problems (why the payoff is unproven until measured)

1. **Memory is not escaped, it becomes a per-column budget.** The route is chosen per job today to
   bound TOTAL memory. If one column is resident while others stream, total memory is the SUM of each
   column's route footprint. A resident single column is O(rows) for that column (a 100M-row string
   column is multiple GB), not O(rows x columns) like full-frame, so it is a real saving, but it is
   not free. Per-column routing needs a global memory budget split across columns, with a
   decline-or-spill path for columns that do not fit. This is strictly more routing logic than
   today's whole-job byte-estimate router.

2. **FK and cross-column columns are coupled and cannot be routed independently.** FK keys,
   `joint_mask`, `group_key`, and `derived` read other columns, and the out-of-core route physically
   reorders rows. So per-column routing is clean only for genuinely independent PAYLOAD columns; the
   coupled ones must stay co-routed and re-aligned to the same row order by the durable source
   offset. Finding the independent set is itself analysis.

3. **Reassembly can eat the win.** If one column streamed (row-reordered) and another is resident
   (source order), the output must be spliced back into one frame in a consistent row order every
   batch, with aligned nulls and types. That per-batch align/join is real overhead that can cancel
   the per-column speedup. "Fastest route" is also not "most bounded route": a Python batch bridge
   cuts memory without improving compute speed, so a cost model that conflates the two picks wrong.

## Prior art to survey before designing

This is a cost-based execution planner. DuckDB, Apache Arrow Acero, and Spark already do per-operator
and per-column execution planning with cost models. The design should borrow that framing (a planner
that assigns each column an execution path from a cost model, under a global resource budget) rather
than hand-roll a router. Survey these before committing to an architecture, per the established
methodology rule.

## Recommendation

- Pursue the direction. "Enable all strategies on all routes" (phases 1 and 2) is the correct
  foundation and is already underway.
- Treat the per-column ROUTER itself as a later phase (call it phase 3), AFTER phases 1 and 2 land.
- Gate it behind a MEASURED PROTOTYPE before building the general machinery: take one real job where
  one column wants resident and the rest stream, build the split by hand, and show it beats whole-job
  routing NET OF reassembly. If the win is small or reassembly dominates, that is cheap to learn.
- Survey the query-engine cost-planner prior art first.
- Scope per-column routing to independent payload columns; keep FK and cross-row columns co-routed.
- Add a global resource budget with a decline-or-spill path per column; do not reintroduce the OOM
  the bounded route exists to prevent.

The risk is building a general per-column router on the assumption of a speedup that the memory
accounting and reassembly cost have not yet proven. The mitigation is: foundation first, prototype
and measure one job, survey prior art, then decide.
