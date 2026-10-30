"""F13 §5 acceptance 10: a seeded marker never reaches any engine-visible surface.

A unique marker is placed in the secret VALUE, in a provider exception payload, and in the
reference path/field. Success and failure are exercised through the direct engine, and every
captured string/serialized artifact is scanned for the marker and its hex/base64 forms, plus the
derived mask key. Positive controls prove the expected fixed reason and ordinary output were
captured, so an empty capture cannot pass vacuously.

Rendered surfaces include tracebacks with ``capture_locals=True`` (the mode that once leaked raw
material from validation frames), a provider-raised ``MaskSecretError`` that carries a value,
captured stdout/stderr (``print``, ``print_exception``, ``sys.excepthook``) and a structured JSON
audit record of the error's attributes. Pre-fix red runs are recorded in
docs/plans/evidence/2026-10-30-f13-r2-red-baseline.txt.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import logging
import os
import pickle
import sys
import traceback
from dataclasses import dataclass

import pytest

from decoy_engine import run_mask_pipeline_chunked, run_pipeline
from decoy_engine.keyprovider import MaskSecretError, MissingMaskSecret, SecretKeyProvider
from decoy_engine.progress import CallbackSink, event_to_dict
from decoy_engine.secret_providers import (
    AES_GCM_V1,
    API_VERSION,
    FERNET_V1,
    HMAC_SHA256_V1,
    MASK_ROOT_V1,
    REQUIREMENTS,
    SecretAccessDenied,
    SecretBuffer,
    SecretCachePolicy,
    SecretDescriptor,
    SecretProviderRegistry,
    SecretProviderUnavailable,
    admit_mask_key,
    parse_ref,
    prepare_legacy,
    prepare_secret,
)
from tests.secret_providers._helpers import (
    ENGINE_VERSION,
    ProtocolProvider,
    TouchedSource,
    keyed_config,
)

MARKER = b"F13-SEEDED-MARKER-7f3a9c1d-do-not-leak!"  # 39 bytes
assert len(MARKER) >= 32


def _forms(extra: bytes = b"") -> list[str]:
    derived = SecretKeyProvider(MARKER, key_version="v1").mask_key()
    out = []
    for b in (MARKER, derived, extra):
        if b:
            out += [
                b.decode("latin-1"),
                b.hex(),
                base64.b64encode(b).decode(),
                base64.urlsafe_b64encode(b).decode(),
            ]
    return out


def assert_clean(*surfaces: object) -> None:
    blob = "\n".join(s if isinstance(s, str) else repr(s) for s in surfaces)
    for needle in _forms():
        assert needle not in blob, "secret material reached an engine-visible surface"


class SDKBoomError(Exception):
    """Stands in for a vendor SDK exception whose text carries paths and the secret."""


class LeakyProvider(ProtocolProvider):
    def resolve(self, ref):  # type: ignore[override]
        self.resolve_calls += 1
        raise SDKBoomError(
            f"GET /v1/kv/F13-PATH-MARKER denied; body={MARKER!r}; token=F13-TOKEN-MARKER"
        )


def test_secret_bearing_objects_have_constant_redacted_renderings():
    buf = SecretBuffer(MARKER)
    prepared = prepare_secret(
        provider=ProtocolProvider(value=MARKER),
        ref=parse_ref("test:x?encoding=raw"),
        requirements=(REQUIREMENTS[MASK_ROOT_V1],),
    )
    for obj in (buf, prepared, prepared.key_provider(), parse_ref("env:F13-PATH-MARKER")):
        assert_clean(repr(obj), str(obj), f"{obj}", f"{obj!r:>80}")
        assert "F13-PATH-MARKER" not in repr(obj) + str(obj)
    for obj in (buf, prepared, prepared.key_provider()):
        with pytest.raises(TypeError):
            pickle.dumps(obj)
        with pytest.raises(TypeError):
            json.dumps(obj)
    with pytest.raises(TypeError):
        copy.copy(buf)
    with pytest.raises(TypeError):
        copy.deepcopy(buf)


def test_close_wipes_the_owned_buffer():
    buf = SecretBuffer(MARKER)
    view = buf.view()
    buf.close()
    assert bytes(view) == bytes(len(MARKER))  # the owned storage itself was overwritten


def test_sdk_exception_text_never_survives_the_choke_point(caplog):
    prov = LeakyProvider()
    with pytest.raises(SecretProviderUnavailable) as info:
        prepare_secret(
            provider=prov,
            ref=parse_ref("test:F13-PATH-MARKER?encoding=raw"),
            requirements=(REQUIREMENTS[MASK_ROOT_V1],),
        )
    exc = info.value
    assert exc.__cause__ is None and exc.__context__ is None
    assert info.value.code == "secret_provider_unavailable"  # positive control
    logger = logging.getLogger("f13.sentry")
    with caplog.at_level(logging.DEBUG, logger="f13.sentry"):
        try:
            raise exc
        except MaskSecretError:
            logger.exception("admission failed")
    rendered = "".join(traceback.format_exception(exc)) + caplog.text + str(exc) + repr(exc)
    assert "admission failed" in caplog.text
    for needle in ("F13-PATH-MARKER", "F13-TOKEN-MARKER", "SDKBoomError", *_forms()):
        assert needle not in rendered


def test_failure_through_the_pipeline_is_value_free(tmp_path, registry, caplog):
    registry.register(LeakyProvider("leaky"))
    cfg = keyed_config(tmp_path, mask_secret_ref="leaky:F13-PATH-MARKER")
    events = []
    with pytest.raises(MaskSecretError) as info:
        run_pipeline(cfg, engine_version=ENGINE_VERSION, progress=CallbackSink(events.append))
    exc = info.value
    assert exc.code == "secret_provider_unavailable"
    rendered = "".join(traceback.format_exception(exc)) + str(exc) + repr(exc)
    rendered += json.dumps([event_to_dict(e) for e in events], default=str)
    for needle in ("F13-PATH-MARKER", "F13-TOKEN-MARKER", "SDKBoomError", *_forms()):
        assert needle not in rendered

    source = TouchedSource()
    with pytest.raises(MaskSecretError) as chunk_info:
        list(run_mask_pipeline_chunked(cfg, source, table="t", engine_version=ENGINE_VERSION))
    assert chunk_info.value.code == "secret_provider_unavailable" and source.touched == 0
    assert "F13-PATH-MARKER" not in "".join(traceback.format_exception(chunk_info.value))


def test_success_surfaces_do_not_carry_the_secret(tmp_path, registry):
    registry.register(ProtocolProvider("ok", value=MARKER))
    cfg = keyed_config(tmp_path, mask_secret_ref="ok:F13-PATH-MARKER?encoding=raw")
    import pandas as pd

    (tmp_path / "t.csv").write_text("email\n" + "\n".join(f"u{i}@x.test" for i in range(5)) + "\n")
    events = []
    import pyarrow as pa

    sources = {"t": pa.Table.from_pandas(pd.read_csv(tmp_path / "t.csv"), preserve_index=False)}
    result = run_pipeline(
        cfg, sources, engine_version=ENGINE_VERSION, progress=CallbackSink(events.append)
    )
    assert len(result.outputs["t"]) == 5  # positive control: ordinary output was produced
    from decoy_engine.plan._serialize import plan_to_yaml

    surfaces = [
        repr(result),
        str(result),
        plan_to_yaml(result.plan) if getattr(result, "plan", None) is not None else "",
        json.dumps([event_to_dict(e) for e in events], default=str),
        pd.DataFrame(result.outputs["t"].to_pandas()).to_csv(),
        json.dumps(cfg, default=str),
    ]
    assert_clean(*surfaces)
    assert "F13-PATH-MARKER" not in "".join(
        s for s in surfaces[:4]
    )  # the ref locator stays out too


# --- r2: traceback locals, value-bearing provider errors, stdio and an audit surface ---------

LOCALS_MARKER = "F13LOCALS"

# case -> (ref, requirement id, value, expected code). Each value carries LOCALS_MARKER and fails
# one validation step after the provider read, while the material sits in engine frames.
VALIDATION_FAILURES = {
    "mask-root-legacy-decoding": (
        "test:x",
        MASK_ROOT_V1,
        b"F13LOCALS-not-hex-and-not-base64!!",
        "bad_secret_ref",
    ),
    "mask-root-bad-hex": ("test:x?encoding=hex", MASK_ROOT_V1, b"F13LOCALS-zz", "bad_secret_ref"),
    "mask-root-weak": ("test:x?encoding=raw", MASK_ROOT_V1, b"F13LOCALS-weak", "weak_mask_secret"),
    "fernet-needs-encoding": ("test:x", FERNET_V1, b"F13LOCALS-fernet-text", "bad_secret_ref"),
    "fernet-invalid": (
        "test:x?encoding=raw",
        FERNET_V1,
        b"F13LOCALS-not-a-fernet-key",
        "secret_material_invalid",
    ),
    "hmac-short": (
        "test:x?encoding=raw",
        HMAC_SHA256_V1,
        b"F13LOCALS-hmac",
        "secret_material_invalid",
    ),
    "aes-gcm-length": (
        "test:x?encoding=raw",
        AES_GCM_V1,
        b"F13LOCALS-aes-gcm-20B",
        "secret_material_invalid",
    ),
}


def _value_forms(value: bytes) -> list[str]:
    forms = [value.decode("latin-1"), value.hex(), base64.b64encode(value).decode()]
    return [*forms, LOCALS_MARKER, LOCALS_MARKER.encode().hex()]


def _provoke(case: str) -> BaseException:
    # The value never becomes a local of the frame the caught traceback starts from.
    ref, req_id = VALIDATION_FAILURES[case][:2]
    provider = ProtocolProvider(value=VALIDATION_FAILURES[case][2])
    try:
        prepare_secret(provider=provider, ref=parse_ref(ref), requirements=(REQUIREMENTS[req_id],))
    except MaskSecretError as exc:
        return exc
    raise AssertionError("validation unexpectedly passed")


def _with_locals(exc: BaseException) -> str:
    return "".join(traceback.TracebackException.from_exception(exc, capture_locals=True).format())


@pytest.mark.parametrize("case", sorted(VALIDATION_FAILURES))
def test_validation_failures_carry_no_secret_in_traceback_locals(case):
    exc = _provoke(case)
    assert exc.code == VALIDATION_FAILURES[case][3]  # positive control: the expected reason
    rendered = _with_locals(exc)
    assert "prepare_secret" in rendered  # positive control: locals were rendered
    for needle in _value_forms(VALIDATION_FAILURES[case][2]):
        assert needle not in rendered, case
    assert exc.__traceback__ is not None
    frames = [f.f_code.co_name for f, _ in traceback.walk_tb(exc.__traceback__)]
    assert "_load" not in frames and "decode_material" not in frames


VALUE_MARKER = "F13-VALUE-MARKER-c0ffee"


class _AuditFormatter(logging.Formatter):
    """A structured audit record: the error's attributes and a locals-bearing trace, as JSON."""

    def format(self, record: logging.LogRecord) -> str:
        exc = record.exc_info[1] if record.exc_info else None
        return json.dumps(
            {
                "event": record.getMessage(),
                "error": {"type": type(exc).__name__, "args": exc.args, "attrs": vars(exc)},
                "trace": _with_locals(exc) if exc is not None else None,
            },
            default=repr,
        )


