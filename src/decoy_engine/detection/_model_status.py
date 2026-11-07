"""Model status, strict loading and the detector run log for the ML detectors (SCAN-ML M1).

One probe answers "can this model run here, and at which version" for the lgbm classifier and the
spaCy NER model. The gate, the scans and the setup surfaces read it; none probes on its own.

The load is strict: `load_detectors` returns a set only when every requested model is usable, and
raises `ModelUnavailableError` otherwise. It never returns a partial or regex-only set, because
the caller asked for a model and a quiet fallback would hide that no model check ran. The lenient
loader (`load_with_fallback`, used by `classify_fields`) is never called here.

Results are cached per process, keyed by (source, model id, installed version), so a model upgrade
inside a long-lived worker is a new key rather than a stale hit. Failures are not cached.

Names, ids, versions and counts are the only things that leave this module (the leak rule in
`progress.py`: where and how much, never what).
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from decoy_engine import reasons
from decoy_engine.detection import ner
from decoy_engine.detection.types import (
    DetectorRun,
    DetectorSource,
    ModelRef,
    validate_ner_model_name,
)
from decoy_engine.errors import DecoyError

if TYPE_CHECKING:
    from decoy_engine.context import ExecutionContext
    from decoy_engine.fileaccess import FileAccess

# A child of the root engine logger, so the CLI routes and silences it with the rest. Built with
# getChild so the string-target sentry does not read the logger name as an import path.
_log = logging.getLogger("decoy_engine").getChild("detectors")
_LOCK = threading.Lock()
# (source, model id, installed version) -> (status, loaded pack or ModelRef). Successes only.
_CACHE: dict[tuple[str, str, str], tuple[ModelStatus, Any]] = {}
_ORDER: tuple[DetectorSource, ...] = ("regex", "classifier", "ner")
_PHASES = ("pre_scan", "post_scan")
_SAFE_VALUE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class ModelUnavailableError(DecoyError):
    """A requested model cannot run here. Carries a reason identity and a cause class, no path.

    `reason` is the qualified identity (`provider.classifier_unavailable`); `code` is its bare
    registered code, which the reason adapter reads. `cause_class` is the loader's `disabled`,
    `import` or `pack` for the classifier and `None` for NER.
    """

    def __init__(self, code: str, *, cause: str | None = None) -> None:
        self.code = code
        self.reason = _qualified(code)
        self.cause_class = cause
        super().__init__(self.reason if cause is None else f"{self.reason} (cause: {cause})")

    def __reduce__(self) -> tuple[Any, ...]:
        return (_rebuild_unavailable, (self.reason, self.cause_class))


def registered_model_reason(reason: str) -> str:
    """The registry symbol (a `ReasonCode`, which carries its namespace) for a qualified identity."""
    return _REASONS[reason]


def _rebuild_unavailable(reason: str, cause: str | None) -> ModelUnavailableError:
    return ModelUnavailableError(registered_model_reason(reason), cause=cause)


@dataclass(frozen=True)
class ModelStatus:
    source: Literal["classifier", "ner"]
    model_id: str
    available: bool
    version: str | None  # installed version when available
    runtime_version: str | None  # lightgbm or spaCy version
    reason: str | None  # qualified reason identity when not available
    cause: str | None = None  # classifier only: disabled | import | pack


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
        return tuple(DetectorRun(s, models[s]) for s in _ORDER if s in self.sources)


def _spacy_version() -> str:
    try:
        return importlib.metadata.version("spacy")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _qualified(code: object) -> str:
    return f"{getattr(code, 'namespace', 'provider')}.{code}"


# The identities a status can carry, keyed as `namespace.code`: a table of registry symbols.
_REASONS = {
    _qualified(code): code
    for code in (
        reasons.provider.CLASSIFIER_UNAVAILABLE,
        reasons.provider.NER_SPACY_NOT_INSTALLED,
        reasons.provider.NER_MODEL_NOT_INSTALLED,
        reasons.provider.NER_MODEL_INCOMPATIBLE,
        reasons.config.NER_MODEL_INVALID,
    )
}


def _classifier(file_access: FileAccess | None = None) -> tuple[ModelStatus, dict[str, Any] | None]:
    """The classifier's status and, when usable, its strictly loaded pack. Never raises."""
    from decoy_engine import fileaccess
    from decoy_engine.profiling.model_pack.classify import _DEFAULT_PACK_ID, _default_pack_dir
    from decoy_engine.profiling.model_pack.loader import ModelPackLoader, ModelPackLoadError

    def down(cause: str) -> tuple[ModelStatus, None]:
        reason = _qualified(reasons.provider.CLASSIFIER_UNAVAILABLE)
        return ModelStatus("classifier", _DEFAULT_PACK_ID, False, None, None, reason, cause), None

    loader = ModelPackLoader(
        fileaccess.engine_dir_handle(_default_pack_dir()), trusted=True, file_access=file_access
    )
    if loader.is_ml_disabled:
        return down("disabled")
    try:  # a missing libomp raises OSError here, not ImportError
        import lightgbm
        import sklearn  # noqa: F401
    except Exception:
        return down("import")
    try:
        manifest = loader.read_manifest()
        key = ("classifier", manifest.pack_id, manifest.version)
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


