"""Environment preflight for the Rust coverage evidence audit
(docs/plans/2026-09-30-rust-coverage-evidence-audit.md).

Checks, without running any masking/generation workload, whether the
prerequisites the audit's real-run entry points need are present on this
host: Postgres, Docker, the platform's cgroup job supervisor socket, cgroup
v2 delegation, the optional native companion, and the pinned commits of the
three repos the audit spans (engine, platform, CLI).

Every check is read-only or self-cleaning:
- Postgres credentials are read from `docker inspect` at call time, never
  hardcoded, and never written to the JSON output (only user/dbname/host/port
  are recorded).
- The cgroup delegation probe creates one test child cgroup directory under
  the CALLING PROCESS's own slice and removes it immediately; it never
  touches `/sys/fs/cgroup` system-wide state, quotas, or controllers.
- The native companion probe imports `decoy_engine` from this worktree's
  `src/` (prepended to `sys.path`) so the result reflects the pinned engine
  commit rather than whatever commit the shared `.venv`'s editable install
  happens to point at (see `_engine_src_note`), but does not install or
  build anything.

Usage: `<engine .venv>/bin/python3 scripts/audit/preflight.py` (prints JSON to
stdout). The native-companion check needs decoy_engine's own dependencies
(pydantic, pyarrow, ...), so run it with the engine's `.venv` interpreter,
not the bare system `python3`; under a bare interpreter that one check
degrades to a recorded import error instead of crashing the whole preflight.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_ENGINE_WORKTREE_ROOT = _HERE.parents[2]
_ENGINE_SRC = _ENGINE_WORKTREE_ROOT / "src"
_PLATFORM_REPO = Path("/home/cam/vscode/decoy-platform")
_CLI_REPO = Path("/home/cam/vscode/decoy")

_PG_CONTAINER = "v3-0-fix-pg"
_PG_DEFAULT_PORT = 55438
_PG_DEFAULT_HOST = "127.0.0.1"

# Per the plan's pinned-commits section: engine main after PR #180.
_PLAN_PINNED_ENGINE_COMMIT = "8dc559e5"

# api/config.py:396 on decoy-platform origin/main, read 2026-09-30:
#   adaptive_scheduler_supervisor_socket_path: str =
#       "/run/decoy-supervisor/cgroup-supervisor.sock"
# Cross-checked at runtime below via `git show origin/main:api/config.py`
# rather than trusted as a hardcoded literal.
_EXPECTED_SUPERVISOR_SOCKET_FALLBACK = "/run/decoy-supervisor/cgroup-supervisor.sock"


def _run(cmd: list[str], *, cwd: str | None = None, timeout: float = 15.0) -> tuple[int, str, str]:
    """Run a fixed, vetted preflight command (docker/git/psql/which); every
    call site in this module passes a literal argv, never untrusted input, so
    S603/S607 (subprocess/partial-path) don't apply here."""
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        return 127, "", f"executable not found: {exc}"
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"timed out after {exc.timeout}s"
    except Exception as exc:  # never let one check crash the whole preflight
        return 1, "", f"{type(exc).__name__}: {exc}"


def check_docker() -> dict:
    rc_ps, out_ps, err_ps = _run(["docker", "ps"])
    rc_ver, out_ver, _err_ver = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    running = None
    if rc_ps == 0:
        # subtract header line
        running = max(len(out_ps.strip().splitlines()) - 1, 0)
    return {
        "docker_ps_ok": rc_ps == 0,
        "docker_ps_error": None if rc_ps == 0 else (err_ps.strip() or out_ps.strip()),
        "server_version": out_ver.strip() if rc_ver == 0 else None,
        "containers_running": running,
    }