def render_everywhere(exc: BaseException, caplog, capsys) -> dict[str, str]:
    """Every engine-visible surface a caller is likely to emit an admission failure on."""
    audit_stream = io.StringIO()
    handler = logging.StreamHandler(audit_stream)
    handler.setFormatter(_AuditFormatter())
    audit = logging.getLogger("f13.audit")
    audit.addHandler(handler)
    audit.propagate = False
    sentry = logging.getLogger("f13.sentry")
    try:
        with caplog.at_level(logging.DEBUG, logger="f13.sentry"):
            try:
                raise exc
            except MaskSecretError:
                sentry.exception("admission failed")
                audit.exception("admission failed")
        print(exc, repr(exc))
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
        sys.excepthook(type(exc), exc, exc.__traceback__)
    finally:
        audit.removeHandler(handler)
        audit.propagate = True
    out, err = capsys.readouterr()
    record = audit_stream.getvalue()
    code = getattr(exc, "code", "")
    assert "admission failed" in caplog.text and code in out and code in err  # positive controls
    assert code in record and json.loads(record)["error"]["attrs"]["code"] == code
    return {
        "str": str(exc),
        "repr": repr(exc),
        "format_exception": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        "capture_locals": _with_locals(exc),
        "json": json.dumps({"args": exc.args, "attrs": vars(exc)}, default=repr),
        "log": caplog.text,
        "stdout": out,
        "stderr": err,
        "audit": record,
    }


