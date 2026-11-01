"""A corrupted embedded snapshot in a stored plan is refused with closed-set text.

`plan_from_yaml` reads an untrusted manifest. Its embedded snapshots carry the `source_path` they
were pinned from; a refusal must not echo it (nor the decoder's failure, which would ride as
`__cause__`). Checked under a strict policy, on every place an error carries text: str, repr,
args, notes, cause, context and the formatted traceback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import traceback
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from decoy_engine.plan import compile_plan
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._serialize import plan_from_yaml, plan_to_yaml
from decoy_engine.profile import Profile
from tests.fileaccess.support import RootRelative

CANARY_PATH = "/tenant-a/private/SNAPSHOT-CANARY.json"


def _texts(exc: BaseException) -> list[str]:
    out: list[str] = []
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        link = stack.pop()
        if link is None or id(link) in seen:
            continue
        seen.add(id(link))
        out += [str(link), repr(link), repr(link.args), repr(getattr(link, "__notes__", None))]
        out += [repr(getattr(link, "path", None)), repr(getattr(link, "message", None))]
        stack += [link.__cause__, link.__context__]
    out.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return out


@pytest.fixture
def policy(tmp_path: Path) -> Iterator[RootRelative]:
    root = tmp_path / "root"
    root.mkdir()
    access = RootRelative(root)
    try:
        yield access
    finally:
        access.close()


def _manifest(payload: bytes, sha256: str, *, statistical: bool = False) -> str:
    profile = Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime.now(timezone.utc),
        decoy_engine_version="test",
    )
    cfg = {
        "global_settings": {"seed": 1},
        "tables": [
            {
                "name": "t",
                "row_count": 5,
                "generate_columns": [{"name": "a", "type": "faker", "provider": "name"}],
            }
        ],
    }
    data = yaml.safe_load(plan_to_yaml(compile_plan(cfg, profile, decoy_engine_version="test")))
    data["generation"]["snapshots"] = [
        {
            "source_path": CANARY_PATH,
            "sha256": sha256,
            "payload_b64": base64.b64encode(payload).decode("ascii"),
            "release_id": None,
        }
    ]
    return yaml.safe_dump(data, sort_keys=False)


def _not_json() -> str:
    raw = b"NOT JSON AT ALL"
    return _manifest(raw, hashlib.sha256(raw).hexdigest())


def _digest_mismatch() -> str:
    return _manifest(b'{"a": 1}', "0" * 64)


def _unpinned_reference() -> str:
    data = yaml.safe_load(_manifest(b"{}", hashlib.sha256(b"{}").hexdigest()))
    data["generation"]["snapshots"] = []
    config = json.loads(data["generation"]["config_json"])
    config["tables"][0]["generate_columns"] = [
        {"name": "a", "type": "statistical", "snapshot_file": CANARY_PATH}
    ]
    data["generation"]["config_json"] = json.dumps(config)
    return yaml.safe_dump(data, sort_keys=False)


CASES = {
    "digest_mismatch": (_digest_mismatch, "dp_pinned_snapshot_digest_mismatch"),
    "payload_not_json": (_not_json, "dp_pinned_snapshot_payload_not_json"),
    "unpinned_reference": (_unpinned_reference, "statistical_snapshot_not_pinned"),
}
POSITIONS = {
    "digest_mismatch": "generation.snapshots[0]",
    "payload_not_json": "generation.snapshots[0]",
    "unpinned_reference": "tables.t.generate_columns.a.snapshot_file",
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_corrupted_snapshot_is_refused_without_its_source_path(
    policy: RootRelative, case: str
) -> None:
    build, code = CASES[case]
    with pytest.raises(PlanCompileError) as info:
        plan_from_yaml(build(), file_access=policy)
    exc = info.value
    assert exc.code == code
    assert exc.__cause__ is None
    for text in _texts(exc):
        assert "CANARY" not in text and "tenant-a" not in text, text
        assert "JSONDecodeError" not in text and "Expecting value" not in text, text
    assert exc.path == POSITIONS[case]


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_ambient_refusal_is_closed_set_too(case: str) -> None:
    build, code = CASES[case]
    with pytest.raises(PlanCompileError) as info:
        plan_from_yaml(build())
    assert info.value.code == code
    for text in _texts(info.value):
        assert "CANARY" not in text, text
