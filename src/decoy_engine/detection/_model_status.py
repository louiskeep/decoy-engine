"""Model status and the detector run log for the spaCy NER model.

`ner_model_state` answers "can this model run here, and at which version"; the gate, the scans and
the setup surfaces read it, none probes on its own. The classifier's counterpart lives in
`profiling/model_pack/_detectors.py`, because detection is the base layer and must not import the
profiling package.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from decoy_engine import reasons
from decoy_engine.detection import ner
from decoy_engine.detection.types import DetectorRun, ModelRef, validate_ner_model_name
from decoy_engine.errors import DecoyError

if TYPE_CHECKING:
    from decoy_engine.context import ExecutionContext

# A child of the root engine logger, so the CLI routes and silences it with the rest. Built with
# getChild so the string-target sentry does not read the logger name as an import path.
_log = logging.getLogger("decoy_engine").getChild("detectors")
_LOCK = threading.Lock()
# (model id, installed version) -> (status, ref). Successes only.
_CACHE: dict[tuple[str, str], tuple[ModelStatus, ModelRef]] = {}
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
        self.reason = qualified(code)
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


def _spacy_version() -> str:
    try:
        return importlib.metadata.version("spacy")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def qualified(code: object) -> str:
    return f"{getattr(code, 'namespace', 'provider')}.{code}"


# The identities a status can carry, keyed as `namespace.code`: a table of registry symbols.
_REASONS = {
    qualified(code): code
    for code in (
        reasons.provider.CLASSIFIER_UNAVAILABLE,
        reasons.provider.NER_SPACY_NOT_INSTALLED,
        reasons.provider.NER_MODEL_NOT_INSTALLED,
        reasons.provider.NER_MODEL_INCOMPATIBLE,
        reasons.config.NER_MODEL_INVALID,
    )
}


def ner_model_state(model: str) -> tuple[ModelStatus, ModelRef | None]:
    """The NER model's status and, when usable, its ref. Never raises, never echoes the name."""
    version: str | None = None
    try:
        validate_ner_model_name(model)
        version = ner.installed_model_version(model)
        with _LOCK:
            hit = _CACHE.get((model, version or ""))
        if hit is not None:
            return hit
        ner.ensure_ner_available(model)
    except ner.NerModelNameError as exc:
        return ModelStatus("ner", "", False, None, None, qualified(exc.code)), None
    except ner.NerUnavailableError as exc:
        return ModelStatus("ner", model, False, version, None, qualified(exc.code)), None
    version = ner.installed_model_version(model)
    if not version:  # removed between the check and here
        gone = qualified(reasons.provider.NER_MODEL_NOT_INSTALLED)
        return ModelStatus("ner", model, False, None, None, gone), None
    status = ModelStatus("ner", model, True, version, _spacy_version(), None)
    ref = ModelRef(model, version)
    with _LOCK:
        _CACHE[(model, version)] = (status, ref)
    return status, ref


def ner_status(model: str = ner.DEFAULT_NER_MODEL) -> ModelStatus:
    return ner_model_state(model)[0]


def refuse(status: ModelStatus) -> NoReturn:
    # Raised from a plain call, never inside an `except`: __cause__ and __context__ stay None,
    # so a loader message (it can carry a pack path label) cannot ride along.
    raise ModelUnavailableError(registered_model_reason(status.reason or ""), cause=status.cause)


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
    for run in runs:
        if run.model is not None and not (
            _SAFE_VALUE.fullmatch(run.model.id) and _SAFE_VALUE.fullmatch(run.model.version)
        ):
            raise ValueError("detector model id and version must be identifiers")
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


def log_detector_models(phase: str, table: str, position: int, counts: Mapping[str, int]) -> None:
    """One INFO record per checked table: `<phase>.models table=<name> <count>=N ...`.

    A table name that is not a plain identifier is logged by its 1-based position (`#3`), so a
    line keeps one token per field.
    """
    if phase not in _PHASES:
        raise ValueError("phase must be pre_scan or post_scan")
    fields = [f"table={table if _SAFE_VALUE.fullmatch(table) else f'#{position}'}"]
    for key, value in counts.items():
        if not isinstance(value, int) or not _SAFE_VALUE.fullmatch(key):
            raise ValueError("detector counts must be identifiers and integers")
        fields.append(f"{key}={value}")
    _log.info("%s.models %s", phase, " ".join(fields))
