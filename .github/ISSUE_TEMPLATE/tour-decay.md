---
name: Tour decay walk-through
about: Quarterly check that an engine onboarding tour still describes reality. Per Item 50 Phase F.
title: 'Tour decay: <tour file>'
labels: ['docs', 'maintenance']
---

## Tour to walk

(Pick one — open separate issues if walking both this quarter.)

- `.tours/1-onboarding.tour` — 9-stop introduction to the engine's structure (public API → `Logger` Protocol → `ExecutionContext` → `Masker` → transforms factory → a representative transform → graph runner → end-to-end tests).
- `.tours/2-hardest-flow.tour` — 9-stop walkthrough of the graph runner's Arrow cache + per-op engine dispatch + bounded-RSS eviction.

## Procedure

1. Open the repo in VS Code with the [CodeTour extension](https://marketplace.visualstudio.com/items?itemName=vsls-contrib.codetour) installed (publisher: `vsls-contrib`).
2. Play through the chosen tour stop by stop.
3. For each stop, check whether:
   - The pinned line still contains what the description claims.
   - The cross-references (other files, ADRs, guides, sibling tours, the published API site at <https://louiskeep.github.io/decoy-engine/>) still resolve.
   - The narrative matches the current implementation — not just literally true at the line, but conceptually accurate about what the file does. The hardest-flow tour especially: are the substrate / boundary / eviction claims still accurate?
4. Note every drift below.

## Drifts found

<!-- One bullet per drift. Cite the stop number, the file, and what's stale. -->

-

## Resolution

- [ ] Open a PR fixing the tour, OR delete the tour entirely if the drift is wider than rationally fixable in this issue.
- [ ] PR: #
- [ ] Walk the (fixed) tour cleanly end-to-end after merge to confirm.

## Cadence

- This walk: <!-- YYYY-MM-DD -->
- Previous walk: <!-- YYYY-MM-DD; check the prior closed tour-decay issue -->
- Next walk due: <!-- 3 months from this walk's completion date -->

> *A wrong tour is worse than no tour.* If you can't confidently fix the drift, delete the tour and reopen this issue with the deletion as the resolution.
