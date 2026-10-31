"""Catalog of the ``runtime`` namespace: secret-provider admission (F13)."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Outcome, Phase, RetryClass

__all__ = [
    "SECRET_ACCESS_DENIED",
    "SECRET_EXPIRED",
    "SECRET_FILE_PERMISSIONS_UNSAFE",
    "SECRET_LEASE_UNSUPPORTED",
    "SECRET_MATERIAL_INVALID",
    "SECRET_PROVIDER_UNAVAILABLE",
    "SECRET_PROVIDER_UNKNOWN",
    "SECRET_VERSION_UNAVAILABLE",
]

_d = _catalog.runtime("SecretProviderError")

SECRET_PROVIDER_UNKNOWN = _d(
    "secret_provider_unknown",
    "The referenced secret provider is not registered.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)
SECRET_PROVIDER_UNAVAILABLE = _d(
    "secret_provider_unavailable",
    "The secret provider could not complete a safe read.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.EXECUTION,
    retry=RetryClass.RETRY_OPERATION,
)
SECRET_ACCESS_DENIED = _d(
    "secret_access_denied",
    "The workload is not permitted to read the pinned secret.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.ENVIRONMENT_REPAIR,
)
SECRET_VERSION_UNAVAILABLE = _d(
    "secret_version_unavailable",
    "The pinned secret version is absent, deleted, destroyed, or inaccessible.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)
SECRET_EXPIRED = _d(
    "secret_expired",
    "The selected secret is outside its usable lifetime.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)
SECRET_LEASE_UNSUPPORTED = _d(
    "secret_lease_unsupported",
    "The selected secret requires a dynamic lease that cannot be safely maintained.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)
SECRET_MATERIAL_INVALID = _d(
    "secret_material_invalid",
    "The secret material does not meet the selected algorithm contract.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)
SECRET_FILE_PERMISSIONS_UNSAFE = _d(
    "secret_file_permissions_unsafe",
    "The secret file is readable or writable by its group or by others.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)
