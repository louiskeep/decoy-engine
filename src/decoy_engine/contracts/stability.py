"""Stability tier of every public engine name (compatibility contract 3.6).

A name is frozen, experimental or documented internal. The tier belongs to an object, not to a
name: one object exported from two places has one tier. This module holds the rules; the census
in `tests/sentry/release/_census.py` applies them to every export and fails on a name with no
tier, an object with two, or a marker that disagrees with the registry.

Order of the rules for one export: an explicit `TIER_EXCEPTIONS` row; then identity (an object
`sdk` defines, an object in `EXPERIMENTAL_OBJECTS`, or an object a frozen path already exports);
then the package rule. A public subpackage that contract 3.4 does not name is documented
internal.
"""

from __future__ import annotations

from typing import Final

FROZEN: Final = "frozen"
EXPERIMENTAL: Final = "experimental"
INTERNAL: Final = "internal"
TIERS: Final = (FROZEN, EXPERIMENTAL, INTERNAL)

# A class or function docstring starts with this; a constant's module docstring does.
EXPERIMENTAL_MARKER: Final = "Experimental:"

# Modules whose exports are frozen whole (contract 3.4: `__init__.py` and `sdk.py`). Every other
# public package defaults to INTERNAL, unless one of its objects is also exported by a frozen
# path or carries a FROZEN row in `TIER_EXCEPTIONS`.
PACKAGE_TIERS: Final[dict[str, str]] = {
    "decoy_engine": FROZEN,
    "decoy_engine.sdk": FROZEN,
}

# `(module, name, tier)`: a name that must differ from its package rule. These are the subpackage
# names section 3.4 lists explicitly; each object stays frozen under every import path.
TIER_EXCEPTIONS: Final[tuple[tuple[str, str, str], ...]] = (
    ("decoy_engine.detection", "DetectorRun", FROZEN),
    ("decoy_engine.detection", "ModelRef", FROZEN),
    ("decoy_engine.detection", "NerRate", FROZEN),
    ("decoy_engine.detection", "log_detector_models", FROZEN),
    ("decoy_engine.detection", "log_detector_run", FROZEN),
    ("decoy_engine.detection", "ner_rates", FROZEN),
    ("decoy_engine.plan.checks", "StrategyRequirement", FROZEN),
    ("decoy_engine.profile", "profile_relationships_from_config", FROZEN),
    ("decoy_engine.profiling", "ModelHit", FROZEN),
)

# Everything `sdk.py` defines is experimental, derived by the census: a class or function whose
# `__module__` is this module, and a module-level constant assigned in its source. `ConfigError`
# and `ConnectorError` are `errors` objects that `sdk` re-exports, so they stay frozen.
EXPERIMENTAL_MODULE: Final = "decoy_engine.sdk"

# Experimental objects that live outside `sdk`, by identity: `(defining module, qualified name)`.
# Every export of the object, at the root, in `providers` or elsewhere, takes the tier.
EXPERIMENTAL_OBJECTS: Final[tuple[tuple[str, str], ...]] = (
    ("decoy_engine.internal.faker_setup", "atomic_swap_db_providers"),
    ("decoy_engine.internal.faker_setup", "load_custom_providers"),
    ("decoy_engine.internal.faker_setup", "register_faker_list_provider"),
    ("decoy_engine.internal.faker_setup", "register_faker_provider"),
    ("decoy_engine.internal.faker_setup", "unregister_faker_provider"),
    ("decoy_engine.providers_v2._faker_adapter", "register_faker_provider_v2"),
)

# Public packages (paths under `decoy_engine`) that declare no `__all__`, so export nothing the
# census can classify. The list is exact and counted: a new package without `__all__` fails the
# census until it declares one or is added here with a reason in review.
NO_ALL_PACKAGES: Final[tuple[str, ...]] = (
    "codesets",
    "contracts",
    "detection.name_hints",
    "execution.native.chunked",
    "execution.native.protocol",
    "fileaccess.adapters",
    "internal",
    "reasons.config",
    "reasons.connector",
    "reasons.decoy",
    "reasons.physical",
    "reasons.plan",
    "reasons.platform",
    "reasons.post_scan",
    "reasons.pre_scan",
    "reasons.provider",
    "reasons.quality",
    "reasons.route",
    "reasons.runtime",
    "reasons.safe_output",
    "reasons.subset",
    "reasons.verification",
    "reasons.warning",
)

# A public package that needs an optional extra, keyed by its path under `decoy_engine`: the one
# dependency whose absence the census tolerates. Its declared names are then classified from
# source. Any other import error fails. The key is relative so the string-target sentry, which
# resolves every `decoy_engine.` path, does not need the extra installed.
OPTIONAL_EXTRAS: Final[dict[str, str]] = {"providers_v2.mimesis": "mimesis"}


def package_tier(module: str) -> str:
    """The tier a name exported by `module` gets when no other rule applies."""
    return PACKAGE_TIERS.get(module, INTERNAL)


# Every `"<name>/vN"` string or bytes constant under `src/` is one of four things (contract 3.1,
# 3.3, the declared-casting registries, this table): a frozen format named in contract 3.1, a
# frozen derivation label listed in contract 3.3, a declared-casting format id (the F06 parse and
# output registries, documented in `docs/declared-casting.md`), or an internal tag listed here.
# The rule: a tag that reaches disk, `--json`, a signed or hashed record or a derivation input is
# frozen; a tag that never leaves the process is internal and needs a reason. `tests/sentry/release/test_tag_census.py` holds the four sets disjoint and
# complete, and scans written artifacts for every tag in this table. Tags formatted in the middle
# or built by concatenation are a known limit of the census.
INTERNAL_TAGS: Final[dict[str, tuple[str, str]]] = {
    "decoy-telemetry/v1": (
        "telemetry/_vocab.py",
        "names the closed span vocabulary; no span attribute, file or --json carries it",
    ),
}

# Derivation labels: the tag is an input to a key, a digest or a seed, so changing it changes
# output bytes or a stored digest. Contract 3.3 lists the same set. Value: the owning module.
DERIVATION_LABELS: Final[dict[str, str]] = {
    "canary-span-normalization/v1": "verification/canary/_types.py",
    "decoy-engine/determinism/v1": "determinism/_derive.py",
    "decoy-engine/keyprovider/v1": "keyprovider.py",
    "decoy-engine/protected-quarantine/v1": "declared_casting/_quarantine_local.py",
    "decoy-probe/v1": "support/_probe.py",
    "decoy-publication-result/v1": "publication/_descriptor.py",
    "decoy-resume-key-check/v1": "internal/crypto.py",
    "decoy-vault-key-check/v1": "internal/crypto.py",
    "fpe-sub-floor-synthetic/v1": "transforms/_text_mask_synthetic.py",
    "decoy.shuffle.unkeyed/v1": "execution/_strategies/_shuffle.py",
    "joint_mask/gen_row/v1": "transforms/joint_mask.py",
    "joint_mask/keyed_row/v1": "transforms/joint_mask.py",
    "joint_mask/null_fallback/v1": "transforms/joint_mask.py",
    "name_email/v1": "generation/composite/_name_email.py",
    "pinned-plan-fingerprint/v1": "generation/_pinned_plan.py",
    "tokenize-key/v1": "execution/_strategies/_tokenize.py",
}
