"""The fragment workflow runs Python the PR controls: least privilege, no stored credentials."""

from __future__ import annotations

import yaml
from _fragment_repo import REPO

WORKFLOW = REPO / ".github" / "workflows" / "changelog-fragment.yml"


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_permissions_are_read_only_contents() -> None:
    wf = _load()
    assert wf["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in wf["jobs"].values())


def test_checkout_does_not_persist_credentials() -> None:
    steps = [s for job in _load()["jobs"].values() for s in job["steps"]]
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts
    assert all(s.get("with", {}).get("persist-credentials") is False for s in checkouts)