def _ner(model: str) -> tuple[ModelStatus, ModelRef | None]:
    """The NER model's status and, when usable, its ref. Never raises, never echoes the name."""
    try:
        validate_ner_model_name(model)
        version = ner.installed_model_version(model)
        key = ("ner", model, version or "")
        with _LOCK:
            hit = _CACHE.get(key)
        if hit is not None:
            return hit[0], hit[1]
        ner.ensure_ner_available(model)
    except ner.NerModelNameError as exc:
        return ModelStatus("ner", "", False, None, None, _qualified(exc.code)), None
    except ner.NerUnavailableError as exc:
        status = ModelStatus("ner", model, False, version, None, _qualified(exc.code))
        return status, None
    status = ModelStatus("ner", model, True, version, _spacy_version(), None)
    ref = ModelRef(model, version or "")
    with _LOCK:
        _CACHE[key] = (status, ref)
    return status, ref


def classifier_status() -> ModelStatus:
    return _classifier()[0]


def ner_status(model: str = ner.DEFAULT_NER_MODEL) -> ModelStatus:
    return _ner(model)[0]


def _refuse(status: ModelStatus) -> NoReturn:
    # Raised from a plain call, never inside an `except`: __cause__ and __context__ stay None,
    # so a loader message (it can carry a pack path label) cannot ride along.
    raise ModelUnavailableError(registered_model_reason(status.reason or ""), cause=status.cause)


def load_detectors(
    sources: Sequence[DetectorSource],
    *,
    ner_model: str,
    file_access: FileAccess | None = None,
) -> DetectorSet:
    """Strictly load every requested model; raise `ModelUnavailableError` if any cannot run."""
    if any(source not in _ORDER for source in sources):
        raise ValueError("unknown detector source")
    wanted = tuple(source for source in _ORDER if source in sources)
    pack: dict[str, Any] | None = None
    ref: ModelRef | None = None
    if "classifier" in wanted:
        status, pack = _classifier(file_access)
        if not status.available:
            _refuse(status)
    if "ner" in wanted:
        validate_ner_model_name(ner_model)  # a bad name is a config error, not an absent model
        status, ref = _ner(ner_model)
        if not status.available:
            _refuse(status)
    return DetectorSet(wanted, pack, ref)


def log_detector_run(
    phase: str,
    runs: Sequence[DetectorRun],
    counts: Mapping[str, int | str],
    *,
    ctx: ExecutionContext | None = None,
) -> None:
    """One INFO record per phase: `<phase>.detectors sources=... <model>=id@ver spacy=... tables=N`.

    `counts` carries `tables` and `columns` first, then any further keys the phase adds. Only
    identifiers and integers are accepted, so nothing a model read can reach the log.
    """
    if phase not in _PHASES:
        raise ValueError("phase must be pre_scan or post_scan")
    fields = [f"sources={','.join(run.source for run in runs)}"]
    fields += [
        f"{run.source}={run.model.id}@{run.model.version}"
        for run in runs
        if run.source != "regex" and run.model is not None
    ]
    if any(run.source == "ner" for run in runs):
        fields.append(f"spacy={_spacy_version()}")
    ordered = ["tables", "columns", *(k for k in counts if k not in ("tables", "columns"))]
    for key in ordered:
        value = counts.get(key, 0)
        if not _SAFE_VALUE.fullmatch(str(value)) or not _SAFE_VALUE.fullmatch(key):
            raise ValueError("detector counts must be identifiers or integers")
        fields.append(f"{key}={value}")
    name = f"{phase}.detectors"
    _log.info("%s %s", name, " ".join(fields))
    if ctx is not None:
        from decoy_engine.context import emit_step

        emit_step(ctx.logger, name, status="finish")
