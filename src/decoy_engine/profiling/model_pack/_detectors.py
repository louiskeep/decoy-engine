"""Strict loading of the ML detectors: classifier status and `load_detectors`.

Lives under profiling because the classifier is a profiling model pack and detection, the base
layer, never imports profiling. The NER half (status, ref, refusal, log line) is in
`detection/_model_status.py`.

The load is strict: `load_detectors` returns a set only when every requested model is usable, and
raises `ModelUnavailableError` otherwise. It never returns a partial or regex-only set, because
the caller asked for a model and a quiet fallback would hide that no model check ran. The lenient
loader (`load_with_fallback`, used by `classify_fields`) is never called here.

Results are cached per process, keyed by (pack id, version), so a pack upgrade inside a
long-lived worker is a new key rather than a stale hit. Failures are not cached.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from decoy_engine import reasons
from decoy_engine.detection import ModelStatus, ner_model_state, qualified, refuse
from decoy_engine.detection.types import (
    DETECTOR_ORDER,
    DetectorRun,
    DetectorSource,
    ModelRef,
    validate_ner_model_name,
)

if TYPE_CHECKING:
    from decoy_engine.fileaccess import FileAccess

_LOCK = threading.Lock()
# (pack id, version) -> (status, loaded pack). Successes only.
_CACHE: dict[tuple[str, str], tuple[ModelStatus, dict[str, Any]]] = {}


@dataclass(frozen=True)
class DetectorSet:
    """What a scan or run was asked to use, with every requested model strictly loaded."""

    sources: tuple[DetectorSource, ...]
    classifier_pack: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    ner: ModelRef | None = None

    @property
    def classifier(self) -> ModelRef | None:
        if self.classifier_pack is None:
            return None
        manifest = self.classifier_pack["manifest"]
        return ModelRef(manifest.pack_id, manifest.version)

    def runs(self) -> tuple[DetectorRun, ...]:
        models = {"regex": None, "classifier": self.classifier, "ner": self.ner}
        return tuple(DetectorRun(s, models[s]) for s in DETECTOR_ORDER if s in self.sources)


def _classifier(file_access: FileAccess | None = None) -> tuple[ModelStatus, dict[str, Any] | None]:
    """The classifier's status and, when usable, its strictly loaded pack. Never raises."""
    from decoy_engine.profiling.model_pack.classify import _DEFAULT_PACK_ID, default_pack_loader
    from decoy_engine.profiling.model_pack.loader import ModelPackLoadError

    def down(cause: str) -> tuple[ModelStatus, None]:
        reason = qualified(reasons.provider.CLASSIFIER_UNAVAILABLE)
        return ModelStatus("classifier", _DEFAULT_PACK_ID, False, None, None, reason, cause), None

    loader = default_pack_loader(file_access)
    if loader.is_ml_disabled:
        return down("disabled")
    try:  # a missing libomp raises OSError here, not ImportError
        import lightgbm
        import sklearn  # noqa: F401
    except Exception:
        return down("import")
    try:
        manifest = loader.read_manifest()
        key = (manifest.pack_id, manifest.version)
        with _LOCK:
            hit = _CACHE.get(key)
            if hit is not None:
                return hit[0], hit[1]
            pack = loader.load()  # strict: raises rather than returning None
            status = ModelStatus(
                "classifier", manifest.pack_id, True, manifest.version, lightgbm.__version__, None
            )
            _CACHE[key] = (status, pack)
            return status, pack
    except ModelPackLoadError as exc:
        return down(exc.cause)
    except Exception:
        return down("pack")


def classifier_status() -> ModelStatus:
    return _classifier()[0]


def load_detectors(
    sources: Sequence[DetectorSource],
    *,
    ner_model: str,
    file_access: FileAccess | None = None,
) -> DetectorSet:
    """Strictly load every requested model; raise `ModelUnavailableError` if any cannot run."""
    if any(source not in DETECTOR_ORDER for source in sources):
        raise ValueError("unknown detector source")
    wanted = tuple(source for source in DETECTOR_ORDER if source in sources)
    pack: dict[str, Any] | None = None
    ref: ModelRef | None = None
    if "classifier" in wanted:
        status, pack = _classifier(file_access)
        if not status.available:
            refuse(status)
    if "ner" in wanted:
        validate_ner_model_name(ner_model)  # a bad name is a config error, not an absent model
        status, ref = ner_model_state(ner_model)
        if not status.available:
            refuse(status)
    return DetectorSet(wanted, pack, ref)