def check_postgres() -> dict:
    result: dict = {
        "container": _PG_CONTAINER,
        "credentials_source": f"docker inspect {_PG_CONTAINER} --format '{{{{json .Config.Env}}}}'",
    }

    rc, out, err = _run(["docker", "inspect", _PG_CONTAINER, "--format", "{{json .Config.Env}}"])
    if rc != 0:
        result["docker_inspect_ok"] = False
        result["docker_inspect_error"] = err.strip() or out.strip()
        result["reachable"] = False
        return result
    result["docker_inspect_ok"] = True

    try:
        env_list: list[str] = json.loads(out.strip())
    except json.JSONDecodeError as exc:
        result["docker_inspect_ok"] = False
        result["docker_inspect_error"] = f"could not parse env JSON: {exc}"
        result["reachable"] = False
        return result

    env: dict[str, str] = dict(kv.split("=", 1) for kv in env_list if "=" in kv)
    password: str | None = env.get("POSTGRES_PASSWORD")
    # postgres:15 image defaults: POSTGRES_USER unset -> "postgres";
    # POSTGRES_DB unset -> same as POSTGRES_USER.
    user: str = env.get("POSTGRES_USER", "postgres")
    dbname: str = env.get("POSTGRES_DB", user)
    result["user"] = user
    result["dbname"] = dbname
    result["password_found"] = password is not None
    # Deliberately never include the password value itself in the recorded
    # output -- this JSON is committed to the repo.

    # Resolve the published host port dynamically rather than trusting the
    # value given in the task description.
    host, port, port_source = (
        _PG_DEFAULT_HOST,
        _PG_DEFAULT_PORT,
        "task-provided default (localhost:55438)",
    )
    rc_port, out_port, _ = _run(["docker", "port", _PG_CONTAINER, "5432/tcp"])
    if rc_port == 0 and out_port.strip():
        # e.g. "0.0.0.0:55438" (first line; container may publish on more
        # than one interface).
        first_line = out_port.strip().splitlines()[0]
        if ":" in first_line:
            mapped_host, mapped_port = first_line.rsplit(":", 1)
            # A published-port host of "0.0.0.0"/"::" means "all interfaces",
            # not a bind we're creating -- resolve it to a connectable loopback
            # address instead of literally dialing the wildcard.
            host = "127.0.0.1" if mapped_host in ("0.0.0.0", "::") else mapped_host  # noqa: S104
            try:
                port = int(mapped_port)
                port_source = f"docker port {_PG_CONTAINER} 5432/tcp"
            except ValueError:
                pass
    result["host"] = host
    result["port"] = port
    result["port_source"] = port_source

    if password is None:
        result["reachable"] = False
        result["error"] = "POSTGRES_PASSWORD not found in container env; cannot authenticate"
        return result

    psql_path = _run(["which", "psql"])[1].strip()
    if psql_path:
        # Password goes via env var for this one subprocess call only, never
        # via argv (would leak in `ps`) and never written to disk. Fixed
        # command, vetted args (host/port/user/dbname all come from the
        # container's own env or a hardcoded default): S603/S607 don't apply.
        env_with_pw = dict(os.environ)
        env_with_pw["PGPASSWORD"] = password
        try:
            proc = subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "psql",
                    "-h",
                    host,
                    "-p",
                    str(port),
                    "-U",
                    user,
                    "-d",
                    dbname,
                    "-tAc",
                    "select version();",
                ],
                env=env_with_pw,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            rc_psql, out_psql, err_psql = proc.returncode, proc.stdout, proc.stderr
        except Exception as exc:
            rc_psql, out_psql, err_psql = 1, "", f"{type(exc).__name__}: {exc}"

        result["check_method"] = "psql -tAc 'select version();'"
        result["reachable"] = rc_psql == 0
        if rc_psql == 0:
            result["server_reports"] = out_psql.strip()
        else:
            result["error"] = err_psql.strip() or out_psql.strip()
    else:
        # Fall back to a bare TCP connect if psql isn't on PATH -- weaker
        # (proves the port is open, not that auth works) but still honest
        # about which check ran.
        result["check_method"] = "raw TCP connect (psql not found on PATH)"
        try:
            with socket.create_connection((host, port), timeout=5.0):
                result["reachable"] = True
        except OSError as exc:
            result["reachable"] = False
            result["error"] = str(exc)

    return result


def check_cgroup_job_supervisor_socket() -> dict:
    result: dict = {
        "source": (
            "decoy-platform origin/main api/config.py "
            "Settings.adaptive_scheduler_supervisor_socket_path"
        )
    }
    rc, out, err = _run(
        ["git", "show", "origin/main:api/config.py"],
        cwd=str(_PLATFORM_REPO),
        timeout=20.0,
    )
    expected_path = None
    if rc == 0:
        for line in out.splitlines():
            if "adaptive_scheduler_supervisor_socket_path" in line and "=" in line:
                # line looks like:
                #   adaptive_scheduler_supervisor_socket_path: str = "/run/..."
                rhs = line.split("=", 1)[1].strip()
                if rhs.startswith('"') or rhs.startswith("'"):
                    expected_path = rhs.strip("\"'")
                break
        result["config_read_ok"] = expected_path is not None
        if expected_path is None:
            result["config_read_error"] = (
                "adaptive_scheduler_supervisor_socket_path not found in "
                "api/config.py at origin/main; using fallback literal"
            )
    else:
        result["config_read_ok"] = False
        result["config_read_error"] = err.strip() or out.strip()

    if expected_path is None:
        expected_path = _EXPECTED_SUPERVISOR_SOCKET_FALLBACK
        result["expected_path_source_note"] = "fallback literal, not read from origin/main"

    result["expected_path"] = expected_path
    exists = os.path.exists(expected_path)
    result["path_exists"] = exists
    result["is_socket"] = bool(exists and stat.S_ISSOCK(os.stat(expected_path).st_mode))
    if not exists:
        result["detail"] = (
            "socket path does not exist -- the adaptive-scheduler cgroup "
            "supervisor is not running on this host; any cell needing it "
            "(entry point 4, DispatchPlan launch) must run on an "
            "integration host, per the plan's preflight note"
        )
    return result


