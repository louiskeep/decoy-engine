"""CI lock-install sentry (CI-E1): hosted engine jobs install from uv.lock, never re-resolve.

pip resolving today's PyPI tested pyarrow 25 against a lock that pins 24 (two error-text tests
went red). The scanner is tests/sentry/_ci_lock_scan.py; this file holds the acceptance tests
against the real workflows and actions and the seeded cases that prove each rule can go red.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import pytest

from tests.sentry._ci_lock_scan import (
    ACTIONS,
    CONSTRAINTS,
    LOCK_ENV_USES,
    ROOT,
    SETUP_UV_USES,
    VERSION,
    WORKFLOWS,
    WRITE_ALLOWLIST,
    _action_as_workflow,
    _real_workflows,
    _step_text,
    _supported_minors,
    audit_findings,
    is_mac_gate,
    permission_findings,
    scan_actions,
    scan_workflow,
)
from tests.sentry.ci_scan._identity import fetch_raw, identity_findings, should_skip

# --- acceptance tests against the real workflows -----------------------------


def test_engine_jobs_install_from_the_lock() -> None:
    workflows = _real_workflows()
    assert len(workflows) >= 10, "workflow scan looks empty"
    bad = {name: f for name, wf in workflows.items() if (f := scan_workflow(wf, name=name))}
    for action in ("lock-env", "setup-uv"):
        if action_findings := scan_workflow(_action_as_workflow(action), role=action):
            bad[f"actions/{action}"] = action_findings
    assert not bad, "hosted jobs not installed from uv.lock:\n" + "\n".join(
        f"  {name}: {m}" for name, msgs in bad.items() for m in msgs
    )


def _all_steps() -> list[dict[str, Any]]:
    return _action_as_workflow("setup-uv")["jobs"]["setup-uv"]["steps"] + [
        s
        for wf in _real_workflows().values()
        for job in (wf.get("jobs") or {}).values()
        for s in job.get("steps") or []
    ]


def test_setup_uv_is_pinned() -> None:
    uv_steps = [s for s in _all_steps() if str(s.get("uses", "")).startswith("astral-sh/setup-uv")]
    assert len(uv_steps) == 1, (
        f"astral-sh/setup-uv appears {len(uv_steps)} times; want only the setup-uv action"
    )
    version = str((uv_steps[0].get("with") or {}).get("version", ""))
    assert VERSION.fullmatch(version), f"setup-uv version is not an exact x.y.z: {version!r}"
    users = [
        n
        for n, wf in _real_workflows().items()
        for j in wf["jobs"].values()
        for st in j.get("steps") or []
        if str(st.get("uses", "")) == LOCK_ENV_USES
    ]
    assert len(users) >= 10, "expected the lock-env action in the converted jobs"
    assert not any("pip install uv" in _step_text(s) for s in _all_steps())


def _constraint_pins() -> dict[str, str]:
    assert CONSTRAINTS.is_file(), f"{CONSTRAINTS.relative_to(ROOT)} is missing"
    pins = {}
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, version = line.partition("==")
            assert version, f"build constraint is not an exact pin: {line}"
            pins[name] = version
    return pins


def test_build_constraints_pin_the_backends() -> None:
    pins = _constraint_pins()
    assert {"maturin", "hatchling"} <= set(pins)
    for name in ("native-companion.yml", "native-companion-release.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        versions = set(re.findall(r"maturin-version:\s*v?([\d.]+)", text))
        assert versions == {pins["maturin"]}, (name, versions, pins["maturin"])
    companion = _real_workflows()["native-companion.yml"]["jobs"]["core-companion-present"]["steps"]
    assert any(
        "--build-constraint .github/build-constraints.txt" in _step_text(s) for s in companion
    )
    packaging = _real_workflows()["ci.yml"]["jobs"]["packaging-gate"]
    assert str(packaging.get("env", {}).get("PIP_CONSTRAINT", "")).endswith(
        ".github/build-constraints.txt"
    )


def test_pip_audit_reads_the_lock() -> None:
    job = _real_workflows()["security.yml"]["jobs"]["pip-audit"]
    findings = audit_findings(job)
    assert not findings, findings


def docs_findings(wf: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    cond = str(wf["jobs"]["deploy"].get("if", ""))
    for needle in (
        "vars.DOCS_DEPLOY == 'true'",
        "github.ref == 'refs/heads/main'",
        "github.event_name == 'push'",
    ):
        if needle not in cond:
            findings.append(f"deploy.if lacks {needle}")
    if wf.get("permissions") != {"contents": "read"}:
        findings.append(
            f"workflow permissions are {wf.get('permissions')}, want contents: read only"
        )
    for name, job in wf["jobs"].items():
        wide = {
            k
            for k, v in (job.get("permissions") or {}).items()
            if k in {"pages", "id-token"} and v == "write"
        }
        if name != "deploy" and wide:
            findings.append(f"{name} holds {sorted(wide)}")
    deploy_perms = wf["jobs"]["deploy"].get("permissions") or {}
    if deploy_perms.get("pages") != "write" or deploy_perms.get("id-token") != "write":
        findings.append("deploy job lacks pages/id-token write")
    return findings


def test_docs_deploy_is_guarded_and_build_is_least_privilege() -> None:
    findings = docs_findings(_real_workflows()["docs.yml"])
    assert not findings, findings


# --- seeded cases: the scanner itself must catch what it claims to -----------


def _job(*steps: Any, env: dict[str, str] | None = None) -> dict[str, Any]:
    built = [{"run": s} if isinstance(s, str) else s for s in steps]
    return {"jobs": {"j": {"steps": built, **({"env": env} if env else {})}}}


LE = {"uses": LOCK_ENV_USES, "with": {"extras": "dev"}}
WHEEL = "wheel=\"$(find d -name '*.whl' | head -n1)\"\n"
SHA = "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86"


def test_seeded_findings() -> None:
    cases = {
        'pip install -e ".[dev]"': ["project install"],
        "uv sync --extra dev": ["uv sync without --frozen", "installs outside"],
        "uv sync --frozen --extra dev": ["installs outside"],
        # An option before the subcommand stops `_uv_positions` at `pip`, so only the
        # `pip sync` entry in SYNC_SUBCOMMANDS finds these.
        "uv pip --system sync requirements.txt": ["uv sync without --frozen", "installs outside"],
        "uv pip -q sync requirements.txt": ["uv sync without --frozen", "installs outside"],
        "uv pip --python p sync requirements.txt": ["uv sync without --frozen", "installs outside"],
        "uv --directory run sync --frozen": ["installs outside"],
        "bash scripts/dev/bootstrap.sh": ["installs outside"],
        "pip install .[dev]": ["project install"],
        "python -m pip install -e .[docs]": ["project install"],
        "uv pip install -e .": ["project install"],
        "pip install pip-audit": ["not an exact == pin"],
        "pip install uv": ["not an exact == pin"],
        "awk '{print}' uv.lock": ["inline lock parsing"],
        "python -c 'open(\"uv.lock\")' uv.lock": ["inline lock parsing"],
    }
    for text, expected in cases.items():
        found = scan_workflow(_job(text))
        assert found and all(any(e in f for f in found) for e in expected), (text, found)
    assert any("project install" in f for f in scan_workflow(_job("echo hi\npip install -e .")))
    assert any(
        "project install" in f for f in scan_workflow(_job("set -e\n  pip install \\\n    -e ."))
    )
    assert any("consumer before" in f for f in scan_workflow(_job("pytest tests")))
    assert any("consumer before" in f for f in scan_workflow(_job("pytest tests", LE)))
    assert any("uv run without" in f for f in scan_workflow(_job(LE, "uv run pytest")))
    assert any("without the setup-uv" in f for f in scan_workflow(_job("uv pip install x==1")))
    codspeed = {"uses": "CodSpeedHQ/action@v4", "with": {"run": "pytest tests/codspeed/"}}
    assert any("consumer before" in f for f in scan_workflow(_job(codspeed)))
    raw = {"uses": "astral-sh/setup-uv@v5", "with": {"version": "0.11.19"}}
    assert any("raw setup-uv" in f for f in scan_workflow(_job(raw)))
    assert not any("raw setup-uv" in f for f in scan_workflow(_job(raw), role="setup-uv"))
    assert any(
        "setup-uv version" in f
        for f in scan_workflow(_job({"uses": "astral-sh/setup-uv@v5"}), role="setup-uv")
    )
    smoke = "pip install \"$wheel\" 'pyarrow>=14'\npython parity_smoke.py"
    found = scan_workflow(_job(smoke))
    assert any("lock-read pin" in f for f in found) and any("exact == pin" in f for f in found)


def test_seeded_clean_jobs() -> None:
    clean = [
        (LE, "pytest tests"),
        ("pip install ruff==0.15.22", "ruff check src"),
        (LE, "uv pip install --python x build==1.2.2"),
        (LE, "uv run --frozen pytest"),
        # `sync` is the value of --directory here; only `run` is the subcommand.
        (LE, "uv --directory sync run --frozen pytest"),
        ("pip install ./decoy-engine-native",),
        ({"uses": SETUP_UV_USES}, "uvx --python 3.12 pip-audit==2.10.1 -r x"),
    ]
    for steps in clean:
        assert scan_workflow(_job(*steps)) == [], steps
    assert scan_workflow(_job(LE, "uv run pytest", env={"UV_FROZEN": "1"})) == []
    pinned = (
        WHEEL + "PYARROW=$(bash scripts/dev/lock_version.sh pyarrow)\n"
        'pip install "$wheel" "pyarrow==$PYARROW"\npython parity_smoke.py'
    )
    assert scan_workflow(_job(pinned)) == []
    unread = WHEEL + 'pip install "$wheel" "pyarrow==$PYARROW"\npython parity_smoke.py'
    assert any("not read by lock_version.sh" in f for f in scan_workflow(_job(unread)))
    # A fake resolver (inline parsing of the lock) must fail even when the pin looks right.
    fake = (
        WHEEL + "PYARROW=$(grep -A1 'name = \"pyarrow\"' uv.lock | tail -1)\n"
        'pip install "$wheel" "pyarrow==$PYARROW"\npython parity_smoke.py'
    )
    found = scan_workflow(_job(fake))
    assert any("inline lock parsing" in f for f in found)
    assert any("not read by lock_version.sh" in f for f in found)


def test_seeded_action_tool_and_index_cases() -> None:
    copy = 'echo "UV_PROJECT_ENVIRONMENT=$RUNNER_TEMP/venv" >> "$GITHUB_ENV"'
    assert any("copies the venv env block" in f for f in scan_workflow(_job(copy)))
    assert scan_workflow(_job(copy), role="lock-env") == []
    findings = {
        "uv lock": "uv lock without --check",
        "uv lock --upgrade-package x": "uv lock without --check",
        "pip install --index-url https://x/simple ruff==1.0": "index override",
        "uv pip install --extra-index-url https://x/simple ruff==1.0": "index override",
        "pip install -r requirements.txt": "unlocked requirements",
        "uvx pip-audit": "tool is not an exact",
        "uv tool install ruff": "tool is not an exact",
        "uv tool --python 3.12 run black": "tool is not an exact",
        "uv tool --cache-dir c install black": "tool is not an exact",
        "uv tool --python= run black": "tool is not an exact",
        "uv tool dir install": "names no package",
        "pip --python= install requests": "not an exact",
        "uv pip --python= install requests": "not an exact",
        "uv tool --a x y run black": "tool is not an exact",
        "uv tool --a b c install black": "tool is not an exact",
        "uv tool frobnicate black": "tool is not an exact",
        "uv tool --brand-new v install black": "tool is not an exact",
        "uv tool --python 3.12 run ruff==0.15.22": "uv run without --frozen",
        "uv --brand-new-option x sync": "uv sync without --frozen",
        "uv --brand-new-option x add requests": "uv add",
        "uv --brand-new-option run sync": "uv sync without --frozen",
        "uv --cache-dir c --directory d --config-file f sync": "uv sync without --frozen",
        "pipx install ruff": "tool is not an exact",
        "pipx run ruff": "tool is not an exact",
    }
    for text, expected in findings.items():
        found = scan_workflow(_job(text))
        assert any(expected in f for f in found), (text, found)
    for text in (
        "uv lock --check",
        "uvx --python 3.12 pip-audit==2.10.1 -r x",
        "pipx run ruff==1.0",
    ):
        assert scan_workflow(_job({"uses": SETUP_UV_USES}, text)) == [], text
    for version, clean in (("latest", 0), ("0.11", 0), ("", 0), ("0.11.19", 1)):
        step = {"uses": f"astral-sh/setup-uv@{SHA}", "with": {"version": version}}
        found = scan_workflow(_job(step), role="setup-uv")
        assert (found == []) == bool(clean), (version, found)


# --- round 3 seeds: every one is red on the round-2 scanner ------------------

EXPORT = "uv export --frozen --all-extras -o x.txt"
AUDIT = 'uvx --python "$PY" pip-audit==2.10.1 --strict --disable-pip --require-hashes -r x.txt'


def _audit_job(
    run: str = AUDIT,
    *,
    legs: list[str] | None = None,
    fail_fast: Any = False,
    step: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
    before: tuple[str, ...] = (),
) -> dict[str, Any]:
    leg_list = sorted(_supported_minors()) if legs is None else legs
    audit = {"run": run, "env": {"PY": "${{ matrix.python }}"}, **(step or {})}
    strategy: dict[str, Any] = {"matrix": {"python": leg_list}}
    if fail_fast is not None:
        strategy["fail-fast"] = fail_fast
    return {
        "strategy": strategy,
        "steps": [{"run": EXPORT}, *({"run": b} for b in before), audit],
        **(job or {}),
    }


def test_audit_matrix_job_is_clean() -> None:
    assert audit_findings(_audit_job()) == []


def test_audit_cannot_be_hidden() -> None:
    hidden = {
        "|| true": AUDIT + " || true",
        "multiline || true": AUDIT + " \\\n  || true",
        "trailing || then true": AUDIT + " ||\n  true",
        "semicolon": AUDIT + "; true",
        "&& wrapper": AUDIT + " && echo ok",
        "pipe": AUDIT + " | tee out.txt",
        "if wrapper": f"if {AUDIT}; then echo ok; fi",
        "negation": "! " + AUDIT,
        "exit 0": AUDIT + "\nexit 0",
        "exit 0 with ||": AUDIT + " || exit 0",
        "set +e": "set +e\n" + AUDIT,
        "rc plumbing": f"rc=0\nfor py in 3.10 3.11 3.12; do\n{AUDIT} || rc=1\ndone\nexit $rc",
        "ignore": AUDIT + " --ignore-vuln PYSEC-1",
        "unpinned": AUDIT.replace("==2.10.1", ""),
        "not strict": AUDIT.replace(" --strict", ""),
        "hardcoded interpreter": AUDIT.replace('"$PY"', "3.12"),
    }
    for name, run in hidden.items():
        assert audit_findings(_audit_job(run)), name
    elsewhere = {
        "exit 0 in an earlier step": _audit_job(before=("exit 0",)),
        "set +e in an earlier step": _audit_job(before=("set +e",)),
        "step continue-on-error": _audit_job(step={"continue-on-error": True}),
        "step continue-on-error expression": _audit_job(
            step={"continue-on-error": "${{ github.event_name == 'push' }}"}
        ),
        "job continue-on-error": _audit_job(job={"continue-on-error": True}),
        "step if": _audit_job(step={"if": "false"}),
        "job if": _audit_job(job={"if": "false"}),
        "fail-fast true": _audit_job(fail_fast=True),
        "fail-fast absent": _audit_job(fail_fast=None),
        "leg missing": _audit_job(legs=["3.11", "3.12"]),
        "matrix exclude": _audit_job(
            job={
                "strategy": {
                    "fail-fast": False,
                    "matrix": {
                        "python": sorted(_supported_minors()),
                        "exclude": [{"python": "3.10"}],
                    },
                }
            }
        ),
    }
    for name, job in elsewhere.items():
        assert audit_findings(job), name


def _wf(
    *steps: Any,
    name_job: str = "j",
    env: dict[str, str] | None = None,
    runs_on: Any = None,
) -> dict[str, Any]:
    built = [{"run": s} if isinstance(s, str) else s for s in steps]
    job: dict[str, Any] = {"steps": built}
    if env:
        job["env"] = env
    if runs_on:
        job["runs-on"] = runs_on
    return {"jobs": {name_job: job}}


def test_inline_lock_parsing_forms() -> None:
    forms = [
        'python -c \'import tomllib; tomllib.load(open("uv.lock", "rb"))\'',
        "python - <uv.lock",
        "jq . <uv.lock",
        "cat uv.lock",
        "sed -n 1p ./uv.lock",
        'grep -c pyarrow "$GITHUB_WORKSPACE/uv.lock"',
        "LOCK=uv.lock\ngrep pyarrow $LOCK",
        "python -c 'open(\"uv.lock\")'",
        "grep pyarrow uv.lock | head -1",
    ]
    for text in forms:
        found = scan_workflow(_wf(text))
        assert any("inline lock parsing" in f for f in found), (text, found)
    for text in (
        "bash scripts/dev/lock_version.sh pyarrow",
        "uv lock --check",
        "# uv.lock note\nls",
    ):
        assert not any("inline lock parsing" in f for f in scan_workflow(_wf(text))), text


def test_option_and_command_normalisation() -> None:
    cases = {
        "pip install --index-url=https://x/simple ruff==1.0": "index override",
        "pip install --extra-index-url=https://x/simple ruff==1.0": "index override",
        "pip install --index=https://x/simple ruff==1.0": "index override",
        "uv pip install --default-index=https://x/simple ruff==1.0": "index override",
        "pip install -ihttps://x/simple ruff==1.0": "index override",
        "pip install -rreqs.txt": "unlocked requirements",
        "pip install --requirement=reqs.txt": "unlocked requirements",
        "pip install -e. ": "project install",
        "pip install -e.[dev]": "project install",
        "pip install --editable=.": "project install",
        "pip3.11 install -e .": "project install",
        "python3 -m pip3 install .[dev]": "project install",
        "pip install $SRC": "project install",
        "pip install /abs/path/pkg": "project install",
        "pip install ../other": "project install",
        "pip install ./not-native": "project install",
        "pip install --quiet install-ok": "not an exact",
        "uv run --with requests pytest": "uv run --with",
        "uv run --with=requests pytest": "uv run --with",
        "uv run --with-requirements r.txt pytest": "uv run --with",
        "uv add requests": "uv add",
        "uv remove requests": "uv remove",
        "/usr/local/bin/uv sync --extra dev": "uv sync without --frozen",
        "$HOME/.local/bin/uv sync --frozen": "installs outside",
        "/usr/bin/pip install -e .": "project install",
        "uv sync --frozen --python=3.12": "installs outside",
        # Shared-core spellings: global options before the subcommand, find-links, index env.
        "uv --quiet sync --frozen": "installs outside",
        "uv pip sync r.txt": "uv sync without --frozen",
        "uv --quiet pip sync r.txt": "uv sync without --frozen",
        "uv --preview-features x sync": "uv sync without --frozen",
        "uv pip sync --frozen r.txt": "installs outside",
        "uv tool run ruff==0.15.22": "uv run without --frozen",
        "uv tool install ruff": "tool is not an exact",
        "uv tool upgrade ruff": "tool is not an exact",
        "pipx inject app requests": "tool is not an exact",
        "pipx upgrade app": "tool is not an exact",
        "pipx upgrade-all": "names no package",
        "pipx reinstall app": "tool is not an exact",
        "pipx runpip app install requests": "tool is not an exact",
        "uv --directory x sync": "uv sync without --frozen",
        "uv --quiet add requests": "uv add",
        "uv --quiet run pytest": "uv run without --frozen",
        "pip --python .venv/bin/python install requests": "not an exact",
        "uv pip --python p install requests": "not an exact",
        "pip install -f https://x ruff==1.0": "index override",
        "pip install -e dist/x.whl": "project install",
        "uv pip install --no-deps -edist/x.whl": "project install",
        "pip install --find-links=https://x ruff==1.0": "index override",
        "PIP_INDEX_URL=https://x pip install ruff==1.0": "index environment",
        "export UV_EXTRA_INDEX_URL=https://x": "index environment",
        'bash -c "pip install -e ."': "project install",
    }
    for text, expected in cases.items():
        found = scan_workflow(_wf(LE, text))
        assert any(expected in f for f in found), (text, found)
    clean = [
        "pip install ./decoy-engine-native",
        "pip install ./decoy-engine-native[dev]==1",
        "pip install dist/x.whl",
        "pip install /tmp/pkg/x.whl",
        'wheel="$(find d -name \'*.whl\' | head -n1)"\npip install "$wheel" ruff==1.0',
        "uv pip install --python=x build==1.2.2",
    ]
    for text in clean:
        assert not [f for f in scan_workflow(_wf(LE, text)) if "outside" not in f], text


def test_venv_env_copy_forms() -> None:
    heredoc = 'cat >> "$GITHUB_ENV" <<EOF\nUV_PROJECT_ENVIRONMENT=$RUNNER_TEMP/venv\nEOF'
    for text in (heredoc, 'echo "DECOY_VENV=x" >> "$GITHUB_ENV"'):
        assert any("copies the venv env block" in f for f in scan_workflow(_wf(text))), text
    assert any(
        "copies the venv env block" in f for f in scan_workflow(_wf("ls", env={"DECOY_VENV": "x"}))
    )
    step_env = {"run": "ls", "env": {"UV_PROJECT_ENVIRONMENT": "x"}}
    assert any("copies the venv env block" in f for f in scan_workflow(_wf(step_env)))
    wf = _wf("ls")
    wf["env"] = {"UV_PROJECT_ENVIRONMENT": "x"}
    assert any("copies the venv env block" in f for f in scan_workflow(wf))


def test_setup_uv_needs_a_commit_sha() -> None:
    sha = "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86"
    for ref in ("v5", "main", "d4b2f3b"):
        step = {"uses": f"astral-sh/setup-uv@{ref}", "with": {"version": "0.11.19"}}
        found = scan_workflow(_wf(step), role="setup-uv")
        assert any("commit SHA" in f for f in found), (ref, found)
    ok = {"uses": f"astral-sh/setup-uv@{sha}", "with": {"version": "0.11.19"}}
    assert scan_workflow(_wf(ok), role="setup-uv") == []
    # A trailing newline must not pass either validator (catalog class 1).
    newline_sha = {"uses": f"astral-sh/setup-uv@{sha}\n", "with": {"version": "0.11.19"}}
    assert any("commit SHA" in f for f in scan_workflow(_wf(newline_sha), role="setup-uv"))
    newline_ver = {"uses": f"astral-sh/setup-uv@{sha}", "with": {"version": "0.11.19\n"}}
    assert any("exact x.y.z" in f for f in scan_workflow(_wf(newline_ver), role="setup-uv"))


def test_permissions_sentry() -> None:
    good = {"permissions": {"contents": "read"}, "jobs": {"a": {"steps": []}}}
    assert permission_findings("x.yml", good) == []
    assert permission_findings("x.yml", {"jobs": {"a": {}}}), "missing top-level block"
    assert permission_findings("x.yml", {**good, "permissions": "write-all"})
    assert permission_findings("x.yml", {**good, "permissions": {"contents": "write"}})
    wide_job = {**good, "jobs": {"a": {"permissions": {"pages": "write"}}}}
    assert permission_findings("x.yml", wide_job)
    assert permission_findings("x.yml", {**good, "jobs": {"a": {"permissions": "write-all"}}})
    allowed = {("x.yml", "a"): frozenset({"pages"})}
    assert permission_findings("x.yml", wide_job, allowlist=allowed) == []


def test_every_real_workflow_declares_permissions() -> None:
    bad = {n: f for n, wf in _real_workflows().items() if (f := permission_findings(n, wf))}
    assert not bad, bad
    assert len(WRITE_ALLOWLIST) == 3, "write-scope allowlist is a ratchet at 3 entries"
    used = {
        (n, j)
        for n, wf in _real_workflows().items()
        for j, job in {**(wf.get("jobs") or {}), "": wf}.items()
        if "write" in str((job or {}).get("permissions", ""))
    }
    assert used == {(n, j) for n, j in WRITE_ALLOWLIST}, "allowlist entries must all be in use"


def test_mac_exemption_is_exactly_one_job() -> None:
    exempt = [
        (n, j)
        for n, wf in _real_workflows().items()
        for j, job in wf["jobs"].items()
        if is_mac_gate(n, j, job)
    ]
    assert exempt == [("gate-selfhosted.yml", "gate")], exempt
    mac = ["self-hosted", "decoy-gate"]
    boot = "bash scripts/dev/bootstrap.sh"
    on_gate = _wf(boot, name_job="gate", runs_on=mac)
    assert not any("outside" in f for f in scan_workflow(on_gate, name="gate-selfhosted.yml"))
    other_labels = _wf(boot, name_job="gate", runs_on=["self-hosted", "macos", "decoy-gate"])
    assert any("outside" in f for f in scan_workflow(other_labels, name="gate-selfhosted.yml"))
    other_job = _wf(boot, name_job="gate2", runs_on=mac)
    assert any("outside" in f for f in scan_workflow(other_job, name="gate-selfhosted.yml"))
    other_file = _wf(boot, name_job="gate", runs_on=mac)
    assert any("outside" in f for f in scan_workflow(other_file, name="ci.yml"))


def test_every_composite_action_is_scanned(tmp_path: Path) -> None:
    shipped = {p.parent.name for p in ACTIONS.glob("**/action.y*ml")}
    assert {"lock-env", "setup-uv"} <= shipped
    assert scan_actions(ACTIONS) == {}
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "action.yaml").write_text(
        "name: s\nruns:\n  using: composite\n  steps:\n    - shell: bash\n      run: pip install -e .\n",
        encoding="utf-8",
    )
    found = scan_actions(tmp_path)
    assert list(found) == ["stray"] and any("project install" in f for f in found["stray"]), found


def _shell(*args: str, cwd: Path, env: dict[str, str] | None = None) -> Any:
    import subprocess

    return subprocess.run(  # noqa: S603
        ["/bin/bash", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def test_lock_profile_lives_in_lock_version_only(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "dev" / "lock_version.sh"
    py = _shell(str(script), "--python", cwd=ROOT).stdout.strip()
    assert re.fullmatch(r"3\.\d+\.\d+", py), py
    others = [
        ROOT / ".github" / "actions" / "lock-env" / "action.yml",
        ROOT / "scripts" / "dev" / "bootstrap.sh",
        ROOT / "scripts" / "release" / "release_checks.sh",
    ]
    for path in others:
        text = path.read_text(encoding="utf-8")
        assert py not in text, f"{path.name} restates the lock-profile Python"
        assert not re.search(r"pyarrow\s+polars|duckdb\s+pyarrow|KEY_DEPS", text), path.name
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for m in re.finditer(
            r"uses:\s*" + re.escape(LOCK_ENV_USES) + r"\s*\n\s*with:\n((?:\s+.*\n)+)",
            path.read_text(),
        ):
            assert f'python: "{py}"' not in m.group(1), f"{path.name} restates the profile Python"
    lock_env = others[0].read_text(encoding="utf-8")
    assert lock_env.count("lock_version.sh --check") == 1, "key-dep check must run once"
    assert "importlib.metadata" not in lock_env
    assert others[1].read_text(encoding="utf-8").count("lock_version.sh") >= 1


def test_lock_version_check_mode(tmp_path: Path) -> None:
    import shutil
    import sys
    from importlib.metadata import version

    dev = tmp_path / "scripts" / "dev"
    dev.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "dev" / "lock_version.sh", dev)
    pytest_v = version("pytest")
    (tmp_path / "uv.lock").write_text(
        f'name = "pytest"\nversion = "{pytest_v}"\n\nname = "nopkg-zzz"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    python = venv / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "DECOY_VENV": str(tmp_path / "venv")}
    ok = _shell(str(dev / "lock_version.sh"), "--check", "pytest", cwd=tmp_path, env=env)
    assert ok.returncode == 0 and ok.stdout.count("pytest") == 1 and "OK" in ok.stdout, ok
    bad = _shell(
        str(dev / "lock_version.sh"), "--check", "pytest", "nopkg-zzz", cwd=tmp_path, env=env
    )
    assert bad.returncode == 1 and "MISMATCH" in bad.stdout, bad
    assert bad.stdout.count("pytest") == 1, "each package prints once"


def test_index_environment_variables_are_findings_in_every_env_block() -> None:
    for name in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL"):
        step = {"env": {name: "https://x"}, "run": "uv pip install ruff==1.0"}
        assert any("index environment" in f for f in scan_workflow(_wf(LE, step))), name
        job = {"jobs": {"j": {"env": {name: "x"}, "steps": [LE]}}}
        assert any("index environment" in f for f in scan_workflow(job)), name
        wf = {"env": {name: "x"}, "jobs": {"j": {"steps": [LE]}}}
        assert any("index environment" in f for f in scan_workflow(wf)), name


# --- shared sentry package tests/sentry/ci_scan: identity with the CLI repository -----

# Ledger of the files in tests/sentry/ci_scan, shared byte-for-byte with the CLI repository.
# `sha256` is this repository's copy; `other_commit` is a commit in `other_repo` holding the same
# bytes at `other_path`, fetched and compared below, so editing a local file and its hash together
# still fails. Changing a shared file means landing it in the other repository first, then
# pointing `other_commit` at that commit. After both repositories merge to main, the orchestrator
# re-points every `other_commit` here, and the CLI ledger, at the merge commits on main.
SHARED_LEDGER = [
    {
        "path": "tests/sentry/ci_scan/__init__.py",
        "sha256": "957ed128539114a8d15c4e03ac77f486f9abee53294fb6b1aadbba536f76ca21",
        "other_repo": "louiskeep/decoy",
        "other_path": "tests/sentry/ci_scan/__init__.py",
        "other_commit": "d60d1fa1e183f3ed4787d96b24fbe944dd666707",
    },
    {
        "path": "tests/sentry/ci_scan/_shell_tokens.py",
        "sha256": "38e9fddea1c22fdc56017b492193f38649fa448aa04461557645a5921e701b78",
        "other_repo": "louiskeep/decoy",
        "other_path": "tests/sentry/ci_scan/_shell_tokens.py",
        "other_commit": "d60d1fa1e183f3ed4787d96b24fbe944dd666707",
    },
    {
        "path": "tests/sentry/ci_scan/_identity.py",
        "sha256": "82e09cfd65539afb6289750017a79dbcb5663116010309e821bc699e2f48b096",
        "other_repo": "louiskeep/decoy",
        "other_path": "tests/sentry/ci_scan/_identity.py",
        "other_commit": "d60d1fa1e183f3ed4787d96b24fbe944dd666707",
    },
    {
        "path": "tests/sentry/ci_scan/test_identity.py",
        "sha256": "ea864e19cd4ea0804f792520e23dec5fd181ebaae1bee8681e5cb80be97fea30",
        "other_repo": "louiskeep/decoy",
        "other_path": "tests/sentry/ci_scan/test_identity.py",
        "other_commit": "d60d1fa1e183f3ed4787d96b24fbe944dd666707",
    },
]


def test_every_shared_file_is_in_the_ledger_and_matches_it() -> None:
    on_disk = {
        f"tests/sentry/ci_scan/{p.name}" for p in (ROOT / "tests/sentry/ci_scan").glob("*.py")
    }
    assert on_disk == {e["path"] for e in SHARED_LEDGER}
    for entry in SHARED_LEDGER:
        digest = hashlib.sha256((ROOT / entry["path"]).read_bytes()).hexdigest()
        assert digest == entry["sha256"], entry["path"]


def test_shared_files_are_byte_identical_to_the_other_repository() -> None:
    for entry in SHARED_LEDGER:
        assert re.fullmatch(r"[0-9a-f]{40}", entry["other_commit"]), "a real commit, not a label"
        fetched = fetch_raw(entry["other_repo"], entry["other_commit"], entry["other_path"])
        if should_skip(fetched, os.environ):
            pytest.skip("offline: the cross-repository check runs in CI and on networked builders")
        local = (ROOT / entry["path"]).read_bytes()
        assert identity_findings(local, entry["sha256"], fetched) == [], entry["path"]


def test_the_shared_core_is_repo_neutral() -> None:
    text = (ROOT / "tests/sentry/ci_scan/_shell_tokens.py").read_text(encoding="utf-8")
    for word in ("requirements-", "uv.lock", "tomllib", "_ci_lock_scan", "pyproject"):
        assert word not in text, f"the shared core must not know {word}"


def test_commands_that_only_look_like_package_commands_stay_clean() -> None:
    clean = [
        "uv tool list",
        "uv tool dir",
        "uv tool --python 3.12 list",
        "uv tool --cache-dir c dir",
        "uv tool --python= list",
        "uv tool uninstall black",
        "uv tool update-shell",
        "uv run --frozen sync",
        "uv tool uninstall black",
        "pipx list",
        "pipx uninstall app",
        "pipx runpip app list",
        "rm -f build.log",
        "docker build -f Dockerfile .",
        "tail -f server.log",
        "grep -f patterns.txt data.txt",
    ]
    for text in clean:
        assert scan_workflow(_wf(LE, text)) == [], text
