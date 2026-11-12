"""AT-20 to AT-22: check-name derivation and the required-check verifier, offline on fakes.

GitHub and git are injected (`fetch`, `git`), so no test calls the network. The "main" tree the fake
git serves is the real `ci/` registry and workflows, which is also the AT-20 fixture source.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from scripts.dev import required_checks as rc

ROOT = Path(__file__).resolve().parents[3]
REPO = "louiskeep/decoy-engine"
MAIN = "a" * 40
HEAD = "b" * 40
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)

# The 16 names GitHub showed for native-companion.yml in run 42170946535.
NATIVE_NAMES = [
    "companion-wheel-linux (3.10, x86_64)",
    "companion-wheel-linux (3.10, aarch64)",
    "companion-wheel-linux (3.11, x86_64)",
    "companion-wheel-linux (3.11, aarch64)",
    "companion-wheel-linux (3.12, x86_64)",
    "companion-wheel-linux (3.12, aarch64)",
    "companion-wheel-windows (3.10)",
    "companion-wheel-windows (3.11)",
    "companion-wheel-windows (3.12)",
    "companion-wheel-macos (3.10)",
    "companion-wheel-macos (3.11)",
    "companion-wheel-macos (3.12)",
    "companion-rust-lint",
    "core-companion-present",
    "companion-fuzz-sanitizer",
    "core-companion-absent",
]
CI_NAMES = [
    "packaging-gate",
    "ml-gate",
    "base-install",
    "ner-gate",
    "dp-certified-gate",
    "determinism",
]
MERGE = {
    "ci.yml": CI_NAMES,
    "native-companion.yml": NATIVE_NAMES,
    "docs.yml": ["docs-build"],
    "compat-preflight.yml": ["compat-preflight"],
    "changelog-fragment.yml": ["changelog-fragment"],
}


def stamp(delta_hours: float = 1) -> str:
    return (NOW - timedelta(hours=delta_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGit:
    """Serves ci/ and the workflows from `root` as the main tree; records every call."""

    def __init__(self, root: Path = ROOT, local_main: str = MAIN) -> None:
        self.root, self.local_main, self.calls = root, local_main, []

    def __call__(self, args: Any) -> str:
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["fetch", "origin"]:
            return ""
        if args[0] == "rev-parse":
            return self.local_main + "\n"
        if args[0] == "ls-tree":
            files = sorted((self.root / ".github/workflows").glob("*.yml"))
            return "\n".join(f".github/workflows/{p.name}" for p in files)
        assert args[0] == "show"
        sha, _, path = args[1].partition(":")
        assert sha == MAIN, f"read at {sha}, not the main sha"
        return (ROOT / path).read_text()


class FakeGitHub:
    """Runs by workflow file, jobs by run id. `runs` maps workflow -> list of run dicts."""

    def __init__(self) -> None:
        self.runs: dict[str, list[dict[str, Any]]] = {}
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.main = MAIN
        self.error: Exception | None = None
        self.job_pages: list[int] = []
        self._next = 1

    def add(
        self,
        wf: str,
        names: list[str] | None = None,
        *,
        event: str = "pull_request",
        sha: str = HEAD,
        attempt: int = 1,
        created: str | None = None,
        conclusions: dict[str, str | None] | None = None,
        status: str = "completed",
        run_conclusion: str | None = "success",
    ) -> int:
        rid, self._next = self._next, self._next + 1
        self.runs.setdefault(wf, []).append(
            {
                "id": rid,
                "event": event,
                "head_sha": sha,
                "run_attempt": attempt,
                "created_at": created or stamp(),
                "status": status,
                "conclusion": run_conclusion,
            }
        )
        names = MERGE[wf] if names is None else names
        over = conclusions or {}
        self.jobs[rid] = [{"name": n, "conclusion": over.get(n, "success")} for n in names]
        return rid

    def __call__(self, path: str) -> Any:
        if self.error:
            raise self.error
        if path == f"repos/{REPO}/branches/main":
            return {"commit": {"sha": self.main}}
        for wf, runs in self.runs.items():
            if path.startswith(f"repos/{REPO}/actions/workflows/{wf}/runs?"):
                return {"workflow_runs": runs}
        for rid, jobs in self.jobs.items():
            if path.startswith(f"repos/{REPO}/actions/runs/{rid}/jobs"):
                qs = parse_qs(urlparse(path).query)
                page, size = int(qs.get("page", ["1"])[0]), int(qs["per_page"][0])
                self.job_pages.append(page)
                for i, job in enumerate(jobs):
                    job.setdefault("id", rid * 10_000 + i)
                return {"total_count": len(jobs), "jobs": jobs[(page - 1) * size : page * size]}
        if "/actions/workflows/" in path and path.split("?")[0].endswith("/runs"):
            return {"workflow_runs": []}
        raise AssertionError(f"unexpected API path {path}")


def green_pr() -> FakeGitHub:
    gh = FakeGitHub()
    for wf in MERGE:
        gh.add(wf)
    return gh


def run_main(argv: list[str], gh: FakeGitHub, git: FakeGit | None = None) -> int:
    return rc.main(argv, fetch=gh, git=git or FakeGit(), now=NOW)


def verify(gh: FakeGitHub, *extra: str, git: FakeGit | None = None) -> int:
    return run_main(["verify", "--sha", HEAD, "--tier", "merge", *extra], gh, git)


# --- AT-20: derivation ----------------------------------------------------------------------


def workflow(name: str) -> dict[Any, Any]:
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def names_of(doc: dict[Any, Any]) -> list[str]:
    return [n for k, j in doc["jobs"].items() for n in rc.job_check_names(k, j)]


def test_native_companion_yields_the_sixteen_names_github_showed() -> None:
    assert sorted(names_of(workflow("native-companion.yml"))) == sorted(NATIVE_NAMES)


def test_ci_yml_merge_jobs_are_named_by_key() -> None:
    doc = workflow("ci.yml")
    for key in CI_NAMES:
        assert rc.job_check_names(key, doc["jobs"][key]) == [key]


def test_literal_name_beats_the_key() -> None:
    assert rc.job_check_names("build", {"name": "docs-build"}) == ["docs-build"]


def matrix_job(**strategy: Any) -> dict[str, Any]:
    return {"strategy": {"matrix": strategy}}


@pytest.mark.parametrize(
    "job, why",
    [
        (matrix_job(v=["1", "2"], exclude=[{"v": "1"}]), "exclude"),
        (matrix_job(v=["1", "2"], include=[{"v": "3"}]), "adds a leg"),
        (matrix_job(include=[{"v": "3"}]), "adds a leg"),
        ({"name": "x ${{ matrix.v }}", "strategy": {"matrix": {"v": ["1"]}}}, "templated"),
        (matrix_job(v="${{ fromJSON(needs.a.outputs.v) }}"), "list"),
    ],
)
def test_unsupported_shapes_name_the_job(job: dict[str, Any], why: str) -> None:
    with pytest.raises(rc.DerivationError) as err:
        rc.job_check_names("leg", job)
    assert "leg" in str(err.value) and why in str(err.value)


def test_include_that_only_adds_keys_keeps_the_names() -> None:
    job = matrix_job(v=["1", "2"], include=[{"v": "2", "extra": "x"}])
    assert rc.job_check_names("j", job) == ["j (1)", "j (2)"]


def test_derivation_error_exits_two_with_the_job(capsys: pytest.CaptureFixture[str]) -> None:
    class Git(FakeGit):
        def __call__(self, args: Any) -> str:
            argv = list(args)
            if argv[0] == "show" and argv[1].endswith("native-companion.yml"):
                doc = workflow("native-companion.yml")
                doc["jobs"]["companion-wheel-macos"]["strategy"]["matrix"]["exclude"] = [{}]
                return yaml.safe_dump(doc)
            return super().__call__(args)

    assert run_main(["contexts", "--ref", MAIN], FakeGitHub(), Git()) == 2
    assert "companion-wheel-macos" in capsys.readouterr().err


def test_contexts_prints_the_merge_tier_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_main(["contexts", "--ref", MAIN], FakeGitHub()) == 0
    out = capsys.readouterr().out
    got = json.loads(out)
    assert sorted(got) == sorted(n for names in MERGE.values() for n in names)
    assert len(got) == len(set(got)) == 25


# --- AT-21: verify --tier merge -------------------------------------------------------------


def test_all_green_exits_zero_and_prints_the_main_sha(capsys: pytest.CaptureFixture[str]) -> None:
    assert verify(green_pr()) == 0
    assert MAIN in capsys.readouterr().out


def drop(gh: FakeGitHub, wf: str, name: str) -> FakeGitHub:
    for rid in {r["id"] for r in gh.runs[wf]}:
        gh.jobs[rid] = [j for j in gh.jobs[rid] if j["name"] != name]
    return gh


def red(wf: str, name: str, conclusion: str | None) -> FakeGitHub:
    gh = FakeGitHub()
    for w in MERGE:
        gh.add(w, conclusions={name: conclusion} if w == wf else None)
    return gh


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "cancelled", "failure", None])
def test_a_non_success_conclusion_is_red(conclusion: str | None) -> None:
    assert verify(red("ci.yml", "ner-gate", conclusion)) == 1


def test_a_missing_name_is_red() -> None:
    assert verify(drop(green_pr(), "ci.yml", "ner-gate")) == 1


def test_a_missing_matrix_leg_is_red() -> None:
    assert (
        verify(drop(green_pr(), "native-companion.yml", "companion-wheel-linux (3.11, aarch64)"))
        == 1
    )


def test_a_missing_workflow_run_is_red() -> None:
    gh = green_pr()
    del gh.runs["docs.yml"]
    assert verify(gh) == 1


def test_run_for_another_head_is_red() -> None:
    gh = green_pr()
    gh.runs["ci.yml"][0]["head_sha"] = "c" * 40
    assert verify(gh) == 1


def test_push_event_run_is_red() -> None:
    gh = green_pr()
    gh.runs["ci.yml"][0]["event"] = "push"
    assert verify(gh) == 1


def test_newest_run_decides() -> None:
    gh = green_pr()
    gh.add("ci.yml", created=stamp(0.5), conclusions={"ner-gate": "failure"})
    assert verify(gh) == 1
    gh2 = green_pr()
    gh2.add("ci.yml", created=stamp(5), conclusions={"ner-gate": "failure"})
    assert verify(gh2) == 0


def test_second_attempt_is_red_unless_allowed(capsys: pytest.CaptureFixture[str]) -> None:
    gh = green_pr()
    gh.runs["ci.yml"][0]["run_attempt"] = 2
    assert verify(gh) == 1
    assert verify(gh, "--allow-rerun", "ci.yml=download") == 0
    assert "download" in capsys.readouterr().out
    assert verify(gh, "--allow-rerun", "docs.yml=download") == 1


@pytest.mark.parametrize("arg", ["ci.yml", "ci.yml=", "=why"])
def test_malformed_allow_rerun_is_a_usage_error(arg: str) -> None:
    assert verify(green_pr(), "--allow-rerun", arg) == 2


def test_api_error_is_never_a_pass() -> None:
    gh = green_pr()
    gh.error = OSError("boom")
    assert verify(gh) == 2


def test_unexpected_response_shape_exits_two() -> None:
    gh = green_pr()
    gh.jobs[1] = [{"conclusion": "success"}]  # no name
    assert verify(gh) == 2


def test_list_comes_from_the_main_sha_not_the_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ci").mkdir()
    text = (ROOT / "ci/required-jobs.toml").read_text()
    (tmp_path / "ci/required-jobs.toml").write_text(text.replace('"ner-gate", ', ""))
    monkeypatch.chdir(tmp_path)
    assert verify(drop(green_pr(), "ci.yml", "ner-gate")) == 1


def test_stale_origin_main_exits_two_and_prints_the_api_sha(
    capsys: pytest.CaptureFixture[str],
) -> None:
    git = FakeGit(local_main="d" * 40)
    assert verify(green_pr(), git=git) == 2
    cap = capsys.readouterr()
    assert "stale origin/main" in cap.err and MAIN in cap.out + cap.err
    assert ["fetch", "origin", "main"] in git.calls


# --- AT-22: verify --tier nightly -----------------------------------------------------------

SCHEDULED = [
    "ci.yml",
    "native-companion.yml",
    "testflight.yml",
    "dps-dependency-matrix.yml",
    "security.yml",
]


def scheduled_names(wf: str) -> list[str]:
    return names_of(workflow(wf))


def nightly(**over: Any) -> FakeGitHub:
    gh = FakeGitHub()
    for wf in SCHEDULED:
        gh.add(wf, scheduled_names(wf), **{"event": "schedule", "sha": MAIN, **over})
    return gh


def run_nightly(gh: FakeGitHub) -> int:
    return run_main(["verify", "--tier", "nightly"], gh)


def test_fresh_green_schedule_runs_exit_zero() -> None:
    assert run_nightly(nightly()) == 0


def test_schedule_run_on_an_older_main_commit_is_no_fresh_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_nightly(nightly(sha="e" * 40)) == 1
    assert "no fresh run" in capsys.readouterr().out


def test_schedule_run_older_than_26_hours_is_stale() -> None:
    assert run_nightly(nightly(created=stamp(26.5))) == 1
    assert run_nightly(nightly(created=stamp(25.5))) == 0


def test_one_red_job_is_red() -> None:
    gh = nightly()
    gh.jobs[gh.runs["security.yml"][0]["id"]][0]["conclusion"] = "failure"
    assert run_nightly(gh) == 1


def test_a_workflow_with_no_schedule_run_is_no_fresh_run() -> None:
    gh = nightly()
    gh.runs["testflight.yml"][0]["event"] = "workflow_dispatch"
    assert run_nightly(gh) == 1


def test_nightly_api_error_exits_two() -> None:
    gh = nightly()
    gh.error = OSError("boom")
    assert run_nightly(gh) == 2


# --- fail-closed nightly verification -------------------------------------------------------


def test_an_empty_job_list_is_red(capsys: pytest.CaptureFixture[str]) -> None:
    gh = nightly()
    gh.jobs[gh.runs["security.yml"][0]["id"]] = []
    assert run_nightly(gh) == 1
    assert "no jobs" in capsys.readouterr().out


def test_a_missing_expected_job_is_red(capsys: pytest.CaptureFixture[str]) -> None:
    gh = nightly()
    del gh.jobs[gh.runs["security.yml"][0]["id"]][-1]
    assert run_nightly(gh) == 1
    assert "is missing" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "conclusion"), [("in_progress", None), ("completed", "cancelled")]
)
def test_a_run_that_is_not_completed_success_is_red(status: str, conclusion: str | None) -> None:
    gh = nightly()
    run = gh.runs["security.yml"][0]
    run["status"], run["conclusion"] = status, conclusion
    assert run_nightly(gh) == 1


def test_a_failed_job_on_page_two_is_red() -> None:
    gh = nightly()
    jobs = gh.jobs[gh.runs["security.yml"][0]["id"]]
    jobs += [{"name": f"extra-{i}", "conclusion": "success"} for i in range(99)]
    jobs.append({"name": "late", "conclusion": "failure"})
    assert len(jobs) > 100
    assert run_nightly(gh) == 1
    assert 2 in gh.job_pages


def test_many_green_jobs_across_pages_pass() -> None:
    gh = nightly()
    jobs = gh.jobs[gh.runs["security.yml"][0]["id"]]
    jobs += [{"name": f"extra-{i}", "conclusion": "success"} for i in range(150)]
    assert run_nightly(gh) == 0
    assert 2 in gh.job_pages


def test_a_short_job_read_is_an_error_not_a_pass() -> None:
    class Short(FakeGitHub):
        def __call__(self, path: str) -> Any:
            out = super().__call__(path)
            if "/jobs" in path:
                out["total_count"] += 1
            return out

    gh = Short()
    for wf in SCHEDULED:
        gh.add(wf, scheduled_names(wf), event="schedule", sha=MAIN)
    assert run_nightly(gh) == 2


def _nightly_with(fake: type[FakeGitHub], extra: int = 0) -> FakeGitHub:
    gh = fake()
    for wf in SCHEDULED:
        gh.add(wf, scheduled_names(wf), event="schedule", sha=MAIN)
    for jobs in gh.jobs.values():
        jobs += [{"name": f"extra-{i}", "conclusion": "success"} for i in range(extra)]
    return gh


def test_a_low_job_count_is_an_error_not_a_pass() -> None:
    class Low(FakeGitHub):
        def __call__(self, path: str) -> Any:
            out = super().__call__(path)
            if "/jobs" in path:
                out["total_count"] -= 1
            return out

    assert run_nightly(_nightly_with(Low)) == 2


@pytest.mark.parametrize("claimed", [150, 200])
def test_a_repeated_job_page_is_an_error_not_a_pass(claimed: int) -> None:
    asked: list[str] = []

    class Repeat(FakeGitHub):
        def __call__(self, path: str) -> Any:
            out = super().__call__(re.sub(r"page=\d+$", "page=1", path))
            if "/jobs" in path:
                asked.append(path.rsplit("=", 1)[1])
                out["total_count"] = claimed
            return out

    assert run_nightly(_nightly_with(Repeat, extra=150)) == 2
    assert "2" in asked


def test_a_job_count_that_moves_between_pages_is_an_error() -> None:
    class Moving(FakeGitHub):
        def __call__(self, path: str) -> Any:
            out = super().__call__(path)
            if "/jobs" in path and path.endswith("page=2"):
                out["total_count"] += 100
            return out

    assert run_nightly(_nightly_with(Moving, extra=150)) == 2


def test_a_job_without_an_id_is_an_error() -> None:
    class NoId(FakeGitHub):
        def __call__(self, path: str) -> Any:
            out = super().__call__(path)
            if "/jobs" in path:
                # One job, so the distinct-id count alone cannot catch the missing id.
                first = out["jobs"][:1]
                out = {"total_count": 1, "jobs": [{k: v for k, v in first[0].items() if k != "id"}]}
            return out

    assert run_nightly(_nightly_with(NoId)) == 2


def test_a_skipped_scheduled_job_is_not_green() -> None:
    gh = nightly()
    gh.jobs[gh.runs["security.yml"][0]["id"]][0]["conclusion"] = "skipped"
    assert run_nightly(gh) == 1


def test_underivable_scheduled_workflow_is_a_problem_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Git(FakeGit):
        def __call__(self, args: Any) -> str:
            argv = list(args)
            if argv[0] == "show" and argv[1].endswith("security.yml"):
                doc = workflow("security.yml")
                doc["jobs"]["pip-audit"]["name"] = "${{ matrix.x }}"
                return yaml.safe_dump(doc)
            return super().__call__(args)

    assert run_main(["verify", "--tier", "nightly"], nightly(), Git()) == 1
    assert "cannot derive" in capsys.readouterr().out


def test_derivation_error_keeps_no_context() -> None:
    doc = workflow("native-companion.yml")
    doc["jobs"]["companion-wheel-macos"]["strategy"]["matrix"]["exclude"] = [{}]
    required = {"merge": [{"workflow": "n.yml", "jobs": ["companion-wheel-macos"]}]}
    with pytest.raises(rc.DerivationError) as info:
        rc.merge_contexts(required, {"n.yml": doc})
    assert info.value.__context__ is None
