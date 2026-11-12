"""CI lane sentry (CI-E2): every marker and workflow has an automatic lane, or a reasoned entry.

The real tree is the negative control; each seeded case edits a deep copy of it and asserts its own
finding. Rules are described in tests/sentry/tooling/_ci_lanes.py.
"""

from __future__ import annotations

import copy
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.sentry.parallel._resources import parse_marker_expr
from tests.sentry.tooling import _ci_lanes as lanes

TREE = lanes.load()


def seeded() -> lanes.Tree:
    return copy.deepcopy(TREE)


def job(tree: lanes.Tree, wf: str, key: str) -> dict[str, Any]:
    return tree.workflows[wf]["jobs"][key]


def with_on(doc: dict[Any, Any], on: Any) -> dict[Any, Any]:
    """The workflow with its triggers replaced. YAML 1.1 parses the `on:` key as True."""
    out = {k: v for k, v in doc.items() if k not in (True, "on")}
    out[True] = on
    return out


def has(found: list[str], text: str) -> bool:
    return any(text in f for f in found)


def only_lane(
    tree: lanes.Tree, marker_job: tuple[str, str], run: str, **job_over: Any
) -> lanes.Tree:
    """Replace the steps of one job with a single run step, leaving every other lane intact."""
    j = job(tree, *marker_job)
    j["steps"] = [{"run": run}]
    j.update(job_over)
    return tree


def test_real_tree_has_no_findings() -> None:
    assert lanes.findings(TREE) == []


# --- AT-1: a marker without a lane ---------------------------------------------------------


def test_new_marker_without_lane_is_a_finding_until_registered_unlaned() -> None:
    t = seeded()
    ini = t.pyproject["tool"]["pytest"]["ini_options"]
    ini["markers"].append("slowx: x")
    ini["addopts"] = ini["addopts"].replace(
        "not benchmark", "not slowx and not benchmark"
    )  # opt-in
    assert has(lanes.marker_findings(t), "'slowx'")
    t.unlaned["marker"].append({"name": "slowx", "reason": "test"})
    assert lanes.marker_findings(t) == []


def test_removing_the_determinism_job_names_the_harness_marker() -> None:
    t = seeded()
    del t.workflows["ci.yml"]["jobs"]["determinism"]
    assert has(lanes.marker_findings(t), "'determinism_harness'")


# --- AT-2, AT-3, AT-3b: opt-in, whole-suite and profile rules ------------------------------


def test_broad_expression_is_not_a_lane_for_an_opt_in_marker() -> None:
    t = seeded()
    for key in ("pre-scan-arrow-boundary",):
        job(t, "benchmark.yml", key)["steps"] = [
            {"run": 'pytest tests/native/x.py -m "perf or not perf"'}
        ]
    t.unlaned["marker"] = [e for e in t.unlaned["marker"] if e["name"] != "benchmark"]
    t.workflows["benchmark.yml"] = with_on(
        t.workflows["benchmark.yml"], ["push"]
    )  # make the job automatic
    assert has(lanes.marker_findings(t), "'benchmark'")


def test_narrow_path_job_is_not_the_lane_for_a_default_marker() -> None:
    t = seeded()
    only_lane(t, ("ci.yml", "regression-gate"), "pytest tests/native/x.py")
    assert has(lanes.marker_findings(t), "'golden'")


@pytest.mark.parametrize(
    ("gate", "marker"),
    [("ml-gate", "ml"), ("ner-gate", "ner"), ("dp-certified-gate", "dp_certified")],
)
def test_profile_marker_needs_its_dedicated_lane(gate: str, marker: str) -> None:
    t = seeded()
    del t.workflows["ci.yml"]["jobs"][gate]
    assert has(lanes.marker_findings(t), f"'{marker}'")  # the whole-suite run still selects it


def test_dedicated_entry_for_an_unregistered_marker_is_a_finding() -> None:
    t = seeded()
    t.required["dedicated_marker"].append({"name": "nope", "reason": "x"})
    assert has(lanes.marker_findings(t), "M6")


# --- AT-4, AT-5: spellings and fail-closed expressions -------------------------------------


