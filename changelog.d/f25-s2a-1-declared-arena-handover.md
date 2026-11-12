### Changed (declared staging hands one scratch arena to the route, F25 S2a-1, 2026-11-12)

A declared out-of-core job stages its typed copies into one scratch arena (a private child of the caller's `temp_dir`, ambient policy only, or in the system temp directory) and hands that arena to the route, so the copy and the route's spill sit in one root and count against one `temp_disk_budget_bytes`, under a strict file-access policy too. Each typed copy is created 0600 by the arena. The arena is removed when the job ends. The internal helpers `scratch_root` and `engine_scratch_root` are removed.
