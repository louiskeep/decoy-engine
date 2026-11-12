### Changed (F22 publication plan narrowed to the V1 scope, F22, 2026-11-11)

Plan only, no code change. `docs/plans/2026-10-30-f22-publication-replan.md` revision 11 states
the V1 publication guarantee: all of a unit or none of it, durable when it reports `published`,
a different result refused, and a retry of a committed operation returns `already_committed`.
Dead-stage reclaim and the rename-based parent creation move to V2 (named limits L1u and L2).
The Windows arm (slice w1) is built after the V1 build, with the isolated-route refusal moved
from c2 to w1. Nine engine slices make up the V1 build.
