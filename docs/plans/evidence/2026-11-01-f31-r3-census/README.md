# F31 revision 3 census

Probes behind the counts cited in `docs/plans/2026-10-30-f31-shared-safe-output-boundary.md` (revision 3).

- Engine counts: `uv run --locked python <probe> src/decoy_engine ...` in a worktree of `ab29ef0c`
  after `uv sync --locked` (Python 3.11.18 from `uv.lock`). Output: `engine-ab29ef0c.txt`.
- Public-entry census: `entries.py` under `uv run --locked` in a worktree of the A-4 tip
  `claude/jolly-carson-3k5stf` at `d171f778`. Output: `entries-d171f778.txt`. The parameter-name
  match is a lower bound; slice E8 replaces it with the annotation-based census in the tool.
- Platform counts: stdlib `ast` scans in a worktree of `00e55c27`, run with the container Python
  (3.13), NOT the platform lock (not installed in this environment). They read source only and
  import nothing. Output: `platform-00e55c27.txt`. The context split of except-bound text reads
  (`http`, `raise`, `audit`, `logger`, `other`) was an inline variant of `r7b.py` that walks to the
  nearest enclosing `Raise`, `HTTPException(...)`, `write_audit*(...)` or logger call.