def assert_absent(surfaces: dict[str, str], needles: list[str]) -> None:
    leaked = sorted({name for name, text in surfaces.items() for n in needles if n in text})
    assert not leaked, f"secret material reached: {', '.join(leaked)}"


class _ValueBearingError(SecretAccessDenied):
    _MESSAGE = f"denied: {VALUE_MARKER}"

    def __init__(self) -> None:
        MaskSecretError.__init__(self, code="secret_access_denied", message=self._MESSAGE)


VALUE_BEARING = {
    "free-code": (lambda: MaskSecretError(code=VALUE_MARKER, message=VALUE_MARKER), None),
    "registered-code": (
        lambda: MaskSecretError(code="secret_access_denied", message=VALUE_MARKER),
        "secret_access_denied",
    ),
    "legacy-code": (lambda: MissingMaskSecret(VALUE_MARKER), "missing_mask_secret"),
    "closed-subclass": (_ValueBearingError, "secret_access_denied"),
    "non-string-code": (lambda: MaskSecretError(code=7, message=VALUE_MARKER), None),  # type: ignore[arg-type]
}


class _ValueBearingProvider(ProtocolProvider):
    def __init__(self, scheme: str, make) -> None:
        super().__init__(scheme)
        self._make = make

    def resolve(self, ref):  # type: ignore[override]
        self.resolve_calls += 1
        raise self._make()