def check_cgroup_v2_delegation() -> dict:
    result: dict = {}
    cgroup_root = "/sys/fs/cgroup"
    result["cgroup_v2_mounted"] = os.path.isdir(cgroup_root) and os.path.exists(
        os.path.join(cgroup_root, "cgroup.controllers")
    )

    try:
        with open("/proc/self/cgroup") as f:
            raw = f.read().strip()
    except OSError as exc:
        result["error"] = f"could not read /proc/self/cgroup: {exc}"
        result["can_create_child_cgroup"] = False
        return result
    result["proc_self_cgroup_raw"] = raw

    # cgroup v2 unified hierarchy: a single line "0::<path>".
    own_rel_path = None
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            own_rel_path = parts[2]
            break
    if own_rel_path is None:
        result["error"] = "no cgroup v2 unified entry (0::...) in /proc/self/cgroup"
        result["can_create_child_cgroup"] = False
        return result

    own_abs_path = cgroup_root + own_rel_path
    result["own_cgroup_path"] = own_abs_path

    controllers_path = os.path.join(cgroup_root, "cgroup.controllers")
    try:
        with open(controllers_path) as f:
            result["controllers_at_root"] = f.read().split()
    except OSError as exc:
        result["controllers_at_root_error"] = str(exc)

    subtree_control_path = os.path.join(own_abs_path, "cgroup.subtree_control")
    try:
        with open(subtree_control_path) as f:
            result["subtree_control_own_slice"] = f.read().strip()
    except OSError as exc:
        result["subtree_control_own_slice_error"] = str(exc)

    # The actual delegation test: try to create ONE child cgroup directory
    # under our own slice, then remove it immediately. This changes nothing
    # system-wide -- it only proves or disproves write access to our own,
    # already-assigned cgroup subtree.
    test_dir = os.path.join(own_abs_path, f"audit-preflight-probe-{os.getpid()}")
    try:
        os.mkdir(test_dir)
    except OSError as exc:
        result["can_create_child_cgroup"] = False
        result["mkdir_error"] = str(exc)
        result["test_path"] = test_dir
        return result

    result["can_create_child_cgroup"] = True
    result["test_path"] = test_dir
    try:
        os.rmdir(test_dir)
        result["cleaned_up"] = True
    except OSError as exc:
        result["cleaned_up"] = False
        result["cleanup_error"] = str(exc)
    return result