def lane_for(run: str) -> list[lanes.Lane]:
    t = seeded()
    only_lane(t, ("ci.yml", "determinism"), run)
    return [ln for ln in lanes.lanes(t) if ln.job == "determinism"]


@pytest.mark.parametrize(
    "run",
    [
        "pytest -m determinism_harness",
        "pytest -mdeterminism_harness",
        "python -m pytest -m determinism_harness",
        "uv run --frozen pytest -m determinism_harness",
        'bash -c "pytest -m determinism_harness"',
        "pytest -m foo -m determinism_harness",
        "pytest tests/x.py \\\n  -m determinism_harness \\\n  --tb=short",
        "FOO=1 pytest -m determinism_harness",
    ],
)
def test_spellings_are_read_as_the_lane(run: str) -> None:
    (lane,) = lane_for(run)
    assert lane.expr == "determinism_harness" and not lane.bad


def test_python_dash_m_is_not_the_marker_option() -> None:
    (lane,) = lane_for("python -m pytest tests")
    assert lane.expr == lanes.addopts_expr(TREE.pyproject)


def test_addopts_override_selects_everything() -> None:
    (lane,) = lane_for("pytest -o addopts= tests")
    assert lane.expr is None and lane.whole_suite and not lane.bad


@pytest.mark.parametrize("expr", ['"foo("', '"mark(x=1)"', '"a + b"', '""', '"not -a"', '"True"'])
def test_unreadable_expressions_fail_closed(expr: str) -> None:
    t = seeded()
    only_lane(t, ("ci.yml", "determinism"), f"pytest -m {expr}")
    found = lanes.marker_findings(t)
    assert has(found, "M4 ci.yml:determinism")
    assert has(found, "'determinism_harness'")  # and it did not count as the lane


# --- AT-6: job events ----------------------------------------------------------------------


def test_not_pr_job_in_a_dispatch_only_workflow_is_no_lane() -> None:
    t = seeded()
    t.workflows["ci.yml"] = with_on(t.workflows["ci.yml"], {"workflow_dispatch": None})
    only_lane(
        t, ("ci.yml", "determinism"), "pytest -m determinism_harness", **{"if": lanes.NOT_PR_IF}
    )
    assert has(lanes.marker_findings(t), "'determinism_harness'")


def test_dispatch_guarded_job_in_a_scheduled_workflow_is_no_lane() -> None:
    t = seeded()
    only_lane(
        t, ("ci.yml", "determinism"), "pytest -m determinism_harness",
        **{"if": "github.event_name == 'workflow_dispatch'"},
    )  # fmt: skip
    assert has(lanes.marker_findings(t), "'determinism_harness'")


# --- AT-7: triggers and the unlaned registry -----------------------------------------------


def test_dispatch_only_workflow_needs_an_unlaned_entry() -> None:
    t = seeded()
    t.unlaned["workflow"] = [e for e in t.unlaned["workflow"] if e["file"] != "codspeed.yml"]
    assert has(lanes.trigger_findings(t), "T1 codspeed.yml")


def test_stale_unlaned_entries_are_findings() -> None:
    t = seeded()
    t.unlaned["workflow"] += [
        {"file": "ci.yml", "reason": "x"},
        {"file": "gone.yml", "reason": "x"},
    ]
    t.unlaned["marker"] += [{"name": "ml", "reason": "x"}, {"name": "ghost", "reason": "x"}]
    found = lanes.findings(t)
    for text in ("T1 ci.yml", "'gone.yml'", "marker 'ml' has a lane", "'ghost' is not registered"):
        assert has(found, text), text


def test_registry_schema_is_closed() -> None:
    cases: list[tuple[str, Any]] = []
    t = seeded()
    t.unlaned["marker"][0]["extra"] = 1
    cases.append(("keys", t))
    t = seeded()
    t.unlaned["marker"][0]["reason"] = " "
    cases.append(("non-empty", t))
    t = seeded()
    t.unlaned["marker"].append(dict(t.unlaned["marker"][0]))
    cases.append(("duplicate", t))
    t = seeded()
    t.unlaned["bogus"] = []
    cases.append(("unknown table", t))
    t = seeded()
    t.required["merge_exempt"][0].pop("reason")
    cases.append(("keys", t))
    for text, tree in cases:
        assert has(lanes.registry_findings(tree), "R1"), text
        assert has(lanes.registry_findings(tree), text), text


