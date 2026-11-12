### Added (scratch native boundary, F25 S1b, 2026-11-12)

`decoy_engine.fileaccess` exports `scratch_native(fn, *args, **kwargs)`, which runs one DuckDB or pyarrow call on engine scratch and, under every policy, the ambient one included, turns a library failure into `FileAccessReadError("engine.scratch")` with no library text, cause or context. Memory failures and resource-errno `OSError`s keep their class, rebuilt without text that could carry a path or cell value where needed, and a file-access denial passes unchanged. No engine route calls it yet.
