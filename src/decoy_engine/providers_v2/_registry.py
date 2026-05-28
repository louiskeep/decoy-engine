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

import importlib.util

from decoy_engine.providers_v2._adapter import BackendAdapter, CapabilityMatrix
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.providers_v2._real_registry import get_default_catalog


def _mimesis_available() -> bool:
    """True if the optional `mimesis` dependency is importable.

    Uses find_spec so the default (Mimesis-absent) path never imports the
    mimesis sub-package, which would raise the install-message ImportError.
    Treats any lookup failure (genuine absence -> None; a blocked or broken
    parent -> ImportError/ValueError) as "not available".
    """
    try:
        return importlib.util.find_spec("mimesis") is not None
    except (ImportError, ValueError):
        return False


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
    """Return the singleton default registry.

    Post-S6: 19 FakerAdapter-bound + 5 DecoyNativeAdapter-bound entries
    (synthetic_ssn/ein/npi/ndc/mrn) = 24.
    Post-S8 (per spec §5): + 2 CompositeAdapter-bound entries
    (composite_name_email, composite_city_state_zip) = 26. Mimesis adds more
    only when installed AND in the adoption matrix (empty by default).

    Two calls return the same object. Tests that want a clean registry
    use `ProviderRegistry({...})` directly.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        # Two-pass dance preserved: build caps lookup over the combined
        # catalog (Faker + DecoyNative), then construct adapters that
        # reference the lookup.
        from decoy_engine.providers_v2._faker_adapter import FakerAdapter
        from decoy_engine.providers_v2.identifiers import (
            EinAdapter,
            MrnAdapter,
            NdcAdapter,
            NpiAdapter,
            SsnAdapter,
        )

        # 19 Faker-bound entries from the catalog.
        faker_catalog = get_default_catalog()
        # 5 DecoyNative entries built from each adapter's capability_matrix().
        decoy_native_adapters: dict[str, BackendAdapter] = {
            "synthetic_ssn": SsnAdapter(),
            "synthetic_ein": EinAdapter(),
            "synthetic_npi": NpiAdapter(),
            "synthetic_ndc": NdcAdapter(),
            "synthetic_mrn": MrnAdapter(),
        }
        decoy_native_caps = {
            name: adapter.capability_matrix(name) for name, adapter in decoy_native_adapters.items()
        }

        caps_by_name = {cap.provider: cap for cap in faker_catalog}
        caps_by_name.update(decoy_native_caps)

        def _caps_lookup(provider: str) -> CapabilityMatrix:
            cap = caps_by_name.get(provider)
            if cap is None:
                raise ProviderError(
                    code="unknown_provider",
                    message=f"Provider {provider!r} is not in the default catalog.",
                )
            return cap

        faker_adapter = FakerAdapter(capabilities_lookup=_caps_lookup)
        bindings: dict[str, tuple[BackendAdapter, CapabilityMatrix]] = {
            cap.provider: (faker_adapter, cap) for cap in faker_catalog
        }
        for name, adapter in decoy_native_adapters.items():
            bindings[name] = (adapter, decoy_native_caps[name])

        # S7 (per spec §5): fold adopted Mimesis providers into the build (NOT a
        # post-hoc override on the singleton). Gated on (a) mimesis installed
        # and (b) the provider being in the adoption matrix. The default
        # adoption set is empty, so the default registry stays 24 providers
        # unless benchmarks justify adoption. MimesisAdapter is poolable, so the
        # binding here is the direct adapter; S9 routing wraps it in PoolAdapter
        # for deterministic columns, identical to Faker-bound poolable providers.
        if _mimesis_available():
            from decoy_engine.providers_v2.mimesis import (
                ADOPTED_MIMESIS_PROVIDERS,
                MimesisAdapter,
            )

            if ADOPTED_MIMESIS_PROVIDERS:
                mimesis_adapter = MimesisAdapter(fallback=faker_adapter)
                for name in ADOPTED_MIMESIS_PROVIDERS:
                    bindings[name] = (mimesis_adapter, mimesis_adapter.capability_matrix(name))

        # S8 (per spec §5 / contracts row 28): fold the two composite generators
        # into the build via CompositeAdapter so they appear in known_providers()
        # and the row-2 unknown_provider check accepts them (24 -> 26). The
        # adapter's single-column path raises (composite_requires_bundle_path);
        # composites write coherent bundles via CompositeGenerator.generate_bundle.
        # poolable=True, so S9 routing wraps them in the bundle pool path.
        from decoy_engine.generation.composite import (
            CompositeAdapter,
            composite_capability,
        )

        for composite_name in ("composite_name_email", "composite_city_state_zip"):
            bindings[composite_name] = (
                CompositeAdapter(composite_name),
                composite_capability(composite_name),
            )

        _DEFAULT_REGISTRY = ProviderRegistry(bindings)
    return _DEFAULT_REGISTRY


def _reset_default_registry_for_tests() -> None:
    """Test-only: clear the singleton so a fresh build happens on next call.
    Used by tests that mutate the V2 custom-provider table to keep state
    from leaking across test cases."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None
