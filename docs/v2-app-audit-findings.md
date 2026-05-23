# V2 App Audit Findings

Status: running notes file, kept across V2 sprint work
Owner: V2 sprint executor (PO + Dennis review)
Last revised: 2026-05-23

## Purpose

This document collects code-base issues found incidentally during V2 sprint
work. Each finding is logged here rather than expanded inline into the sprint
plan, so the sprint cadence stays predictable. After V2.0-A closes, this file
is reviewed and findings are either folded into V2.x backlog items or fixed in
follow-up PRs.

Findings carry: severity (S0 bug-now / S1 high / S2 medium / S3 low),
discovery context, evidence, and (when fixed in-loop) the fix reference.

---

## Findings

### 2026-05-23 · F-AUDIT-001 · NameError in custom-provider FK validation (S0)

**Discovered during:** V2.0-prep ruff baseline, F821 hit.

**Location:** [src/decoy_engine/graph/runner.py:931](../src/decoy_engine/graph/runner.py#L931)

**Symptom.** `_column_in_node` is called from inside
`_validate_custom_provider_entry` (function defined at line 879). The
function `_column_in_node` is defined as a NESTED function inside a different
sibling, `_validate_column_relationships` (line 346, with the nested def at
line 450). The two callers at lines 673 and 682 work fine (same enclosing
scope); the call at line 931 raises `NameError: name '_column_in_node' is not
defined` at runtime.

**Why this matters.** This code path executes during graph validation
whenever a `mask` or `generate` node has a custom-provider-backed FK. Any
existing fixture that exercises that path would have surfaced the bug, so
either no fixture exists (gap in test coverage) or the path is unreachable in
practice (dead code). Either way the engine is shipping a guaranteed-crash
branch.

**Fix applied (V2.0-prep, this sprint):** promote `_column_in_node` to
module-level. Pure function, no closures, two non-nested callers update
implicitly via name resolution. Adds a unit test that exercises the custom-
provider FK path and would have caught this.

---

### How to add a finding

Use the heading pattern `### <date> · F-AUDIT-NNN · <one-line summary> (Sx)`
where NNN is a zero-padded sequence number and Sx is severity. Body sections:

- **Discovered during:** which sprint step surfaced the finding.
- **Location:** file path with line number (markdown link).
- **Symptom:** what's wrong, in code terms.
- **Why this matters:** why a future contributor or shipped runtime cares.
- **Fix applied:** include if fixed in-loop. Otherwise leave blank.
- **Followup ticket:** link to GitHub issue if filed.

Findings move to "Resolved" at the bottom of the file once their fix has
landed and a regression test exists. Findings that turn out to be wrong are
struck through with a one-line correction noted.
