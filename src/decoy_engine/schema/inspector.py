"""
SchemaInspector: stub.

Connector schema introspection (table list, column types, primary/foreign
keys) for use by `forge init` and synthetic-data scaffolding. Tracked in
SHARED_ENGINE_ARCHITECTURE.md; published here so CLI/platform can import
the symbol once. Implementation arrives with Phase 2.
"""


class SchemaInspector:
    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("SchemaInspector is not yet implemented; planned for Phase 2.")
