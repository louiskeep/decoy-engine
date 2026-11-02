"""Every admission boundary raises with no inherited chain, even inside a caller's handler.

A caller may call admission while handling its own exception (a decode error holding secret
bytes, for example). Python attaches the handled exception as `__context__` of anything raised
in that window, so a rebuilt error would carry it. A-4's AT-X1 (`raise_clean`) closed this for the
`file:` read; the class is every public boundary entry, so each is a cell here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from decoy_engine.keyprovider import MaskSecretError, key_provider_from_ref
from decoy_engine.secret_providers import (
    REQUIREMENTS,
    admit_mask_key,
    parse_ref,
    prepare_legacy,
    prepare_ref,
    prepare_secret,
)
from decoy_engine.secret_providers._local import EnvSecretProvider, FileSecretProvider

CANARY = "CALLER-CONTEXT-CANARY-hunter2"
MASK_ROOT = (REQUIREMENTS["decoy-mask-root-v1"],)
_KEYED = {
    "global_settings": {},
    "tables": [{"name": "t", "columns": [{"name": "e", "strategy": "hash", "namespace": "e"}]}],
}


class _ShortKey:
    """A duck-typed `KeyProvider` whose key is below the 32-byte floor."""

    key_version = "v1"

    def mask_key(self) -> bytes:
        return b"short"


ROUTES: dict[str, Callable[[], Any]] = {
    "prepare_ref": lambda: prepare_ref("env:DECOY_CTX_UNSET_VAR"),
    "key_provider_from_ref": lambda: key_provider_from_ref("env:DECOY_CTX_UNSET_VAR"),
    "admit_mask_key": lambda: admit_mask_key(
        {"global_settings": {"mask_secret_ref": "env:DECOY_CTX_UNSET_VAR"}, "tables": []}
    ),
    "admit_mask_key_no_secret": lambda: admit_mask_key(_KEYED),
    "prepare_legacy": lambda: prepare_legacy(_ShortKey(), MASK_ROOT),
    "prepare_secret": lambda: prepare_secret(
        provider=EnvSecretProvider(),
        ref=parse_ref("env:DECOY_CTX_UNSET_VAR"),
        requirements=MASK_ROOT,
    ),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_an_admission_failure_inside_a_callers_handler_has_no_context(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DECOY_CTX_UNSET_VAR", raising=False)
    carrier = CANARY.encode() + b"\xff"
    try:
        carrier.decode("utf-8")
    except UnicodeDecodeError:
        with pytest.raises(MaskSecretError) as info:
            ROUTES[route]()
    exc = info.value
    assert exc.__cause__ is None and exc.__context__ is None and exc.__suppress_context__
    assert CANARY not in repr(exc) + str(exc)


# The failing-resolution branch: ambient access is forbidden and nothing is installed, so the
# file-access policy cannot be resolved. A route that reads a `file:` secret meets the refusal
# there (key_provider_from_ref at its entry, the others at the provider read inside the
# boundary); the two that read no file still run with the latch set.
_FILE_REF = "file:/tenant-a/private/CALLER-CONTEXT.key"
ROUTES_UNRESOLVED: dict[str, Callable[[], Any]] = {
    "prepare_ref": lambda: prepare_ref(_FILE_REF),
    "key_provider_from_ref": lambda: key_provider_from_ref(_FILE_REF),
    "admit_mask_key": lambda: admit_mask_key(
        {"global_settings": {"mask_secret_ref": _FILE_REF}, "tables": []}
    ),
    "admit_mask_key_no_secret": lambda: admit_mask_key(_KEYED),
    "prepare_legacy": lambda: prepare_legacy(_ShortKey(), MASK_ROOT),
    "prepare_secret": lambda: prepare_secret(
        provider=FileSecretProvider(), ref=parse_ref(_FILE_REF), requirements=MASK_ROOT
    ),
}


@pytest.mark.parametrize("route", sorted(ROUTES_UNRESOLVED))
def test_a_failing_policy_resolution_inside_a_callers_handler_has_no_context(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.fileaccess import FileAccessUnavailable, _entry

    monkeypatch.setattr(_entry, "_ambient_forbidden", True)
    token = _entry._CURRENT.set(None)
    carrier = CANARY.encode() + b"\xff"
    try:
        try:
            carrier.decode("utf-8")
        except UnicodeDecodeError:
            with pytest.raises((MaskSecretError, FileAccessUnavailable)) as info:
                ROUTES_UNRESOLVED[route]()
    finally:
        _entry._CURRENT.reset(token)
    exc = info.value
    if route in ("prepare_ref", "key_provider_from_ref", "admit_mask_key", "prepare_secret"):
        assert type(exc) is FileAccessUnavailable
    assert exc.__cause__ is None and exc.__context__ is None and exc.__suppress_context__
    assert CANARY not in repr(exc) + str(exc)
    assert "tenant-a" not in repr(exc) + str(exc)
