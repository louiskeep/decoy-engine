"""Exception hierarchy for the V2 provider package.

`ProviderError` covers compile-time and runtime provider-routing
failures (unknown semantic name, capability violation, deterministic-
config invariants). `AdapterError` is a narrower subclass for failures
inside a concrete `BackendAdapter` implementation (Faker raised,
locale unsupported, custom-provider callable crashed).

`ProviderError` is a peer of `decoy_engine.plan.PlanCompileError`, not
a subclass: provider errors arise at runtime (Faker import fails,
custom provider raises) as well as at compile time (unknown provider
in config). Callers that want compile-time variants treated as
`PlanCompileError` re-raise at the planner boundary; the planner does
this for `unknown_provider`.

Codes used in S4:

- `unknown_provider`: the requested semantic name has no binding in the registry.
- `deterministic_requires_namespace_and_seed`: ProviderSpec(deterministic=True)
  without both `namespace` and `seed`.
- `seed_wrong_length`: ProviderSpec(seed=...) where len != 8.
- `namespace_empty`: ProviderSpec(deterministic=True, namespace="").
- `unsupported_locale`: adapter does not support the requested locale.
- `capability_violation`: caller asked for a capability the adapter
  declares it does not support (e.g. deterministic Faker at S4 close).
"""

from __future__ import annotations


class ProviderError(Exception):
    """Provider-routing failure. Peer of PlanCompileError.

    Args:
        code: machine-readable error code (lowercase snake_case).
        message: human-readable explanation including enough context to
            find + fix the offending input.
    """

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


class AdapterError(ProviderError):
    """Failure inside a concrete BackendAdapter implementation.

    Distinct from ProviderError so the planner can catch the wider
    class (`except ProviderError`) while the runtime can catch the
    narrower one (`except AdapterError`) when wrapping an adapter call.
    """
