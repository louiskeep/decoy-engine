Breaking: a table masked through `source_loader` with strategies under `tables:` and no `sources:` entry now refuses (`config/loader_table_undeclared`) instead of returning the loader's rows unmasked.

### Fixed (declared table never dropped from the plan, LOADER-SRC, 2026-11-14)

`run_pipeline` with a `source_loader` and a `tables:` entry that has no `sources:` entry profiled the table as nothing, so the plan held no work for it and the raw rows came back as output; only a quality warning noted the undeclared columns. The run now refuses before the loader runs or any data row is read, with `ConfigError`, reason `config/loader_table_undeclared`.

A second guard runs after the plan is built. A supplied mask table that has no plan entry is refused with `config/declared_table_dropped`, so no other path can echo a declared table. Both reasons are new and name only the table.
