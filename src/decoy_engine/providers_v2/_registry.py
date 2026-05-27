"""ProviderRegistry: semantic-name -> (BackendAdapter, CapabilityMatrix) routing.

The single source of truth for "what backend serves which provider" at
runtime and at plan-compile time. `get_default_registry()` returns the
singleton built from `_real_registry.py`'s 25-entry catalog wired to
the default FakerAdapter.

Per S4 spec §5 + best-practices §2.1: the registry is immutable;
`override(...)` returns a new instance with the override applied. The
default registry is never mutated.

Source pattern: language-server URN registries (LSP's textDocument URI
mapped to capabilities) and package manager registries (canonical
package name -> metadata). Immutable lookup table; rebuilds are cheap.
"""

from __future__ import annotations

from decoy_engine.providers_v2._adapter import BackendAdapter, CapabilityMatrix
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.providers_v2._real_registry import get_default_catalog


class ProviderRegistry:
    """Frozen semantic-name -> (BackendAdapter, CapabilityMatrix) routing table."""

    def __init__(
        self,
        bindings: dict[str, tuple[BackendAdapter, CapabilityMatrix]],
    ) -> None:
        # Store as immutable internal dict.
        self._bindings: dict[str, tuple[BackendAdapter, CapabilityMatrix]] = dict(bindings)

    def get_adapter(self, provider: str) -> BackendAdapter:
        """Return the BackendAdapter bound to `provider`.

        Raises `ProviderError(code='unknown_provider')` if no binding exists.
        """
        entry = self._bindings.get(provider)
        if entry is None:
            raise ProviderError(
                code="unknown_provider",
                message=(
                    f"Provider {provider!r} is not in the registry. Known "
                    f"providers: {sorted(self._bindings.keys())!r}."
                ),
            )
        return entry[0]

    def get_capabilities(self, provider: str) -> CapabilityMatrix:
        """Return the CapabilityMatrix bound to `provider`.

        Raises `ProviderError(code='unknown_provider')` if no binding exists.
        """
        entry = self._bindings.get(provider)
        if entry is None:
            raise ProviderError(
                code="unknown_provider",
                message=(
                    f"Provider {provider!r} is not in the registry. Known "
                    f"providers: {sorted(self._bindings.keys())!r}."
                ),
            )
        return entry[1]

    def has(self, provider: str) -> bool:
        """True if `provider` is in the registry."""
        return provider in self._bindings

    def known_providers(self) -> frozenset[str]:
        """Frozenset of every registered provider name."""
        return frozenset(self._bindings.keys())

    def override(
        self,
        provider: str,
        adapter: BackendAdapter,
        capabilities: CapabilityMatrix,
    ) -> ProviderRegistry:
        """Return a NEW registry with `provider` rebound to `adapter` +
        `capabilities`.

        Used by per-pipeline backend pinning and by tests that need to
        inject a fake adapter. The default registry is never mutated.
        """
        new_bindings = dict(self._bindings)
        new_bindings[provider] = (adapter, capabilities)
        return ProviderRegistry(new_bindings)


# Default registry singleton. Built lazily on first access so that the
# FakerAdapter (which imports faker) is not constructed at module-import
# time (helps test isolation + matches the S2 build_namespace_registry
# pattern of deferred construction).
_DEFAULT_REGISTRY: ProviderRegistry | None = None


def get_default_registry() -> ProviderRegistry:
    """Return the singleton default registry: 25 FakerAdapter-bound entries.

    Two calls return the same object. Tests that want a clean registry
    use `ProviderRegistry({...})` directly.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        # Build the default FakerAdapter wired to a closure-style
        # capabilities lookup that resolves against the registry being
        # constructed. The two-pass dance (build caps lookup first,
        # then build registry that references the adapter that references
        # the caps lookup) keeps the dependency direction clean.
        from decoy_engine.providers_v2._faker_adapter import FakerAdapter

        catalog = get_default_catalog()
        caps_by_name = {cap.provider: cap for cap in catalog}

        def _caps_lookup(provider: str) -> CapabilityMatrix:
            cap = caps_by_name.get(provider)
            if cap is None:
                raise ProviderError(
                    code="unknown_provider",
                    message=f"Provider {provider!r} is not in the default catalog.",
                )
            return cap

        adapter = FakerAdapter(capabilities_lookup=_caps_lookup)
        bindings: dict[str, tuple[BackendAdapter, CapabilityMatrix]] = {
            cap.provider: (adapter, cap) for cap in catalog
        }
        _DEFAULT_REGISTRY = ProviderRegistry(bindings)
    return _DEFAULT_REGISTRY


def _reset_default_registry_for_tests() -> None:
    """Test-only: clear the singleton so a fresh build happens on next call.
    Used by tests that mutate the V2 custom-provider table to keep state
    from leaking across test cases."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None
