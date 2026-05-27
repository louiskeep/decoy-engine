"""PlanCompileError: typed error type for compile-time plan validation failures.

Carries a `code` (machine-readable, used by callers to route errors to
UI fields or CLI exit semantics), `path` (the YAML location where the
error was detected; None when the error is global), and a human-readable
message.

S1 ships five `code` values out of the 9-row compile-check ownership
table. S2 will subclass this with `NamespaceConfigError` per the S2 spec
TODO 5 resolution; S3-S13 add new codes following the same naming
convention (snake_case, suffixed with `_invalid` / `_missing` /
`_unsupported` as appropriate).

Callers catch `except PlanCompileError as e:` and inspect `e.code`,
`e.path`, `e.message`. Subclass-not-peer convention means
`except PlanCompileError` catches every compile-time error type.
"""

from __future__ import annotations


class PlanCompileError(Exception):
    """Raised by `compile_plan` on validation failure.

    Args:
        code: machine-readable error code (e.g. "namespace_ambiguity",
            "unknown_provider", "fk_cycle"). Lowercase snake_case.
        path: YAML location of the offending input, in dotted form
            (e.g. "tables.customers.columns.customer_id"). None for
            global errors.
        message: human-readable explanation of the failure. Should
            include enough context for the user to find and fix it.
    """

    def __init__(self, code: str, path: str | None, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        # Render a single string for stack traces; callers should inspect
        # the structured fields rather than parse the string.
        rendered = f"[{code}]"
        if path is not None:
            rendered += f" {path}:"
        rendered += f" {message}"
        super().__init__(rendered)
