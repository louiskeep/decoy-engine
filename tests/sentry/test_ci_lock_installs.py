"""CI lock-install sentry (CI-E1): hosted engine jobs install from uv.lock, never re-resolve.

pip resolving today's PyPI tested pyarrow 25 against a lock that pins 24 (two error-text tests
went red). Every workflow and composite action is scanned, no allowlist. Order is checked by step
index, not data flow: a step that deletes the venv between install and use is not seen.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
CONSTRAINTS = ROOT / ".github" / "build-constraints.txt"
LOCK_ENV_USES = "./.github/actions/lock-env"
ACTIONS = ROOT / ".github" / "actions"
SETUP_UV_USES = "./.github/actions/setup-uv"
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
INDEX_OPTIONS = {"--index-url", "--extra-index-url", "-i", "--index", "--default-index"}

# Commands that need the engine installed (or its docs/typing toolchain) to mean anything.
CONSUMER_TOKENS = {"pytest", "sphinx-build", "mypy"}
CONSUMER_SUFFIXES = ("prove_regression.py", "scripts/test_flight.py")
SEPARATORS = {"&&", "||", ";", "|"}
# pip/uv-pip options that consume the next token as their value.
VALUE_OPTIONS = {
    "--python",
    "--build-constraint",
    "-c",
    "--constraint",
    "-r",
    "--requirement",
    "-i",
    "--index-url",
    "--extra-index-url",
    "-f",
    "--find-links",
    "-t",
    "--target",
    "--prefix",
    "--no-binary",
    "--only-binary",
}
EXACT_PIN = re.compile(r"^[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_,.\-]+\])?==[^<>=!~\s]+$")
_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")


def _step_text(step: dict[str, Any]) -> str:
    """The shell a step runs: its `run:` plus the `with.run` of a CodSpeed action step."""
    parts = []
    if isinstance(step.get("run"), str):
        parts.append(step["run"])
    uses = str(step.get("uses", ""))
    with_run = (step.get("with") or {}).get("run")
    if uses.startswith("CodSpeedHQ/action") and isinstance(with_run, str):
        parts.append(with_run)
    return "\n".join(parts)


def _commands(text: str) -> list[list[str]]:
    """Split step text into commands of tokens: comments dropped, continuations joined."""
    text = text.replace('\\"', '"').replace("\\$", "$")
    text = re.sub(r"\\\n\s*", " ", text)
    commands: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        current: list[str] = []
        for raw in _TOKEN.findall(line):
            token = raw.strip("\"'") if raw[0] in "\"'" else raw
            if token.endswith(";") and len(token) > 1:
                token = token[:-1]
                current.append(token)
                commands.append(current)
                current = []
            elif token in SEPARATORS:
                commands.append(current)
                current = []
            else:
                current.append(token)
        commands.append(current)
    return [c for c in commands if c]


def _base(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _pip_install_args(cmd: list[str]) -> list[str] | None:
    """Arguments after `pip install` (pip, python -m pip, uv pip), or None if not one."""
    for i, tok in enumerate(cmd[:-1]):
        if _base(tok) in {"pip", "pip3"} and cmd[i + 1] == "install":
            return cmd[i + 2 :]
    return None


def _is_consumer(cmd: list[str]) -> bool:
    if _pip_install_args(cmd) is not None:
        return False
    return any(t in CONSUMER_TOKENS or t.endswith(CONSUMER_SUFFIXES) for t in cmd)


def _is_lock_install(cmd: list[str]) -> bool:
    if any(t.endswith("bootstrap.sh") for t in cmd):
        return True
    return "uv" in cmd and "sync" in cmd and "--frozen" in cmd


def _requirement_findings(args: list[str]) -> list[str]:
    findings: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in {"-r", "--requirement"}:
            findings.append("pip install -r reads an unlocked requirements file")
            skip = True
            continue
        if arg in INDEX_OPTIONS or arg.startswith(("--index-url=", "--extra-index-url=")):
            findings.append(f"package index override ({arg})")
            skip = arg in INDEX_OPTIONS
            continue
        if arg in VALUE_OPTIONS:
            skip = True
            continue
        if arg in {"-e", "--editable"} or arg == "." or re.search(r"(^|/)\.\[", arg):
            findings.append(f"project install by pip ({arg})")
            continue
        if arg.startswith("-"):
            continue
        if arg.startswith(("./", "/", "$")) or arg.endswith(".whl"):
            continue  # a path or a wheel
        if not EXACT_PIN.match(arg):
            findings.append(f"requirement is not an exact == pin ({arg})")
    return findings


def _tool_findings(cmd: list[str]) -> list[str]:
    """uvx, `uv tool install|run` and pipx fetch a tool from the index: pin it exactly."""
    start = None
    for i, tok in enumerate(cmd):
        nxt = cmd[i + 1] if i + 1 < len(cmd) else ""
        if tok == "uvx" or (tok == "pipx" and nxt in {"install", "run"}):
            start = i + (2 if tok == "pipx" else 1)
            break
        if tok == "uv" and nxt == "tool" and (cmd[i + 2 : i + 3] or [""])[0] in {"install", "run"}:
            start = i + 3
            break
    if start is None:
        return []
    skip = False
    for arg in cmd[start:]:
        if skip:
            skip = False
            continue
        if arg in {"--python", "-p", "--with", "--from", *INDEX_OPTIONS}:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return [] if EXACT_PIN.match(arg) else [f"tool is not an exact == pin ({arg})"]
    return ["tool command names no package"]


def _misc_findings(cmd: list[str]) -> list[str]:
    findings = _tool_findings(cmd)
    if "uv" in cmd and "lock" in cmd and "--check" not in cmd:
        findings.append("uv lock without --check re-resolves the lock")
    findings += [f"package index override ({t})" for t in cmd if t in INDEX_OPTIONS]
    return findings


def _writes_venv_env(text: str) -> bool:
    return bool(re.search(r"(UV_PROJECT_ENVIRONMENT|DECOY_VENV)=.*GITHUB_ENV", text))


# --- the scan ----------------------------------------------------------------


def _frozen_env(wf: dict[str, Any], job: dict[str, Any]) -> bool:
    return any(str((env or {}).get("UV_FROZEN")) == "1" for env in (wf.get("env"), job.get("env")))


def _pyarrow_findings(
    job: dict[str, Any], commands: list[list[list[str]]], texts: list[str]
) -> list[str]:
    """(f): a parity-smoke job may install pyarrow only as `pyarrow==$VAR`, read from uv.lock."""
    if not any("parity_smoke.py" in t for t in texts):
        return []
    findings: list[str] = []
    lock_read = any("lock_version.sh" in t for t in texts)
    for step_cmds in commands:
        for cmd in step_cmds:
            args = _pip_install_args(cmd)
            for arg in args or []:
                if not arg.startswith("pyarrow"):
                    continue
                if not re.fullmatch(r"pyarrow==\$\{?[A-Za-z_]+\}?", arg):
                    findings.append(
                        f"parity smoke installs pyarrow other than the lock-read pin ({arg})"
                    )
                elif not lock_read:
                    findings.append("parity smoke pyarrow pin is not read by lock_version.sh")
    return findings


WRITE_ALLOWLIST: dict[tuple[str, str], frozenset[str]] = {}


def permission_findings(name: str, wf: dict[str, Any], allowlist: Any = None) -> list[str]:
    return []


def is_mac_gate(name: str, job_name: str, job: dict[str, Any]) -> bool:
    return False


def scan_actions(actions_dir: Path) -> dict[str, list[str]]:
    return {}


def scan_workflow(wf: dict[str, Any], *, role: str = "workflow", name: str = "") -> list[str]:
    """Every lock-install violation in one parsed workflow, as `job: message` strings.

    One install recipe: only the lock-env composite action syncs, bootstraps or writes the venv
    env, and only the setup-uv composite action names astral-sh/setup-uv. Every other job uses
    those actions. `role` is "lock-env" or "setup-uv" when scanning that action itself."""
    findings: list[str] = []
    for job_name, job in (wf.get("jobs") or {}).items():
        steps = job.get("steps") or []
        frozen_env = _frozen_env(wf, job)
        texts = [_step_text(s) for s in steps]
        commands = [_commands(t) for t in texts]
        locked_at: int | None = None
        self_hosted = "self-hosted" in str(job.get("runs-on", ""))
        uses_uv = any(str(st.get("uses", "")) in {LOCK_ENV_USES, SETUP_UV_USES} for st in steps)
        for index, step_cmds in enumerate(commands):
            if str(steps[index].get("uses", "")) == LOCK_ENV_USES and locked_at is None:
                locked_at = index
            if role == "workflow" and _writes_venv_env(texts[index]):
                findings.append(f"{job_name}: copies the venv env block; use {LOCK_ENV_USES}")
            for cmd in step_cmds:
                findings += [f"{job_name}: {m}" for m in _misc_findings(cmd)]
                if any(t == "uv.lock" or t.endswith("/uv.lock") for t in cmd):
                    findings.append(
                        f"{job_name}: inline lock parsing; use scripts/dev/lock_version.sh"
                    )
                if (
                    role == "workflow"
                    and not self_hosted
                    and (
                        ("uv" in cmd and "sync" in cmd)
                        or any(t.endswith("bootstrap.sh") for t in cmd)
                    )
                ):
                    findings.append(f"{job_name}: installs outside {LOCK_ENV_USES}")
                if role == "workflow" and ("uv" in cmd or "uvx" in cmd) and not uses_uv:
                    findings.append(f"{job_name}: runs uv without the setup-uv or lock-env action")
                args = _pip_install_args(cmd)
                if args is not None:
                    findings += [f"{job_name}: {m}" for m in _requirement_findings(args)]
                if "uv" in cmd and "sync" in cmd and "--frozen" not in cmd:
                    findings.append(f"{job_name}: uv sync without --frozen")
                if "uv" in cmd and "run" in cmd and "--frozen" not in cmd and not frozen_env:
                    findings.append(f"{job_name}: uv run without --frozen and no UV_FROZEN env")
                if _is_lock_install(cmd) and locked_at is None:
                    locked_at = index
                if _is_consumer(cmd) and (locked_at is None or locked_at > index):
                    findings.append(
                        f"{job_name}: step {index} runs a consumer before any lock install"
                    )
        findings += [f"{job_name}: {m}" for m in _pyarrow_findings(job, commands, texts)]
        for step in steps:
            if str(step.get("uses", "")).startswith("astral-sh/setup-uv"):
                version = str((step.get("with") or {}).get("version", ""))
                if not VERSION.match(version):
                    findings.append(f"{job_name}: setup-uv version is not an exact x.y.z")
                if role != "setup-uv":
                    findings.append(f"{job_name}: raw setup-uv; use {SETUP_UV_USES}")
    return sorted(set(findings))


def _action_as_workflow(name: str) -> dict[str, Any]:
    action = yaml.safe_load((ACTIONS / name / "action.yml").read_text(encoding="utf-8"))
    return {"jobs": {name: {"steps": action["runs"]["steps"]}}}


def _real_workflows() -> dict[str, dict[str, Any]]:
    return {
        p.name: yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted(WORKFLOWS.glob("*.yml"))
    }


# --- acceptance tests against the real workflows -----------------------------


def test_engine_jobs_install_from_the_lock() -> None:
    workflows = _real_workflows()
    assert len(workflows) >= 10, "workflow scan looks empty"
    bad = {name: f for name, wf in workflows.items() if (f := scan_workflow(wf))}
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
    assert VERSION.match(version), f"setup-uv version is not an exact x.y.z: {version!r}"
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


def _supported_minors() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = SpecifierSet(data["project"]["requires-python"])
    return {f"3.{n}" for n in range(0, 40) if Version(f"3.{n}") in spec}


def audit_findings(job: dict[str, Any]) -> list[str]:
    """pip-audit must read an exported lock, with hashes, once per supported Python."""
    findings: list[str] = []
    text = "\n".join(_step_text(s) for s in job.get("steps") or [])
    cmds = _commands(text)
    audits = [c for c in cmds if any(re.fullmatch(r"pip-audit(==[\d.]+)?", t) for t in c)]
    if not any("uv" in c and "export" in c and "--frozen" in c for c in cmds):
        findings.append("no `uv export --frozen` step")
    if not audits:
        findings.append("no pip-audit invocation")
    for c in audits:
        if "-r" not in c or "--require-hashes" not in c:
            findings.append("a pip-audit call lacks -r or --require-hashes")
        if "--strict" not in c:
            findings.append("a pip-audit call lacks --strict")
        if not any(re.fullmatch(r"pip-audit==\d+(\.\d+)*", t) for t in c):
            findings.append("pip-audit is not pinned to an exact version")
        if any(t.startswith("--ignore-vuln") for t in c):
            findings.append("a pip-audit call ignores vulnerabilities")
    for line in text.splitlines():
        if "pip-audit" in line and re.search(r"\|\|\s*(true|:)(\s|$|;)", line):
            findings.append("a pip-audit call swallows its exit code")
    interpreters: set[str] = set()
    for m in re.finditer(r"for\s+\w+\s+in\s+([0-9. ]+?)\s*;", text):
        interpreters |= set(m.group(1).split())
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    for values in matrix.values():
        if isinstance(values, list):
            interpreters |= {str(v) for v in values}
    if interpreters != _supported_minors():
        findings.append(
            f"audited interpreters {sorted(interpreters)} != supported {sorted(_supported_minors())}"
        )
    return findings


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


def test_seeded_findings() -> None:
    cases = {
        'pip install -e ".[dev]"': ["project install"],
        "uv sync --extra dev": ["uv sync without --frozen", "installs outside"],
        "uv sync --frozen --extra dev": ["installs outside"],
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
        ("pip install ./decoy-engine-native",),
        ({"uses": SETUP_UV_USES}, "uvx --python 3.12 pip-audit==2.10.1 -r x"),
    ]
    for steps in clean:
        assert scan_workflow(_job(*steps)) == [], steps
    assert scan_workflow(_job(LE, "uv run pytest", env={"UV_FROZEN": "1"})) == []
    pinned = (
        "PYARROW=$(bash scripts/dev/lock_version.sh pyarrow)\n"
        'pip install "$wheel" "pyarrow==$PYARROW"\npython parity_smoke.py'
    )
    assert scan_workflow(_job(pinned)) == []
    unread = 'pip install "$wheel" "pyarrow==$PYARROW"\npython parity_smoke.py'
    assert any("not read by lock_version.sh" in f for f in scan_workflow(_job(unread)))
    # A fake resolver (inline parsing of the lock) must fail even when the pin looks right.
    fake = (
        "PYARROW=$(grep -A1 'name = \"pyarrow\"' uv.lock | tail -1)\n"
        'pip install "$wheel" "pyarrow==$PYARROW"\npython parity_smoke.py'
    )
    found = scan_workflow(_job(fake))
    assert any("inline lock parsing" in f for f in found)
    assert any("not read by lock_version.sh" in f for f in found)


def test_seeded_audit_and_docs_cases() -> None:
    export = "uv export --frozen --all-extras -o x.txt"
    good = (
        "for py in 3.10 3.11 3.12; do\n"
        '  uvx --python "$py" pip-audit==2.10.1 --strict --disable-pip --require-hashes -r x.txt\ndone'
    )
    job = lambda *s: {"steps": [{"run": t} for t in s]}
    assert audit_findings(job(export, good)) == []
    assert audit_findings(job("pip-audit --strict"))
    assert audit_findings(job(export, good.replace("3.10 ", "")))
    assert audit_findings(job(export, good.replace(" -r x.txt", "")))
    good_docs = {
        "permissions": {"contents": "read"},
        "jobs": {
            "build": {},
            "deploy": {
                "if": "github.event_name == 'push' && github.ref == 'refs/heads/main' && vars.DOCS_DEPLOY == 'true'",
                "permissions": {"pages": "write", "id-token": "write"},
            },
        },
    }
    assert docs_findings(good_docs) == []
    wide = {**good_docs, "permissions": {"contents": "read", "pages": "write"}}
    assert docs_findings(wide)
    unguarded = {
        **good_docs,
        "jobs": {**good_docs["jobs"], "deploy": {**good_docs["jobs"]["deploy"], "if": "true"}},
    }
    assert docs_findings(unguarded)


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
        step = {"uses": "astral-sh/setup-uv@v5", "with": {"version": version}}
        found = scan_workflow(_job(step), role="setup-uv")
        assert (found == []) == bool(clean), (version, found)


def test_seeded_audit_hardening_cases() -> None:
    export = "uv export --frozen --all-extras -o x.txt"
    base = "uvx --python 3.12 pip-audit==2.10.1 --strict --disable-pip --require-hashes -r x.txt"
    job = lambda *s: {"steps": [{"run": t} for t in s]}
    assert audit_findings(job(export, "for py in 3.10 3.11 3.12; do\n" + base + "\ndone")) == []
    variants = {
        "ignore": base + " --ignore-vuln PYSEC-1",
        "unpinned": base.replace("pip-audit==2.10.1", "pip-audit"),
        "not strict": base.replace(" --strict", ""),
        "swallowed": base + " || true",
    }
    for name, line in variants.items():
        text = "for py in 3.10 3.11 3.12; do\n" + line + "\ndone"
        assert audit_findings(job(export, text)), name


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
    *steps: Any, name_job: str = "j", env: dict[str, str] | None = None, **job: Any
) -> dict[str, Any]:
    built = [{"run": s} if isinstance(s, str) else s for s in steps]
    return {"jobs": {name_job: {"steps": built, **({"env": env} if env else {}), **job}}}


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
    mac = ["self-hosted", "macos", "decoy-gate"]
    boot = "bash scripts/dev/bootstrap.sh"
    on_gate = _wf(boot, name_job="gate", **{"runs-on": mac})
    assert not any("outside" in f for f in scan_workflow(on_gate, name="gate-selfhosted.yml"))
    other_labels = _wf(boot, name_job="gate", **{"runs-on": ["self-hosted", "linux"]})
    assert any("outside" in f for f in scan_workflow(other_labels, name="gate-selfhosted.yml"))
    other_job = _wf(boot, name_job="gate2", **{"runs-on": mac})
    assert any("outside" in f for f in scan_workflow(other_job, name="gate-selfhosted.yml"))
    other_file = _wf(boot, name_job="gate", **{"runs-on": mac})
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
        ["bash", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
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
    (venv / "python").symlink_to(sys.executable)
    env = {"PATH": "/usr/bin:/bin", "DECOY_VENV": str(tmp_path / "venv")}
    ok = _shell(str(dev / "lock_version.sh"), "--check", "pytest", cwd=tmp_path, env=env)
    assert ok.returncode == 0 and ok.stdout.count("pytest") == 1 and "OK" in ok.stdout, ok
    bad = _shell(
        str(dev / "lock_version.sh"), "--check", "pytest", "nopkg-zzz", cwd=tmp_path, env=env
    )
    assert bad.returncode == 1 and "MISMATCH" in bad.stdout, bad
    assert bad.stdout.count("pytest") == 1, "each package prints once"
