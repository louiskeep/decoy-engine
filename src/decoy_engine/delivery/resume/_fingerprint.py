"""The value-free key a resume store is tied to: masking semantics, versions, key, chunk size.

Pattern: key check value (KCV, payment HSM practice): a short MAC of a fixed label under the
key proves "same key" without storing it. The same key-confirmation idea is used by the engine's
`derive_key_digest` in `generation/_reproducibility.py`; here the digest is truncated and lives in
a file on disk, so it is not the generation envelope itself.

`build_fingerprint` takes the component versions as an argument. The engine does not know which
CLI distribution drives it, so the caller collects `(distribution, version)` pairs once and
injects them. Per-table source and target binding is `binding_digest`, built on the engine's one
canonical JSON digest (`records.payload_digest`); no second hasher exists.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from decoy_engine.detection.ner import installed_model_version
from decoy_engine.internal.crypto import RESUME_KEY_CHECK_LABEL, key_check_value
from decoy_engine.keyprovider import KeyProvider, mask_key_from_provider
from decoy_engine.plan import job_seed_for_config, pipeline_config_hash, run_ner_models
from decoy_engine.records import payload_digest

__all__ = ["RunFingerprint", "binding_digest", "build_fingerprint"]

# The version of a key the engine derives from the job seed, mirroring `SeedKeyProvider`.
_SEED_KEY_VERSION = "seed"
# Engine-owned component names: a caller cannot supply one, so it cannot forge or hide a model.
_NER_PREFIX = "decoy-ner:"


def _ner_components(config: dict[str, Any]) -> list[tuple[str, str]]:
    """One `(decoy-ner:<surface>, model==version[,...])` pair per NER surface the run uses."""
    by_surface: dict[str, list[str]] = {}
    for surface, model in run_ner_models(config):
        version = installed_model_version(model) or "unavailable"
        by_surface.setdefault(surface, []).append(f"{model}=={version}")
    return [(_NER_PREFIX + surface, ",".join(sorted(ids))) for surface, ids in by_surface.items()]


@dataclass(frozen=True)
class RunFingerprint:
    """Every run-level input a kept table depends on. Holds no key bytes and no value."""

    config_hash: str
    components: tuple[tuple[str, str], ...]
    key_check: str
    key_version: str
    chunk_size: int

    @property
    def fingerprint_digest(self) -> str:
        """The tagged sha256 of the canonical JSON of the run-level fields."""
        body = {
            "config_hash": self.config_hash,
            "components": [list(pair) for pair in self.components],
            "key_check": self.key_check,
            "key_version": self.key_version,
            "chunk_size": self.chunk_size,
        }
        return payload_digest(body)


def binding_digest(source: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    """The tagged sha256 of one table's source and target spec.

    `pipeline_config_hash` drops `sources` and `targets`, so this is what notices a changed path
    or format.
    """
    return payload_digest({"source": _plain(source), "target": _plain(target)})


def _plain(value: Any) -> Any:
    # The canonical encoder takes plain dicts and lists only; any read-only mapping hashes alike.
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def build_fingerprint(
    config: dict[str, Any],
    key_provider: KeyProvider | None,
    *,
    chunk_size: int,
    components: Iterable[tuple[str, str]],
) -> RunFingerprint:
    """The fingerprint of a run, from its effective config and admitted key provider.

    `key_provider=None` is the seed path: the key is the job seed, exactly as the masking
    strategies derive it, so no seed logic is copied here.
    """
    given = [tuple(pair) for pair in components]
    if any(len(pair) != 2 or not all(type(part) is str for part in pair) for pair in given):
        raise TypeError("each component must be a (distribution, version) pair of strings")
    if any(pair[0].startswith(_NER_PREFIX) for pair in given):
        raise ValueError("component names starting with decoy-ner: are reserved")
    if not given:
        raise ValueError("a resume fingerprint needs at least one component version")
    pairs = tuple(sorted((pair[0], pair[1]) for pair in [*given, *_ner_components(config)]))
    config_hash = pipeline_config_hash(config)
    version = key_provider.key_version if key_provider is not None else _SEED_KEY_VERSION
    # The key is produced inside `key_check_value`'s MAC call, so no frame here names it.
    key_check = key_check_value(
        lambda: mask_key_from_provider(key_provider, job_seed_for_config(config)),
        RESUME_KEY_CHECK_LABEL,
    )
    return RunFingerprint(
        config_hash=config_hash,
        components=pairs,
        key_check=key_check,
        key_version=version,
        chunk_size=chunk_size,
    )
