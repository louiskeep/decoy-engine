"""Versioned secret-provider contract (``secret-provider/v1``), preflight and adapters.

The engine owns the interface; consumers (CLI, platform) authenticate and authorize before
invoking a provider. See docs/security/secret-providers.md.
"""

from __future__ import annotations

from decoy_engine.secret_providers._buffer import SecretBuffer
from decoy_engine.secret_providers._cache import SecretCache, default_cache
from decoy_engine.secret_providers._errors import (
    SecretAccessDenied,
    SecretExpired,
    SecretFilePermissionsUnsafe,
    SecretLeaseUnsupported,
    SecretMaterialInvalid,
    SecretProviderError,
    SecretProviderUnavailable,
    SecretProviderUnknown,
    SecretVersionUnavailable,
)
from decoy_engine.secret_providers._preflight import (
    AdmissionScope,
    AdmittedKey,
    admit_mask_key,
    assert_plan_requirements_admitted,
    prepare_legacy,
    prepare_ref,
    prepare_secret,
)
from decoy_engine.secret_providers._registry import (
    SecretProviderRegistry,
    default_registry,
    register_secret_provider,
)
from decoy_engine.secret_providers._requirements import (
    AES_GCM_V1,
    FERNET_V1,
    HMAC_SHA256_V1,
    MASK_ROOT_V1,
    REQUIREMENTS,
    SecretRequirement,
    secret_requirements_for_config,
    secret_requirements_for_plan,
)
from decoy_engine.secret_providers._types import (
    API_VERSION,
    EXPIRY_SKEW_SECONDS,
    MAX_CACHE_TTL_SECONDS,
    PreparedKeyProvider,
    PreparedSecret,
    ResolveContext,
    ResolvedSecret,
    SecretCachePolicy,
    SecretDescriptor,
    SecretProvider,
    SecretRef,
    parse_ref,
)

__all__ = [
    "AES_GCM_V1",
    "API_VERSION",
    "EXPIRY_SKEW_SECONDS",
    "FERNET_V1",
    "HMAC_SHA256_V1",
    "MASK_ROOT_V1",
    "MAX_CACHE_TTL_SECONDS",
    "REQUIREMENTS",
    "AdmissionScope",
    "AdmittedKey",
    "PreparedKeyProvider",
    "PreparedSecret",
    "ResolveContext",
    "ResolvedSecret",
    "SecretAccessDenied",
    "SecretBuffer",
    "SecretCache",
    "SecretCachePolicy",
    "SecretDescriptor",
    "SecretExpired",
    "SecretFilePermissionsUnsafe",
    "SecretLeaseUnsupported",
    "SecretMaterialInvalid",
    "SecretProvider",
    "SecretProviderError",
    "SecretProviderRegistry",
    "SecretProviderUnavailable",
    "SecretProviderUnknown",
    "SecretRef",
    "SecretRequirement",
    "SecretVersionUnavailable",
    "admit_mask_key",
    "assert_plan_requirements_admitted",
    "default_cache",
    "default_registry",
    "parse_ref",
    "prepare_legacy",
    "prepare_ref",
    "prepare_secret",
    "register_secret_provider",
    "secret_requirements_for_config",
    "secret_requirements_for_plan",
]