@pytest.mark.parametrize("case", sorted(VALUE_BEARING))
def test_a_value_bearing_provider_error_is_rebuilt_from_its_code(case, caplog, capsys):
    make, code = VALUE_BEARING[case]
    with pytest.raises(MaskSecretError) as info:
        prepare_secret(
            provider=_ValueBearingProvider("vb", make),
            ref=parse_ref("vb:x?encoding=raw"),
            requirements=(REQUIREMENTS[MASK_ROOT_V1],),
        )
    exc = info.value
    assert exc.code == (code or "secret_provider_unavailable")
    assert type(exc).__module__.startswith("decoy_engine.")
    assert_absent(render_everywhere(exc, caplog, capsys), [VALUE_MARKER])


@pytest.mark.parametrize("case", sorted(VALUE_BEARING))
def test_a_value_bearing_error_from_a_custom_preflight_is_rebuilt(case, tmp_path, registry):
    make, code = VALUE_BEARING[case]

    class OwnPreflight(ProtocolProvider):
        def preflight(self, ref, requirements, cache_policy):  # type: ignore[override]
            raise make()

    registry.register(OwnPreflight("ownpf"))
    cfg = keyed_config(tmp_path, mask_secret_ref="ownpf:x")
    with pytest.raises(MaskSecretError) as info:
        run_pipeline(cfg, engine_version=ENGINE_VERSION)
    exc = info.value
    assert exc.code == (code or "secret_provider_unavailable")
    assert VALUE_MARKER not in _with_locals(exc) + str(exc) + repr(exc) + json.dumps(
        vars(exc), default=repr
    )


def test_sdk_exception_text_never_survives_any_surface(caplog, capsys):
    prov = LeakyProvider()
    with pytest.raises(SecretProviderUnavailable) as info:
        prepare_secret(
            provider=prov,
            ref=parse_ref("test:F13-PATH-MARKER?encoding=raw"),
            requirements=(REQUIREMENTS[MASK_ROOT_V1],),
        )
    surfaces = render_everywhere(info.value, caplog, capsys)
    assert_absent(surfaces, ["F13-PATH-MARKER", "F13-TOKEN-MARKER", "SDKBoomError", *_forms()])


@pytest.mark.parametrize("case", sorted(VALIDATION_FAILURES))
def test_validation_failures_are_value_free_on_every_surface(case, caplog, capsys):
    surfaces = render_everywhere(_provoke(case), caplog, capsys)
    assert_absent(surfaces, _value_forms(VALIDATION_FAILURES[case][2]))


