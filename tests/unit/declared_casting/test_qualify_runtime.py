"""``qualify_runtime`` returns fixed tokens only, and refuses a runtime with no owner identity."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.acceptance._f06_support import PROTECTED_QUARANTINE
from tests.acceptance.test_f06_route_parity import _runtime


def _qualify(runtime: Any) -> str | None:
    from decoy_engine.declared_casting._quarantine_protocol import qualify_runtime

    return qualify_runtime(runtime, PROTECTED_QUARANTINE)


def test_empty_owner_is_refused(tmp_path: Any) -> None:
    from dataclasses import replace

    assert _qualify(_runtime(tmp_path)) is None
    assert _qualify(replace(_runtime(tmp_path), owner="")) == "empty_owner"


@pytest.mark.parametrize(
    ("detail", "token"),
    [
        ("weak_key", "weak_key"),
        ("/secret/path: permission denied", "unqualified"),
        ("", "unqualified"),
    ],
)
def test_sink_detail_is_mapped_to_a_fixed_token(tmp_path: Any, detail: str, token: str) -> None:
    from decoy_engine.declared_casting import _quarantine_protocol as P  # noqa: N812

    real = _runtime(tmp_path)
    cap = P.QuarantineCapability(P.PROTECTION_ENCRYPTED, "v", False, detail)
    sink = SimpleNamespace(
        capability=cap,
        **{
            n: getattr(real.sink, n)
            for n in dir(P.ProtectedQuarantineSink)
            if not n.startswith("_") and n != "capability"
        },
    )
    assert _qualify(SimpleNamespace(sink=sink, owner="t")) == token