def check_native_companion() -> dict:
    result: dict = {
        "engine_src_used": str(_ENGINE_SRC),
        "note": (
            "this worktree's src/ was prepended to sys.path so the probe "
            "reflects this worktree's HEAD, not the shared .venv's editable "
            "install (which points at the main checkout, currently behind "
            "the pinned commit -- see pinned_commits.engine below)"
        ),
    }
    sys.path.insert(0, str(_ENGINE_SRC))
    try:
        from decoy_engine.execution.native._companion_status import (
            native_companion_status,
        )
    except Exception as exc:
        result["import_error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        # keep sys.path clean for any later checks in this process
        if str(_ENGINE_SRC) in sys.path:
            sys.path.remove(str(_ENGINE_SRC))

    status = native_companion_status()
    result["present"] = status.present
    result["ok"] = status.ok
    result["reason"] = status.reason
    result["abi_expected"] = status.abi_expected
    result["abi_actual"] = status.abi_actual
    result["version"] = status.version
    result["cause"] = str(status.cause) if status.cause is not None else None

    module_path = None
    module_sha256 = None
    try:
        spec = importlib.util.find_spec("decoy_engine_native")
    except Exception:
        spec = None
    if spec is not None and spec.origin:
        module_path = spec.origin
        try:
            with open(spec.origin, "rb") as f:
                module_sha256 = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            module_sha256 = None
    result["module_path"] = module_path
    result["module_sha256"] = module_sha256
    return result


def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    return _run(["git", *args], cwd=str(repo), timeout=20.0)


def check_pinned_commits() -> dict:
    result: dict = {}

    rc, out, _ = _git(_ENGINE_WORKTREE_ROOT, "rev-parse", "HEAD")
    engine_head = out.strip() if rc == 0 else None
    rc_b, out_b, _ = _git(_ENGINE_WORKTREE_ROOT, "rev-parse", "--abbrev-ref", "HEAD")
    engine_branch = out_b.strip() if rc_b == 0 else None
    baseline_is_ancestor = None
    if engine_head:
        rc_anc, _, _ = _git(
            _ENGINE_WORKTREE_ROOT, "merge-base", "--is-ancestor", _PLAN_PINNED_ENGINE_COMMIT, "HEAD"
        )
        baseline_is_ancestor = rc_anc == 0
    result["engine"] = {
        "repo": str(_ENGINE_WORKTREE_ROOT),
        "worktree_head": engine_head,
        "branch": engine_branch,
        "plan_pinned_baseline": _PLAN_PINNED_ENGINE_COMMIT,
        "baseline_is_ancestor_of_head": baseline_is_ancestor,
    }

    rc, out, err = _git(_PLATFORM_REPO, "rev-parse", "origin/main")
    result["platform"] = {
        "repo": str(_PLATFORM_REPO),
        "origin_main": out.strip() if rc == 0 else None,
        "error": None if rc == 0 else (err.strip() or out.strip()),
        "note": (
            "read from the local origin/main ref as of this preflight run; "
            "this script does not run `git fetch` itself (no network side "
            "effect), so re-run after fetching if the remote has moved"
        ),
    }

    rc, out, err = _git(_CLI_REPO, "rev-parse", "origin/main")
    result["cli"] = {
        "repo": str(_CLI_REPO),
        "origin_main": out.strip() if rc == 0 else None,
        "error": None if rc == 0 else (err.strip() or out.strip()),
    }

    return result


def build_summary(report: dict) -> dict:
    available: list[str] = []
    missing: list[str] = []
    notes: list[str] = []

    pg = report["postgres"]
    (available if pg.get("reachable") else missing).append("postgres")

    docker = report["docker"]
    (available if docker.get("docker_ps_ok") else missing).append("docker_ps")

    sup = report["cgroup_job_supervisor_socket"]
    (available if sup.get("is_socket") else missing).append("cgroup_job_supervisor_socket")
    if not sup.get("is_socket"):
        notes.append(
            "adaptive-scheduler entry point (4) needs an integration host "
            "with the cgroup supervisor running; not available locally"
        )

    cg = report["cgroup_v2_delegation"]
    (available if cg.get("can_create_child_cgroup") else missing).append("cgroup_v2_delegation")
    if not cg.get("can_create_child_cgroup"):
        notes.append(
            "no cgroup v2 delegation in this container; cgroup-based peak-"
            "memory reads and cgroup-scoped job launches need an "
            "integration host too"
        )

    nc = report["native_companion"]
    if nc.get("present"):
        available.append("native_companion")
    else:
        missing.append("native_companion")
        notes.append(
            "decoy-engine-native is not installed in the shared .venv; "
            "every 'Rust companion' cell will show compiled_kernel_executed"
            "=False until it is built/installed for this worktree's commit"
        )

    pc = report["pinned_commits"]
    if (
        pc["engine"].get("worktree_head")
        and pc["platform"].get("origin_main")
        and pc["cli"].get("origin_main")
    ):
        available.append("pinned_commits_resolved")
    else:
        missing.append("pinned_commits_resolved")

    return {"available": available, "missing": missing, "notes": notes}


def main() -> None:
    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan": "docs/plans/2026-09-30-rust-coverage-evidence-audit.md",
        "python_executable": sys.executable,
    }
    report["docker"] = check_docker()
    report["postgres"] = check_postgres()
    report["cgroup_job_supervisor_socket"] = check_cgroup_job_supervisor_socket()
    report["cgroup_v2_delegation"] = check_cgroup_v2_delegation()
    report["native_companion"] = check_native_companion()
    report["pinned_commits"] = check_pinned_commits()
    report["summary"] = build_summary(report)

    print(json.dumps(report, indent=2, sort_keys=False))


if __name__ == "__main__":
    main()
