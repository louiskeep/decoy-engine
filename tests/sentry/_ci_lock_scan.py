"""Scanner behind the CI lock-install sentry (CI-E1): what a hosted job may install, and how.

The tests live in test_ci_lock_installs.py. Every workflow and composite action is scanned. The
rules, each seeded in the test file: one install recipe (the lock-env action), one uv pin (the
setup-uv action, pinned to a commit SHA), no unpinned index or project install by pip, no inline
parsing of uv.lock, a pip-audit leg that cannot be hidden, and explicit least-privilege
permissions. Order is checked by step index, not data flow: a step that deletes the venv between
install and use is not seen.

Shell parsing lives in ci_scan/_shell_tokens.py, one file shared byte-for-byte with the CLI
repository. Against the CI-E1 scanner (f2b7b3da) a 708-command differential scan shows only:
- reworded findings: an editable install reports `project install by pip (-e)` once, because
  `-e` now consumes its value instead of also reporting `.` or the path, and `--editable` is
  reported as `(-e)`;
- removed false positives: index options flagged on non-package commands (`ls -i`, `rm -f`), and
  `uv run --frozen sync` reported as an install;
- stricter, on purpose: index environment variables and `-f`/`--find-links`, the bodies of
  `bash -c "..."`, global options before pip and uv subcommands, `uv pip sync`, uv options this
  scanner does not know, and fetching `uv tool` and pipx verbs (`inject`, `upgrade`, ...).
`uv tool run` keeps its base rule: it needs `--frozen` or UV_FROZEN like `uv run`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from tests.sentry.ci_scan._shell_tokens import (
    EXACT_PIN,
    INDEX_OPTIONS,
    code_lines,
    env_mapping_index_names,
    index_env_names,
    is_package_command,
    parse_install_args,
    pip_install_args,
    tool_fetch,
    uv_run_findings,
    uv_subcommands,
)
from tests.sentry.ci_scan._shell_tokens import commands as shell_commands

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
CONSTRAINTS = ROOT / ".github" / "build-constraints.txt"
ACTIONS = ROOT / ".github" / "actions"
LOCK_ENV_USES = "./.github/actions/lock-env"
SETUP_UV_USES = "./.github/actions/setup-uv"
# `uv pip sync` installs a requirements file just as `uv sync` installs the project.
SYNC_SUBCOMMANDS = {"sync", "pip sync"}
VERSION = re.compile(r"\d+\.\d+\.\d+")
SETUP_UV_SHA = re.compile(r"astral-sh/setup-uv@[0-9a-f]{40}")

# Commands that need the engine installed (or its docs/typing toolchain) to mean anything.
CONSUMER_TOKENS = {"pytest", "sphinx-build", "mypy"}
CONSUMER_SUFFIXES = ("prove_regression.py", "scripts/test_flight.py")
_VENV_NAMES = ("UV_PROJECT_ENVIRONMENT", "DECOY_VENV")
_EXIT_ZERO = re.compile(r"\bexit(\s+(0|\"0\"|'0'))?\s*($|[;&|)}])")
_ERREXIT_OFF = re.compile(r"\bset\s+(\+e\b|\+o\s+errexit)")

# Write scopes the repo grants on purpose, keyed (workflow file, job name; "" = workflow level).
# A ratchet: it may shrink, and a new entry needs a reviewed change plus a reason here.
WRITE_ALLOWLIST: dict[tuple[str, str], frozenset[str]] = {
    ("benchmark.yml", ""): frozenset({"pull-requests"}),  # posts the regression comment
    ("docs.yml", "deploy"): frozenset({"pages", "id-token"}),  # Pages deploy, main push only
    ("native-companion-release.yml", "sign-publish"): frozenset({"contents"}),  # makes the release
}
# The one self-hosted job that may bootstrap without the lock-env action: the merge gate, which runs
# on any decoy-gate runner (the Mac or the Linux GCP runner).
MAC_GATE = ("gate-selfhosted.yml", "gate", ["self-hosted", "decoy-gate"])


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


def _is_consumer(cmd: list[str]) -> bool:
    if pip_install_args(cmd) is not None:
        return False
    return any(t in CONSUMER_TOKENS or t.endswith(CONSUMER_SUFFIXES) for t in cmd)


def _is_lock_install(cmd: list[str]) -> bool:
    if any(t.endswith("bootstrap.sh") for t in cmd):
        return True
    return bool(SYNC_SUBCOMMANDS & set(uv_subcommands(cmd))) and "--frozen" in cmd


def _wheel_vars(text: str) -> set[str]:
    """Shell variables holding a built wheel: assigned a `*.whl` value, or derived from one."""
    assigns = re.findall(r"(?m)^\s*(?:export\s+)?(\w+)=(.*)$", re.sub(r"\\\n\s*", " ", text))
    known: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, value in assigns:
            derived = ".whl" in value or any(re.search(rf"\${{?{v}\b", value) for v in known)
            if derived and name not in known:
                known.add(name)
                changed = True
    return known


def _path_finding(arg: str, wheels: set[str]) -> str | None:
    """A path-like install argument is fine only as a built wheel or the native companion."""
    if arg == "." or re.search(r"(^|/)\.\[", arg):
        return f"project install by pip ({arg})"
    if arg.endswith(".whl") or any(c.startswith("decoy-engine-native") for c in arg.split("/")):
        return None
    names = re.findall(r"\$\{?(\w+)", arg)
    if names and all(n in wheels for n in names):
        return None
    return f"project install by pip ({arg})"


def _requirement_findings(args: list[str], wheels: set[str]) -> list[str]:
    parsed = parse_install_args(args)
    findings = ["pip install -r reads an unlocked requirements file" for _ in parsed.requirements]
    findings += [f"package index override ({opt})" for opt in parsed.index_overrides]
    findings += ["project install by pip (-e)" for _ in parsed.editables]
    for arg in parsed.positionals:
        if arg.startswith((".", "/", "$", "~")) or ("/" in arg and "==" not in arg):
            if msg := _path_finding(arg, wheels):
                findings.append(msg)
        elif not EXACT_PIN.fullmatch(arg):
            findings.append(f"requirement is not an exact == pin ({arg})")
    return findings


def _tool_findings(cmd: list[str]) -> list[str]:
    """uvx, `uv tool install|run` and pipx fetch a tool from the index: pin it exactly."""
    fetch = tool_fetch(cmd)
    if fetch is None:
        return []
    skip = False
    for arg in fetch[1]:
        if skip:
            skip = False
            continue
        if arg in {"--python", "-p", "--with", "--from", *INDEX_OPTIONS}:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return [] if EXACT_PIN.fullmatch(arg) else [f"tool is not an exact == pin ({arg})"]
    return ["tool command names no package"]


def _misc_findings(cmd: list[str]) -> list[str]:
    findings = _tool_findings(cmd)
    subs = set(uv_subcommands(cmd))
    if "lock" in subs and "--check" not in cmd:
        findings.append("uv lock without --check re-resolves the lock")
    if is_package_command(cmd):
        findings += [f"package index override ({t})" for t in cmd if t in INDEX_OPTIONS]
    findings += [f"index environment variable ({n})" for n in index_env_names(cmd)]
    for sub in sorted(subs & {"add", "remove"}):
        findings.append(f"uv {sub} edits the dependency set")
    return findings


def _writes_venv_env(text: str) -> bool:
    """True when step text puts the venv variables into $GITHUB_ENV: one line or a heredoc."""
    names = "|".join(_VENV_NAMES)
    if re.search(rf"({names})=.*GITHUB_ENV", text):
        return True
    heredoc = r"(?m)^([^\n]*)<<-?\s*['\"]?(\w+)['\"]?([^\n]*)\n(.*?)\n\s*\2\b"
    for m in re.finditer(heredoc, text, re.S):
        if "GITHUB_ENV" in m.group(1) + m.group(3) and re.search(rf"({names})=", m.group(4)):
            return True
    return False


def _env_sets_venv(*envs: Any) -> bool:
    return any(n in (env or {}) for env in envs for n in _VENV_NAMES)


def _frozen_env(wf: dict[str, Any], job: dict[str, Any]) -> bool:
    return any(str((env or {}).get("UV_FROZEN")) == "1" for env in (wf.get("env"), job.get("env")))


def is_mac_gate(name: str, job_name: str, job: dict[str, Any]) -> bool:
    """The exact job allowed to bootstrap on a self-hosted runner: workflow, job and labels all match."""
    return (name, job_name, job.get("runs-on")) == MAC_GATE


def _pyarrow_findings(commands: list[list[list[str]]], texts: list[str]) -> list[str]:
    """(f): a parity-smoke job may install pyarrow only as `pyarrow==$VAR`, read from uv.lock."""
    if not any("parity_smoke.py" in t for t in texts):
        return []
    findings: list[str] = []
    lock_read = any("lock_version.sh" in t for t in texts)
    for step_cmds in commands:
        for cmd in step_cmds:
            for arg in pip_install_args(cmd) or []:
                if not arg.startswith("pyarrow"):
                    continue
                if not re.fullmatch(r"pyarrow==\$\{?[A-Za-z_]+\}?", arg):
                    findings.append(
                        f"parity smoke installs pyarrow other than the lock-read pin ({arg})"
                    )
                elif not lock_read:
                    findings.append("parity smoke pyarrow pin is not read by lock_version.sh")
    return findings


def _step_findings(
    job_name: str, step: dict[str, Any], text: str, cmds: list[list[str]], role: str
) -> list[str]:
    """Findings that need only one step: venv env copies, inline lock parsing, index and pip use."""
    findings: list[str] = []
    if role == "workflow":
        if _writes_venv_env(text) or _env_sets_venv(step.get("env")):
            findings.append(f"{job_name}: copies the venv env block; use {LOCK_ENV_USES}")
    if any("uv.lock" in line for line in code_lines(text)):
        findings.append(f"{job_name}: inline lock parsing; use scripts/dev/lock_version.sh")
    wheels = _wheel_vars(text)
    findings += [
        f"{job_name}: index environment variable ({n}) in env"
        for n in env_mapping_index_names(step.get("env"))
    ]
    for cmd in cmds:
        findings += [f"{job_name}: {m}" for m in _misc_findings(cmd)]
        args = pip_install_args(cmd)
        if args is not None:
            findings += [f"{job_name}: {m}" for m in _requirement_findings(args, wheels)]
        if SYNC_SUBCOMMANDS & set(uv_subcommands(cmd)) and "--frozen" not in cmd:
            findings.append(f"{job_name}: uv sync without --frozen")
    return findings


def scan_workflow(wf: dict[str, Any], *, role: str = "workflow", name: str = "") -> list[str]:
    """Every lock-install violation in one parsed workflow, as `job: message` strings.

    One install recipe: only the lock-env composite action syncs, bootstraps or writes the venv
    env, and only the setup-uv composite action names astral-sh/setup-uv. Every other job uses
    those actions. `role` is "lock-env" or "setup-uv" when scanning that action itself; `name` is
    the workflow file name, used for the one Mac-gate exemption."""
    findings: list[str] = []
    if role == "workflow" and _env_sets_venv(wf.get("env")):
        findings.append("workflow: copies the venv env block; use " + LOCK_ENV_USES)
    findings += [
        f"workflow: index environment variable ({n}) in env"
        for n in env_mapping_index_names(wf.get("env"))
    ]
    for job_name, job in (wf.get("jobs") or {}).items():
        steps = job.get("steps") or []
        frozen_env = _frozen_env(wf, job)
        texts = [_step_text(s) for s in steps]
        commands = [shell_commands(t) for t in texts]
        locked_at: int | None = None
        exempt = is_mac_gate(name, job_name, job)
        uses_uv = any(str(st.get("uses", "")) in {LOCK_ENV_USES, SETUP_UV_USES} for st in steps)
        if role == "workflow" and _env_sets_venv(job.get("env")):
            findings.append(f"{job_name}: copies the venv env block; use {LOCK_ENV_USES}")
        findings += [
            f"{job_name}: index environment variable ({n}) in env"
            for n in env_mapping_index_names(job.get("env"))
        ]
        for index, step_cmds in enumerate(commands):
            if str(steps[index].get("uses", "")) == LOCK_ENV_USES and locked_at is None:
                locked_at = index
            findings += _step_findings(job_name, steps[index], texts[index], step_cmds, role)
            for cmd in step_cmds:
                installs = SYNC_SUBCOMMANDS & set(uv_subcommands(cmd)) or any(
                    t.endswith("bootstrap.sh") for t in cmd
                )
                if role == "workflow" and not exempt and installs:
                    findings.append(f"{job_name}: installs outside {LOCK_ENV_USES}")
                if role == "workflow" and ("uv" in cmd or "uvx" in cmd) and not uses_uv:
                    findings.append(f"{job_name}: runs uv without the setup-uv or lock-env action")
                findings += [
                    f"{job_name}: {m}" for m in uv_run_findings(cmd, frozen_env=frozen_env)
                ]
                if _is_lock_install(cmd) and locked_at is None:
                    locked_at = index
                if _is_consumer(cmd) and (locked_at is None or locked_at > index):
                    findings.append(
                        f"{job_name}: step {index} runs a consumer before any lock install"
                    )
        findings += [f"{job_name}: {m}" for m in _pyarrow_findings(commands, texts)]
        for step in steps:
            uses = str(step.get("uses", ""))
            if not uses.startswith("astral-sh/setup-uv"):
                continue
            if not VERSION.fullmatch(str((step.get("with") or {}).get("version", ""))):
                findings.append(f"{job_name}: setup-uv version is not an exact x.y.z")
            if not SETUP_UV_SHA.fullmatch(uses):
                findings.append(f"{job_name}: setup-uv is not pinned to a commit SHA")
            if role != "setup-uv":
                findings.append(f"{job_name}: raw setup-uv; use {SETUP_UV_USES}")
    return sorted(set(findings))


def _action_as_workflow(name: str, path: Path | None = None) -> dict[str, Any]:
    if path is None:
        path = next((ACTIONS / name).glob("action.y*ml"))
    action = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {"jobs": {name: {"steps": (action.get("runs") or {}).get("steps") or []}}}


def scan_actions(actions_dir: Path) -> dict[str, list[str]]:
    """Scan every composite action under `actions_dir`, found by glob, not by a known list."""
    bad: dict[str, list[str]] = {}
    for path in sorted(actions_dir.glob("**/action.y*ml")):
        name = path.parent.name
        role = name if name in {"lock-env", "setup-uv"} else "workflow"
        if found := scan_workflow(_action_as_workflow(name, path), role=role):
            bad[name] = found
    return bad


def _real_workflows() -> dict[str, dict[str, Any]]:
    return {
        p.name: yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted(WORKFLOWS.glob("*.yml"))
    }


# --- permissions ----------------------------------------------------------------


def _write_scopes(perms: Any) -> set[str] | None:
    """Scopes granted beyond read in one permissions value; None when it is not a mapping."""
    if not isinstance(perms, dict):
        return None
    return {str(k) for k, v in perms.items() if str(v) not in {"read", "none"}}


def permission_findings(
    name: str,
    wf: dict[str, Any],
    allowlist: dict[tuple[str, str], frozenset[str]] | None = None,
) -> list[str]:
    """A workflow declares top-level permissions, and only allowlisted places hold write scopes."""
    allow = WRITE_ALLOWLIST if allowlist is None else allowlist
    findings: list[str] = []
    places: dict[str, Any] = {"": wf.get("permissions")}
    for job_name, job in (wf.get("jobs") or {}).items():
        if "permissions" in job:
            places[job_name] = job["permissions"]
    if not isinstance(places[""], dict):
        findings.append(f"{name}: no top-level permissions mapping (got {places['']!r})")
    for place, perms in places.items():
        if place == "" and not isinstance(perms, dict):
            continue
        scopes = _write_scopes(perms)
        label = f"{name} {place or 'workflow'}"
        if scopes is None:
            findings.append(f"{label}: permissions is not a mapping ({perms!r})")
            continue
        if extra := scopes - allow.get((name, place), frozenset()):
            findings.append(f"{label}: holds write scopes {sorted(extra)} outside the allowlist")
    return findings


# --- pip-audit ------------------------------------------------------------------


def _supported_minors() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = SpecifierSet(data["project"]["requires-python"])
    return {f"3.{n}" for n in range(0, 40) if Version(f"3.{n}") in spec}


def _hides_failure(value: Any) -> bool:
    """continue-on-error is allowed only as a literal false."""
    return value not in (False, "false")


def _audit_command_findings(cmd: list[str]) -> list[str]:
    findings: list[str] = []
    if "-r" not in cmd or "--require-hashes" not in cmd:
        findings.append("a pip-audit call lacks -r or --require-hashes")
    if "--strict" not in cmd:
        findings.append("a pip-audit call lacks --strict")
    if not any(re.fullmatch(r"pip-audit==\d+(\.\d+)*", t) for t in cmd):
        findings.append("pip-audit is not pinned to an exact version")
    if any(t.startswith("--ignore-vuln") for t in cmd):
        findings.append("a pip-audit call ignores vulnerabilities")
    return findings


def _audit_legs(job: dict[str, Any], step: dict[str, Any], cmd: list[str]) -> list[str]:
    """The interpreter of the audit command must come from the job matrix, which must be all minors."""
    strategy = job.get("strategy") or {}
    matrix = strategy.get("matrix") or {}
    findings: list[str] = []
    if strategy.get("fail-fast") is not False:
        findings.append("pip-audit matrix needs fail-fast: false so every leg reports")
    if {"include", "exclude"} & set(matrix):
        findings.append("pip-audit matrix uses include/exclude, which can drop a leg")
    py = cmd[cmd.index("--python") + 1] if "--python" in cmd[:-1] else ""
    var = re.fullmatch(r"\$\{?(\w+)\}?", py)
    source = str((step.get("env") or {}).get(var.group(1), "")) if var else ""
    key = re.fullmatch(r"\$\{\{\s*matrix\.(\w+)\s*\}\}", source)
    if not key or not isinstance(matrix.get(key.group(1)), list):
        findings.append("the pip-audit interpreter is not taken from the job matrix")
        return findings
    legs = {str(v) for v in matrix[key.group(1)]}
    if legs != _supported_minors():
        findings.append(
            f"audited interpreters {sorted(legs)} != supported {sorted(_supported_minors())}"
        )
    return findings


def audit_findings(job: dict[str, Any]) -> list[str]:
    """pip-audit reads an exported lock, with hashes, once per supported Python, and cannot be hidden.

    One bare command per matrix leg: a wrapper (`||`, `;`, `&&`, a pipe, `if`), `exit 0`,
    `set +e`, a step `if`, or `continue-on-error` at step or job level would turn a red audit
    green, so each is a finding. The scan covers every step of the job, not only the audit step."""
    findings: list[str] = []
    steps = job.get("steps") or []
    texts = [_step_text(s) for s in steps]
    all_cmds = [c for t in texts for c in shell_commands(t)]
    audit_steps = [i for i, t in enumerate(texts) if "pip-audit" in " ".join(code_lines(t))]
    audits = [c for c in all_cmds if any(re.fullmatch(r"pip-audit(==[\d.]+)?", t) for t in c)]
    if not any("uv" in c and "export" in c and "--frozen" in c for c in all_cmds):
        findings.append("no `uv export --frozen` step")
    if len(audit_steps) != 1 or len(audits) != 1:
        findings.append(
            f"want exactly one pip-audit step and command, got {len(audit_steps)}/{len(audits)}"
        )
    for text in texts:
        code = "\n".join(code_lines(text))
        if _EXIT_ZERO.search(code):
            findings.append("a step exits 0 explicitly, which can hide the audit result")
        if _ERREXIT_OFF.search(code):
            findings.append("a step turns off errexit (set +e), which can hide the audit result")
    for holder, label in [(job, "job")] + [(s, "step") for s in steps]:
        if "continue-on-error" in holder and _hides_failure(holder["continue-on-error"]):
            findings.append(f"continue-on-error at {label} level hides the audit result")
    if "if" in job:
        findings.append("a job-level `if` can skip the audit")
    if len(audit_steps) == 1:
        step, lines = steps[audit_steps[0]], code_lines(texts[audit_steps[0]])
        cmds = shell_commands(texts[audit_steps[0]])
        bare = re.sub(r"\"[^\"]*\"|'[^']*'", "", lines[0]) if lines else ""
        if "if" in step:
            findings.append("a step-level `if` can skip the audit")
        if (
            len(lines) != 1
            or len(cmds) != 1
            or re.search(r"[|;&!(]|^\s*(if|for|while|exit)\b", bare)
        ):
            findings.append(
                "the audit step must be one bare command (no wrapper, pipe, `if`, `||`)"
            )
        elif cmds[0][0] not in {"uvx", "pip-audit", "uv"}:
            findings.append("the audit command must start with uvx, uv or pip-audit")
        if len(cmds) == 1:
            findings += _audit_legs(job, step, cmds[0])
    for c in audits:
        findings += _audit_command_findings(c)
    return sorted(set(findings))
