"""F13 §5 acceptance 10: a seeded marker never reaches any engine-visible surface.

A unique marker is placed in the secret VALUE, in a provider exception payload, and in the
reference path/field. Success and failure are exercised through the direct engine, and every
captured string/serialized artifact is scanned for the marker and its hex/base64 forms, plus the
derived mask key. Positive controls prove the expected fixed reason and ordinary output were
captured, so an empty capture cannot pass vacuously.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import pickle
import traceback

import pytest

from decoy_engine import run_mask_pipeline_chunked, run_pipeline
from decoy_engine.keyprovider import MaskSecretError, SecretKeyProvider
from decoy_engine.progress import CallbackSink, event_to_dict
from decoy_engine.secret_providers import (
    MASK_ROOT_V1,
    REQUIREMENTS,
    SecretBuffer,
    SecretProviderUnavailable,
    parse_ref,
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
        raise SDKBoomError(f"GET /v1/kv/F13-PATH-MARKER denied; body={MARKER!r}; token=F13-TOKEN-MARKER")


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
