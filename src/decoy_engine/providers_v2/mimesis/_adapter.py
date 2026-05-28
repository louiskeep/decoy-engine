"""MimesisAdapter: optional second concrete BackendAdapter (engine-v2 S7).

Wraps Mimesis behind the `BackendAdapter` protocol, mirroring `FakerAdapter`.

Determinism is POOL-ROUTED (S7 spec §2), not derive_value/Domain:
- `MimesisAdapter.generate(deterministic=True)` is REJECTED with
  `capability_violation`, exactly like FakerAdapter.
- `generate_batch` seeds the Mimesis instance from `spec.seed` (8-byte
  big-endian -> uint64, the Faker `seed_instance` convention) so a pool build
  is byte-identical across processes.
- `capability_matrix` declares `poolable=True` + `supports_deterministic=False`.
  S5's `PoolAdapter` wraps this adapter, indexes the built pool via
  `derive_index`, and is what boosts `supports_deterministic=True`.

There is no `MimesisDomain`: the rejected alternative (seed a fresh Mimesis
instance from the 32 derived bytes per row) throws away the batch speed win
and carries the whole library as hidden Domain state. Pool-routed is uniform
with how Faker gets determinism, since every Mimesis candidate is poolable.

Mimesis is optional: this module is imported only when `mimesis` is installed
(the package `__init__` guards the import; the registry gates on
`importlib.util.find_spec`).

Source pattern: Mimesis documented provider API (`mimesis.Generic` aggregates
`Person`/`Address`/`Datetime`; constructor `seed=` yields reproducible output).
Per best-practices §6.2 the methodology (use Mimesis, do not reinvent) is cited.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import mimesis as mimesis_module
from mimesis import Generic
from mimesis.locales import Locale

from decoy_engine.providers_v2._adapter import (
    BackendAdapter,
    CapabilityMatrix,
    ProviderSpec,
)
from decoy_engine.providers_v2._errors import AdapterError, ProviderError

if TYPE_CHECKING:
    from decoy_engine.generation.pool._events import QualityWarning

_MIMESIS_VERSION: str = mimesis_module.__version__

# Semantic provider name -> (Generic attribute, method). The candidate set is
# the 11 poolable PII providers (S7 spec §2 table); confirmed against the
# installed mimesis>=14.0 Generic API.
_MIMESIS_PROVIDER_MAP: dict[str, tuple[str, str]] = {
    "person_name": ("person", "full_name"),
    "person_first_name": ("person", "first_name"),
    "person_last_name": ("person", "last_name"),
    "person_full_name": ("person", "full_name"),
    "person_email": ("person", "email"),
    "person_phone": ("person", "telephone"),
    "person_dob": ("datetime", "date"),
    "address_street": ("address", "street_name"),
    "address_city": ("address", "city"),
    "address_state": ("address", "state"),
    "address_zip": ("address", "zip_code"),
}

# Faker-style locale string -> Mimesis Locale. Built defensively: only members
# present in the installed Locale enum are included, so `supported_locales`
# reflects real coverage rather than an aspirational list.
_FAKER_TO_MIMESIS_LOCALE_NAMES: dict[str, str] = {
    "en_US": "EN",
    "en_GB": "EN_GB",
    "de_DE": "DE",
    "es_ES": "ES",
    "fr_FR": "FR",
    "it_IT": "IT",
    "ja_JP": "JA",
    "ru_RU": "RU",
    "pt_BR": "PT_BR",
}
_FAKER_TO_MIMESIS_LOCALE: dict[str, Locale] = {
    faker: getattr(Locale, name)
    for faker, name in _FAKER_TO_MIMESIS_LOCALE_NAMES.items()
    if hasattr(Locale, name)
}
_SUPPORTED_LOCALES: tuple[str, ...] = tuple(_FAKER_TO_MIMESIS_LOCALE)


def mimesis_capability(provider: str) -> CapabilityMatrix:
    """Build the CapabilityMatrix for a Mimesis-bound provider.

    Every Mimesis candidate is poolable PII; `supports_deterministic` is False
    at the direct-generate layer (Faker-parity). PoolAdapter boosts it to True
    on wrap because `poolable=True`. No new CapabilityMatrix fields (R9).
    """
    if provider not in _MIMESIS_PROVIDER_MAP:
        raise ProviderError(
            code="unknown_provider",
            message=(
                f"MimesisAdapter has no mapping for {provider!r}. Candidates: "
                f"{sorted(_MIMESIS_PROVIDER_MAP)!r}."
            ),
        )
    return CapabilityMatrix(
        provider=provider,
        backend_type="mimesis",
        backend_version=_MIMESIS_VERSION,
        supports_deterministic=False,
        supports_uniqueness=True,
        supports_value_reuse=True,
        preserves_source_cardinality=False,
        participates_in_fk_pk=False,
        poolable=True,
        supported_locales=_SUPPORTED_LOCALES,
        supports_coherent_link=False,
        format_regex=None,
        blocklist_validators=(),
        fallback_behavior="fail_plan_compile",
    )


class MimesisAdapter:
    """Concrete BackendAdapter wrapping Mimesis. Pool-routed determinism."""

    backend_type: str = "mimesis"
    backend_version: str = _MIMESIS_VERSION

    def __init__(
        self,
        locale: str = "en_US",
        *,
        capabilities_lookup: Callable[[str], CapabilityMatrix] | None = None,
        fallback: BackendAdapter | None = None,
    ) -> None:
        """
        Args:
            locale: default Faker-style locale; per-call locale rides
                ``ProviderSpec.locale``.
            capabilities_lookup: accepted for protocol symmetry with
                FakerAdapter. MimesisAdapter is self-describing
                (``capability_matrix`` builds its own caps), so this is stored
                but not consulted for cap production.
            fallback: a BackendAdapter (a FakerAdapter in the default build)
                used when a requested locale is outside Mimesis coverage. Held
                here so the adapter never reaches back into the registry at
                generate time (preserves adapter <- registry direction).
        """
        self._default_locale = locale
        self._caps_lookup = capabilities_lookup
        self._fallback = fallback
        self._unseeded: dict[str, Generic] = {}
        self._warnings: list[QualityWarning] = []

    @property
    def warnings(self) -> tuple[QualityWarning, ...]:
        """QualityWarnings accumulated during locale fallbacks (drained by
        the execution layer into the manifest quality_summary at S10)."""
        return tuple(self._warnings)

    def _mimesis_locale(self, locale: str | None) -> Locale | None:
        return _FAKER_TO_MIMESIS_LOCALE.get(locale or self._default_locale)

    def _generic(self, mlocale: Locale, seed: int | None = None) -> Generic:
        if seed is not None:
            # Fresh seeded instance per batch: constructor-seed reproducibility
            # is the documented Mimesis contract (parity-suite item 6).
            return Generic(locale=mlocale, seed=seed)
        if mlocale.value not in self._unseeded:
            self._unseeded[mlocale.value] = Generic(locale=mlocale)
        return self._unseeded[mlocale.value]

    def _call(self, provider: str, gen: Generic) -> Any:
        attr, method = _MIMESIS_PROVIDER_MAP[provider]
        try:
            return getattr(getattr(gen, attr), method)()
        except Exception as exc:
            raise AdapterError(
                code="capability_violation",
                message=f"MimesisAdapter.generate({provider!r}) failed: {type(exc).__name__}: {exc}",
            ) from exc

    def _require_known(self, provider: str) -> None:
        if provider not in _MIMESIS_PROVIDER_MAP:
            raise AdapterError(
                code="unknown_provider",
                message=(
                    f"MimesisAdapter has no mapping for {provider!r}. Candidates: "
                    f"{sorted(_MIMESIS_PROVIDER_MAP)!r}."
                ),
            )

    def _emit_locale_fallback(self, provider: str, requested_locale: str) -> None:
        # Deferred import: QualityWarning is the S5-owned event type in the
        # generation.pool layer (R14). Importing it lazily here keeps a static
        # lower(providers_v2)->higher(generation.pool) layer edge out of module
        # load; the dependency only materializes when a fallback actually fires.
        from decoy_engine.generation.pool._events import QualityWarning

        self._warnings.append(
            QualityWarning(
                code="mimesis_locale_fallback",
                provider=provider,
                detail={
                    "requested_locale": requested_locale,
                    "mimesis_locales": list(_SUPPORTED_LOCALES),
                },
            )
        )

    def _fallback_for(self, provider: str, requested_locale: str) -> BackendAdapter:
        if self._fallback is None:
            raise AdapterError(
                code="unsupported_locale",
                message=(
                    f"MimesisAdapter has no Faker fallback configured and locale "
                    f"{requested_locale!r} is outside Mimesis coverage for "
                    f"{provider!r}. Construct MimesisAdapter(fallback=FakerAdapter(...))."
                ),
            )
        self._emit_locale_fallback(provider, requested_locale)
        return self._fallback

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | None = None,
    ) -> Any:
        """Return one masked value (non-deterministic direct-generate path).

        Deterministic mode is the pool path (PoolAdapter wraps this adapter),
        so a direct deterministic request is rejected, Faker-parity.
        """
        if spec.deterministic:
            raise ProviderError(
                code="capability_violation",
                message=(
                    f"MimesisAdapter does not support deterministic mode for "
                    f"{provider!r} at the direct-generate layer (catalog declares "
                    "`supports_deterministic: False`). Route through S5's "
                    "PoolAdapter; determinism is pool-routed via derive_index."
                ),
            )
        self._require_known(provider)
        mlocale = self._mimesis_locale(spec.locale)
        if mlocale is None:
            requested = spec.locale or self._default_locale
            fallback = self._fallback_for(provider, requested)
            return fallback.generate(provider, spec=spec, source_value=source_value)
        return self._call(provider, self._generic(mlocale))

    def generate_batch(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        count: int,
    ) -> Sequence[Any]:
        """Return `count` values. The pool-build hot path (S5).

        When `spec.seed` is set (PoolBuilder passes the derived pool_seed), the
        Mimesis instance is seeded so the batch is reproducible across processes
        (parity item 6). `seed=None` stays random (S4 random-by-default).
        """
        if spec.deterministic:
            raise ProviderError(
                code="capability_violation",
                message=(
                    "generate_batch does not support deterministic mode; "
                    "deterministic callers wrap this adapter in PoolAdapter (S5)."
                ),
            )
        self._require_known(provider)
        mlocale = self._mimesis_locale(spec.locale)
        if mlocale is None:
            requested = spec.locale or self._default_locale
            fallback = self._fallback_for(provider, requested)
            return fallback.generate_batch(provider, spec=spec, count=count)
        seed_int = int.from_bytes(spec.seed, "big") if spec.seed is not None else None
        gen = self._generic(mlocale, seed=seed_int)
        return [self._call(provider, gen) for _ in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        """Return the per-provider capabilities. Self-describing (the adapter
        knows its own candidate set), unlike FakerAdapter's catalog lookup."""
        return mimesis_capability(provider)