# --- AT-8: path filters --------------------------------------------------------------------


def test_path_filters_are_findings() -> None:
    t = seeded()
    t.workflows["ci.yml"] = with_on(
        t.workflows["ci.yml"],
        {
            "pull_request": {"paths": ["src/**"]},
            "push": {"paths-ignore": ["docs/**"]},
        },
    )
    found = [f for f in lanes.trigger_findings(t) if f.startswith("T2")]
    assert len(found) == 2


# --- AT-9 and the shared trigger decoder ---------------------------------------------------


@pytest.mark.parametrize(
    "on",
    [
        "on: pull_request",
        "on: [pull_request, push]",
        "on:\n  pull_request:\n    types: [opened]\n  push:",
    ],
)
def test_workflow_events_reads_every_form(on: str) -> None:
    from tests.sentry import test_ci_config

    doc = yaml.safe_load(f"{on}\njobs:\n  g:\n    runs-on: self-hosted\n    steps: []\n")
    events = lanes.workflow_events(doc)
    assert "pull_request" in events
    found = test_ci_config.selfhosted_violations(doc)
    assert any("pull_request" in f for f in found)


def test_scheduled_selfhosted_job_is_a_finding() -> None:
    from tests.sentry import test_ci_config

    doc = yaml.safe_load(
        "on: schedule\njobs:\n  g:\n    runs-on: [self-hosted, x]\n    steps: []\n"
    )
    assert any("['schedule']" in f for f in test_ci_config.selfhosted_violations(doc))


# --- AT-10: required list ------------------------------------------------------------------


def _r(t: lanes.Tree) -> list[str]:
    return lanes.registry_findings(t)


def test_required_list_rules() -> None:
    t = seeded()
    job(t, "ci.yml", "determinism")["if"] = "always()"
    assert has(_r(t), "R2 ci.yml:determinism: merge job has an `if`")
    t = seeded()
    t.workflows["docs.yml"] = with_on(t.workflows["docs.yml"], ["workflow_dispatch"])
    assert has(_r(t), "no pull_request trigger")
    t = seeded()
    job(t, "ci.yml", "ml-gate")["runs-on"] = ["self-hosted", "x"]
    assert has(_r(t), "is self-hosted")
    t = seeded()
    job(t, "ci.yml", "ml-gate")["needs"] = "ruff"
    assert has(_r(t), "outside the merge tier")
    t = seeded()
    job(t, "ci.yml", "ml-gate")["name"] = "ml ${{ matrix.x }}"
    assert has(_r(t), "templated name")
    t = seeded()
    t.required["merge"][0]["jobs"].append("missing")
    assert has(_r(t), "R2 ci.yml:missing: merge job does not exist")
    t = seeded()
    t.workflows["ci.yml"]["jobs"]["extra"] = {
        "runs-on": "ubuntu-latest",
        "timeout-minutes": 1,
        "steps": [],
    }
    assert has(_r(t), "R3 ci.yml:extra")
    t = seeded()
    t.workflows["codspeed.yml"] = with_on(
        t.workflows["codspeed.yml"], {"pull_request_target": None}
    )
    assert has(_r(t), "R3 codspeed.yml:benchmarks")


def test_push_only_job_cannot_reuse_a_required_check_name() -> None:
    t = seeded()
    t.workflows["gate-selfhosted.yml"]["jobs"]["extra"] = {
        "name": "docs-build",
        "runs-on": "ubuntu-latest",
        "timeout-minutes": 1,
    }
    found = _r(t)
    assert has(found, "R2 check name 'docs-build' is reported by")
    assert has(found, "docs.yml:build") and has(found, "gate-selfhosted.yml:extra")


def test_merge_job_with_an_unsupported_matrix_is_a_finding() -> None:
    t = seeded()
    job(t, "native-companion.yml", "companion-wheel-macos")["strategy"]["matrix"]["exclude"] = [
        {"python-version": "3.10"}
    ]
    assert has(_r(t), "R2 native-companion.yml:companion-wheel-macos: matrix exclude")


