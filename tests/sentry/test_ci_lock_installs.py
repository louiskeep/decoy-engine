"""CI lock-install sentry (CI-E1): hosted engine jobs install from uv.lock, never re-resolve.

A hosted job that runs `pip install -e ".[dev]"` resolves today's PyPI, so it tests a different
dependency set than the Mac gate and the certified DP profile (pyarrow 25 vs the locked 24 made
two error-text tests red). Every workflow is scanned with no allowlist. Order is checked by step
index, not by data flow: a step that deletes the venv between install and use is not seen.
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


# --- parsing -----------------------------------------------------------------


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
    lock_read = any("uv.lock" in t for t in texts)
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
                    findings.append("parity smoke pyarrow pin is not read from uv.lock")
    return findings


def scan_workflow(wf: dict[str, Any]) -> list[str]:
    """Every lock-install violation in one parsed workflow, as `job: message` strings."""
    findings: list[str] = []
    for job_name, job in (wf.get("jobs") or {}).items():
        steps = job.get("steps") or []
        frozen_env = _frozen_env(wf, job)
        texts = [_step_text(s) for s in steps]
        commands = [_commands(t) for t in texts]
        locked_at: int | None = None
        for index, step_cmds in enumerate(commands):
            for cmd in step_cmds:
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
            if str(step.get("uses", "")).startswith("astral-sh/setup-uv") and not (
                step.get("with") or {}
            ).get("version"):
                findings.append(f"{job_name}: setup-uv without a pinned version")
    return sorted(set(findings))


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
    assert not bad, "hosted jobs not installed from uv.lock:\n" + "\n".join(
        f"  {name}: {m}" for name, msgs in bad.items() for m in msgs
    )


def _all_steps() -> list[dict[str, Any]]:
    return [
        s
        for wf in _real_workflows().values()
        for job in (wf.get("jobs") or {}).values()
        for s in job.get("steps") or []
    ]


def test_setup_uv_is_pinned() -> None:
    uv_steps = [s for s in _all_steps() if str(s.get("uses", "")).startswith("astral-sh/setup-uv")]
    versions = {(s.get("with") or {}).get("version") for s in uv_steps}
    assert None not in versions, "a setup-uv step has no with.version"
    assert len(uv_steps) >= 8, "expected setup-uv in every converted job"
    assert len(versions) == 1, f"setup-uv versions differ: {sorted(map(str, versions))}"
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


def test_seeded_findings() -> None:
    cases = {
        'pip install -e ".[dev]"': ["project install"],
        "uv sync --extra dev": ["uv sync without --frozen"],
        "pip install .[dev]": ["project install"],
        "python -m pip install -e .[docs]": ["project install"],
        "uv pip install -e .": ["project install"],
        "pip install pip-audit": ["not an exact == pin"],
        "pip install uv": ["not an exact == pin"],
    }
    for text, expected in cases.items():
        found = scan_workflow(_job(text))
        assert found and all(any(e in f for f in found) for e in expected), (text, found)
    assert any("project install" in f for f in scan_workflow(_job("echo hi\npip install -e .")))
    assert any(
        "project install" in f for f in scan_workflow(_job("set -e\n  pip install \\\n    -e ."))
    )
    assert any("consumer before" in f for f in scan_workflow(_job("pytest tests")))
    assert any(
        "consumer before" in f
        for f in scan_workflow(_job("pytest tests", "uv sync --frozen --extra dev"))
    )
    assert any(
        "uv run without" in f for f in scan_workflow(_job("uv sync --frozen", "uv run pytest"))
    )
    codspeed = {"uses": "CodSpeedHQ/action@v4", "with": {"run": "pytest tests/codspeed/"}}
    assert any("consumer before" in f for f in scan_workflow(_job(codspeed)))
    assert any(
        "setup-uv without" in f for f in scan_workflow(_job({"uses": "astral-sh/setup-uv@v5"}))
    )
    smoke = "pip install \"$wheel\" 'pyarrow>=14'\npython parity_smoke.py"
    found = scan_workflow(_job(smoke))
    assert any("lock-read pin" in f for f in found) and any("exact == pin" in f for f in found)


def test_seeded_clean_jobs() -> None:
    clean = [
        ("uv sync --frozen --extra dev", "pytest tests"),
        ("bash scripts/dev/bootstrap.sh", "pytest tests"),
        ("pip install ruff==0.15.22", "ruff check src"),
        ("uv pip install --python x build==1.2.2",),
        ("uv sync --frozen", "uv run --frozen pytest"),
        ("pip install ./decoy-engine-native",),
    ]
    for steps in clean:
        assert scan_workflow(_job(*steps)) == [], steps
    assert scan_workflow(_job("uv sync --frozen", "uv run pytest", env={"UV_FROZEN": "1"})) == []
    pinned = (
        "PYARROW=$(awk '{print}' uv.lock)\n"
        'pip install "$wheel" "pyarrow==$PYARROW"\npython parity_smoke.py'
    )
    assert scan_workflow(_job(pinned)) == []
    unread = 'pip install "$wheel" "pyarrow==$PYARROW"\npython parity_smoke.py'
    assert any("not read from uv.lock" in f for f in scan_workflow(_job(unread)))


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
