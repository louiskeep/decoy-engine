### Changed (scratch reader issued by the arena, F25 S2a-1b-i, 2026-11-12)

The reader that serves engine scratch files (`ScratchAccess`) now lives with the scratch arena and is issued only by `ScratchArena.reader()`, on a descriptor of its own. Declared staging reads its typed copies through the arena's reader. The arena's transitional path adoption now accepts a path at or below a live issued root under every file access policy, as `open_protected_dir` does; any other path still needs the ambient policy. Output bytes are unchanged.