def test_job_in_a_schedule_only_workflow_needs_no_listing() -> None:
    t = seeded()
    t.workflows["security.yml"]["jobs"]["extra"] = {
        "runs-on": "ubuntu-latest",
        "timeout-minutes": 1,
    }
    assert not has(_r(t), "security.yml:extra")


# --- AT-11: concurrency and timeouts -------------------------------------------------------


def test_release_workflow_may_keep_cancel_in_progress_false() -> None:
    t = seeded()
    conc = t.workflows["native-companion-release.yml"]["concurrency"]
    assert conc["cancel-in-progress"] is False and lanes.trigger_findings(t) == []


def test_concurrency_and_timeout_rules() -> None:
    t = seeded()
    del t.workflows["ci.yml"]["concurrency"]
    assert has(lanes.trigger_findings(t), "T4 ci.yml: pull_request workflow lacks")
    t = seeded()
    t.workflows["docs.yml"]["concurrency"] = {"group": "pages", "cancel-in-progress": False}
    assert has(lanes.trigger_findings(t), "cancel-in-progress is false")
    t = seeded()
    del job(t, "native-companion.yml", "companion-rust-lint")["timeout-minutes"]
    assert has(lanes.trigger_findings(t), "T3 native-companion.yml:companion-rust-lint")


# --- AT-12: gates.sh agrees with the hosted full suite -------------------------------------


def test_gates_expression_must_equal_the_hosted_one() -> None:
    t = seeded()
    t = replace(t, gates=re.sub(r"(PYTEST_ARGS=\(-m '[^']*)", r"\1 and not perf", t.gates, count=1))
    assert has(lanes.marker_findings(t), "M5")
    t = replace(seeded(), gates=TREE.gates.replace("PYTEST_ARGS=(-m '", "PYTEST_ARGS=(-m x '", 1))
    assert has(lanes.marker_findings(t), "M5")
    t = replace(
        seeded(), gates=re.sub(r"^(PYTEST_ARGS=\(.*\))$", r"\1 junk", TREE.gates, flags=re.M)
    )
    assert has(lanes.marker_findings(t), "M5 gates.sh has no single")


def test_gates_excludes_the_harness_that_its_own_lane_owns() -> None:
    expr = parse_marker_expr(TREE.gates)
    assert not lanes.selects(expr, frozenset({"determinism_harness"}))
    assert "determinism" in TREE.required["merge"][0]["jobs"]


# --- the determinism job collects every file that carries its marker ------------------------

# Collecting the whole tree counts module-level skips of unrelated files, and the job's zero-skip
# guard then fails. The job therefore names the owning module; this keeps that list complete.
_MARK_SITE = re.compile(r"mark\.determinism" + r"_harness\b")


def marker_sites(root: Path) -> list[str]:
    return sorted(
        str(p.relative_to(root))
        for p in (root / "tests").rglob("*.py")
        if _MARK_SITE.search(p.read_text())
    )


def outside_scope(sites: list[str], paths: tuple[str, ...]) -> list[str]:
    return [s for s in sites if not any(s == p or s.startswith(p + "/") for p in paths)]


def determinism_paths(tree: lanes.Tree) -> tuple[str, ...]:
    (lane,) = [ln for ln in lanes.lanes(tree) if ln.job == "determinism"]
    return lane.paths


def test_determinism_job_collects_every_marked_file() -> None:
    sites = marker_sites(lanes.ROOT)
    assert sites, "no file carries the marker, so the scope check would pass vacuously"
    assert outside_scope(sites, determinism_paths(TREE)) == []


def test_determinism_job_is_scoped_not_whole_tree() -> None:
    assert determinism_paths(TREE) and "tests" not in determinism_paths(TREE)


def test_marker_site_outside_the_job_scope_is_caught() -> None:
    sites = [*marker_sites(lanes.ROOT), "tests/physical/test_new_harness.py"]
    assert outside_scope(sites, determinism_paths(TREE)) == ["tests/physical/test_new_harness.py"]


def test_scope_that_drops_the_owning_module_is_caught() -> None:
    t = seeded()
    only_lane(t, ("ci.yml", "determinism"), "pytest tests/unit -m determinism_harness")
    assert outside_scope(marker_sites(lanes.ROOT), determinism_paths(t)) != []
