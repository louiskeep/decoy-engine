"""FakerAdapter: the first concrete BackendAdapter implementation.

Wraps Faker behind the BackendAdapter protocol. At S4 close every
provider declares `supports_deterministic: False`; deterministic Faker
lights up in S5 via PoolAdapter wrapping FakerAdapter and indexing
into a pre-built pool via `derive_index`.

Implementation note (per S4 spec §7 + B1 PO call): the Faker -> semantic-
name mapping builds on top of V1's `decoy_engine.internal.faker_setup`
helpers where applicable (`make_faker(locale)` for instance construction).
The V2 adapter surface is the same either way; this is implementation
sharing, not a shim. V1's `register_faker_provider` table is NOT routed
into FakerAdapter; V2 has its own registration entry
(`register_faker_provider_v2`) so the V1 + V2 tables stay independent
until S9 removes the V1 strategy stack.

Source pattern: Faker's documented provider API (each Faker instance
exposes provider methods like `email()`, `name()`, `phone_number()`).
The semantic-name catalog at `_real_registry.py` binds engine-visible
names ("person_email") to Faker method calls.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import faker as faker_module

from decoy_engine.providers_v2._adapter import (
    CapabilityMatrix,
    ProviderSpec,
)
from decoy_engine.providers_v2._errors import AdapterError, ProviderError

# Semantic name -> Faker method name. Frozen; lookup table only.
_FAKER_METHOD_MAP: dict[str, str] = {
    # Identifiers
    "synthetic_ssn": "ssn",
    "synthetic_ein": "ein",
    "synthetic_account_number": "bban",
    "synthetic_npi": "numerify",
    "synthetic_ndc": "numerify",
    "synthetic_mrn": "numerify",
    "synthetic_member_id": "numerify",
    "synthetic_plan_id": "numerify",
    # Person attributes
    "person_name": "name",
    "person_first_name": "first_name",
    "person_last_name": "last_name",
    "person_full_name": "name",
    "person_email": "email",
    "person_phone": "phone_number",
    "person_dob": "date_of_birth",
    # Address attributes
    "address_street": "street_address",
    "address_city": "city",
    "address_state": "state",
    "address_zip": "postcode",
    "address_full": "address",
    # Generic
    "lorem_text": "text",
    "uuid": "uuid4",
    "random_int_range": "random_int",
    "random_choice": "random_element",
}


# Per-provider kwargs for Faker methods that need shape hints
# (e.g. numerify needs a digit pattern for synthetic IDs).
_FAKER_DEFAULT_KWARGS: dict[str, dict[str, Any]] = {
    "synthetic_npi": {"text": "##########"},  # 10-digit NPI
    "synthetic_ndc": {"text": "#####-####-##"},  # NDC 11-digit format
    "synthetic_mrn": {"text": "MRN########"},  # 8-digit MRN
    "synthetic_member_id": {"text": "MBR########"},
    "synthetic_plan_id": {"text": "PLN######"},
    "random_choice": {"elements": ("a", "b", "c")},
}


# V2-native custom providers registered via register_faker_provider_v2.
# Distinct from V1's _CUSTOM_FAKER_PROVIDERS table at
# decoy_engine.internal.faker_setup; the V1 + V2 tables stay independent
# until S9 removes the V1 strategy stack.
_V2_CUSTOM_PROVIDERS: dict[str, Callable[[Any], Any]] = {}


class FakerAdapter:
    """Concrete BackendAdapter wrapping Faker.

    `capability_matrix(provider)` returns the registry-resolved entry
    for `provider`; the adapter is registry-driven (the planner consults
    the registry, which routes to this adapter for Faker-bound names).
    The registry holds the per-provider CapabilityMatrix; the adapter
    is a thin protocol wrapper.
    """

    backend_type: str = "faker"
    backend_version: str = faker_module.VERSION

    def __init__(
        self,
        locale: str = "en_US",
        *,
        capabilities_lookup: Callable[[str], CapabilityMatrix] | None = None,
    ) -> None:
        """
        Args:
            locale: Faker locale (defaults to "en_US"). Per-call locale
                overrides land via ProviderSpec.locale at execute time.
            capabilities_lookup: function returning the CapabilityMatrix
                for a given provider name. Injected by the registry so
                the adapter doesn't reach back into the registry singleton
                directly (preserves the dependency direction:
                adapter <- registry, never adapter -> registry).
        """
        self._default_locale = locale
        self._caps_lookup = capabilities_lookup
        self._faker_instances: dict[str, Any] = {}

    def _faker(self, locale: str | None) -> Any:
        effective_locale = locale or self._default_locale
        if effective_locale not in self._faker_instances:
            self._faker_instances[effective_locale] = faker_module.Faker(effective_locale)
        return self._faker_instances[effective_locale]

    def _faker_call(self, provider: str, spec: ProviderSpec) -> Any:
        # V2-native custom providers take precedence over the built-in
        # _FAKER_METHOD_MAP for names that match.
        if provider in _V2_CUSTOM_PROVIDERS:
            return _V2_CUSTOM_PROVIDERS[provider](self._faker(spec.locale))
        method_name = _FAKER_METHOD_MAP.get(provider)
        if method_name is None:
            raise AdapterError(
                code="unknown_provider",
                message=(
                    f"FakerAdapter has no method mapping for {provider!r}. "
                    "Built-in catalog covers the 25 documented semantic names; "
                    "custom names land via register_faker_provider_v2."
                ),
            )
        kwargs = dict(_FAKER_DEFAULT_KWARGS.get(provider, {}))
        kwargs.update(spec.extra)
        method = getattr(self._faker(spec.locale), method_name)
        try:
            return method(**kwargs)
        except Exception as exc:
            raise AdapterError(
                code="capability_violation",
                message=(
                    f"FakerAdapter.generate({provider!r}) failed: {type(exc).__name__}: {exc}"
                ),
            ) from exc

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | None = None,
    ) -> Any:
        """Return one masked value.

        At S4 close FakerAdapter rejects deterministic requests with
        `capability_violation`: every catalog entry declares
        `supports_deterministic: False`. S5's PoolAdapter wraps this
        adapter and routes deterministic requests through
        `derive_index(...)` into a pre-built pool.
        """
        if spec.deterministic:
            raise ProviderError(
                code="capability_violation",
                message=(
                    f"FakerAdapter does not support deterministic mode for "
                    f"{provider!r} at S4 close (catalog declares "
                    "`supports_deterministic: False`). Route through S5's "
                    "PoolAdapter for poolable providers or S6's "
                    "DecoyNativeAdapter for format-bound identifiers."
                ),
            )
        return self._faker_call(provider, spec)

    def generate_batch(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        count: int,
    ) -> Sequence[Any]:
        """Return `count` values. Non-deterministic path; pool builds
        consume this for the S5 PoolAdapter wrap."""
        if spec.deterministic:
            raise ProviderError(
                code="capability_violation",
                message=(
                    "generate_batch does not support deterministic mode; "
                    "deterministic callers use generate(...) per row, or "
                    "wrap this adapter in PoolAdapter (S5)."
                ),
            )
        return [self._faker_call(provider, spec) for _ in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        """Return the per-provider capabilities via the registry-injected lookup.

        Raises `ProviderError(code='unknown_provider')` if the registry
        does not know the provider name.
        """
        if self._caps_lookup is None:
            raise AdapterError(
                code="capability_violation",
                message=(
                    "FakerAdapter constructed without a capabilities_lookup; "
                    "use ProviderRegistry to instantiate, not bare FakerAdapter()."
                ),
            )
        return self._caps_lookup(provider)


def register_faker_provider_v2(
    name: str,
    fn: Callable[[Any], Any],
) -> None:
    """[V2] Register a custom faker-backed provider against the V2 adapter.

    Distinct from V1's `register_faker_provider` on `decoy_engine.providers`;
    the V1 table stays in place per the B1 PO call. V2 callers (S5 pool
    builds, S6+ adapters) consult only the V2 table; V1 callers (V1
    strategy stack until S9) consult only the V1 table. Coexistence is
    intentional until S9 removes the V1 strategy stack.

    The custom provider defaults to non-deterministic, non-poolable
    capabilities. Customers who want deterministic mode pass a full
    `CapabilityMatrix` via `ProviderRegistry.override(...)` with a
    custom `BackendAdapter` that wires through
    `decoy_engine.determinism`.
    """
    _V2_CUSTOM_PROVIDERS[name] = fn


def _unregister_faker_provider_v2(name: str) -> None:
    """Test-only: clear a V2-registered custom provider."""
    _V2_CUSTOM_PROVIDERS.pop(name, None)


def _v2_custom_provider_names() -> frozenset[str]:
    """Test-only: snapshot of V2 custom-provider names."""
    return frozenset(_V2_CUSTOM_PROVIDERS.keys())