# --- r3: value-bearing OBJECTS in admission frames (dataclass repr renders every field) -------

CANARY = "F13-CANARY-dsn://svc:hunter2@vault.internal/secret/path"


@dataclass
class _CanaryProvider:
    """An extension provider that holds a token, DSN or path as a dataclass field."""

    token: str = CANARY
    api_version = API_VERSION
    scheme = "canary"

    def describe(self, ref):
        return SecretDescriptor(scheme=self.scheme, key_version="v1", renewable=False)

    def resolve(self, ref):
        raise SDKBoomError("denied")

    def preflight(self, ref, requirements, cache_policy):
        raise SDKBoomError("denied")


@dataclass
class _CanaryClock:
    token: str = CANARY

    def monotonic(self) -> float:
        raise SDKBoomError("clock")

    def wall(self) -> float:
        raise SDKBoomError("clock")


@dataclass
class _CanaryKeyProvider:
    token: str = CANARY
    key_version: int = 1

    def mask_key(self) -> bytes:
        return b"short"


@dataclass
class _CanaryRegistry(SecretProviderRegistry):
    provider: object = None
    token: str = CANARY

    def __post_init__(self) -> None:
        SecretProviderRegistry.__init__(self, (self.provider,))  # type: ignore[arg-type]


def _raised(call) -> BaseException:
    # The canaries are built inside ``call`` so no frame of this module ever binds one.
    try:
        call()
    except MaskSecretError as exc:
        return exc
    raise AssertionError("expected an admission failure")


_CFG = {"global_settings": {"mask_secret_ref": "canary:x"}, "tables": []}
_KEYED = {
    "global_settings": {},
    "tables": [{"name": "t", "columns": [{"name": "email", "strategy": "hash", "namespace": "e"}]}],
}

CANARY_CASES = {
    "prepare_secret-provider": lambda: prepare_secret(
        provider=_CanaryProvider(),
        ref=parse_ref("canary:x?encoding=raw"),
        requirements=(REQUIREMENTS[MASK_ROOT_V1],),
    ),
    "prepare_secret-clock": lambda: prepare_secret(
        provider=ProtocolProvider(),
        ref=parse_ref("test:x?encoding=raw"),
        requirements=(REQUIREMENTS[MASK_ROOT_V1],),
        cache_policy=SecretCachePolicy(ttl_seconds=5, clock=_CanaryClock()),
    ),
    "admit-registry-and-provider": lambda: admit_mask_key(
        _CFG, registry=_CanaryRegistry(provider=_CanaryProvider())
    ),
    "admit-registry-and-clock": lambda: admit_mask_key(
        _CFG,
        registry=_CanaryRegistry(provider=ProtocolProvider("canary")),
        cache_policy=SecretCachePolicy(ttl_seconds=5, clock=_CanaryClock()),
    ),
    "admit-key-provider": lambda: admit_mask_key(_KEYED, key_provider=_CanaryKeyProvider()),
    "prepare_legacy-key-provider": lambda: prepare_legacy(
        _CanaryKeyProvider(), (REQUIREMENTS[MASK_ROOT_V1],)
    ),
}


@pytest.mark.skipif(
    "MUTANT_UNDER_TEST" in os.environ,
    reason="mutmut wraps every function in a trampoline frame that holds the call arguments",
)
@pytest.mark.parametrize("case", sorted(CANARY_CASES))
def test_value_bearing_objects_never_reach_traceback_locals(case, caplog, capsys):
    exc = _raised(CANARY_CASES[case])
    assert isinstance(exc, MaskSecretError) and exc.__cause__ is None and exc.__context__ is None
    # File names, not function names: mutation tooling renames the functions.
    files = [f.f_code.co_filename for f, _ in traceback.walk_tb(exc.__traceback__)]
    assert any(name.endswith("_preflight.py") for name in files)
    surfaces = render_everywhere(exc, caplog, capsys)
    assert "_preflight.py" in surfaces["capture_locals"]  # positive control: locals were rendered
    assert_absent(surfaces, [CANARY, "hunter2", "vault.internal"])
