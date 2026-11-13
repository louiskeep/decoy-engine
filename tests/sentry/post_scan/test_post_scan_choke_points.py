"""The post-scan choke points: one blocking decision, one pass-through rule, one key core, one
reason-identity serializer and one applicability predicate.

Each is a shrink-only ratchet over `_scan.py` site keys (`tests/sentry/baselines/post-scan-*`).
A new site fails, and a removed site must leave its baseline. The seeded cells plant the breach
in the real module text, so a scanner that stops seeing it turns the cell red.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.sentry.post_scan import _scan as scan

SENTRIES: dict[str, Callable[[], list[str]]] = {
    "post-scan-blocking": scan.blocking_sites,
    "post-scan-pass-through": scan.pass_through_sites,
    "post-scan-key-core": scan.key_core_sites,
    "post-scan-reason-identity": scan.serializer_sites,
    "post-scan-gate-applies": scan.applies_sites,
    "post-scan-commit": scan.commit_sites,
    "post-scan-model-predicate": scan.model_sites,
    "post-scan-readability": scan.readability_sites,
}


def _baseline(sentry_id: str) -> set[str]:
    data = json.loads((scan.BASELINES / f"{sentry_id}.json").read_text(encoding="utf-8"))
    assert data["sentry_id"] == sentry_id
    return set(data["entries"])


@pytest.mark.parametrize("sentry_id", SENTRIES)
def test_no_new_site_beyond_the_baseline(sentry_id: str) -> None:
    new = sorted(set(SENTRIES[sentry_id]()) - _baseline(sentry_id))
    assert not new, f"{sentry_id}: new breach of the choke point: {new}"


@pytest.mark.parametrize("sentry_id", SENTRIES)
def test_the_baseline_lists_only_sites_that_still_exist(sentry_id: str) -> None:
    stale = sorted(_baseline(sentry_id) - set(SENTRIES[sentry_id]()))
    assert not stale, f"{sentry_id}: sites were fixed; delete these baseline keys: {stale}"


def _real(rel: str) -> str:
    return (scan.SRC / rel).read_text(encoding="utf-8")


def _keys(label: scan.Label, source: str, rel: str) -> list[str]:
    return scan.sites(source, f"decoy_engine/{rel}", label)


# ----- one blocking decision ----------------------------------------------------------------


def test_raising_the_blocked_error_from_the_key_module_is_flagged() -> None:
    planted = _real("verification/postmask/_keys.py") + (
        "\n\ndef _planted(report):\n    raise PostScanBlockedError(report)\n"
    )
    found = _keys(scan.blocking_label, planted, "verification/postmask/_keys.py")
    assert any("::construct::" in k for k in found)


@pytest.mark.parametrize(
    "text", ['sev == "fail"', 'f.severity in ("fail", "error")', 'x["severity"] != "error"']
)
def test_comparing_a_severity_with_an_outcome_outside_the_verdict_is_flagged(text: str) -> None:
    found = _keys(scan.blocking_label, f"def f(sev, f, x):\n    return {text}\n", "x.py")
    assert found == ["decoy_engine/x.py::f::severity-compare::0"]


@pytest.mark.parametrize(
    "text", ['sev == "info"', 'status == "error"', 'f.severity in ("info", "warning")']
)
def test_other_comparisons_are_not_flagged(text: str) -> None:
    assert _keys(scan.blocking_label, f"def f(sev, f, status):\n    return {text}\n", "x.py") == []


# ----- one pass-through rule ----------------------------------------------------------------

GUARDED = (
    "def f():\n    try:\n        g()\n    except Exception as exc:\n"
    "        if passes_through(exc):\n            raise\n        return 1\n"
)


def test_a_guarded_broad_handler_is_clean() -> None:
    assert _keys(scan.pass_through_label, GUARDED, "x.py") == []


@pytest.mark.parametrize(
    "source",
    [
        GUARDED.replace("        if passes_through(exc):\n            raise\n", ""),
        GUARDED.replace("Exception as exc", "BaseException as exc").replace(
            "        if passes_through(exc):\n            raise\n", ""
        ),
        GUARDED.replace("passes_through(exc)", "passes_through(other)"),
        GUARDED.replace("Exception as exc", "Exception"),
        "def f():\n    try:\n        g()\n    except:\n        return 1\n",
    ],
)
def test_a_broad_handler_without_the_guard_is_flagged(source: str) -> None:
    assert len(_keys(scan.pass_through_label, source, "x.py")) == 1


def test_a_handler_that_catches_a_decoy_error_is_flagged() -> None:
    src = "def f():\n    try:\n        g()\n    except DecoyError:\n        return 1\n"
    assert _keys(scan.pass_through_label, src, "x.py") == ["decoy_engine/x.py::f::decoy-handler::0"]


def test_dropping_the_guard_from_the_source_side_handler_is_flagged() -> None:
    real = _real("verification/postmask/_keys.py")
    guard = "            if passes_through(exc):\n                raise\n"
    assert real.count(guard) >= 2
    planted = real.replace(guard, "", 1)
    rel = "verification/postmask/_keys.py"
    assert len(_keys(scan.pass_through_label, planted, rel)) == 1


# ----- one key core -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ("import duckdb\n", "duckdb-import"),
        ("from duckdb import connect\n", "duckdb-import"),
        (
            "from decoy_engine.execution.out_of_core._duckdb import connect_duckdb\n",
            "duckdb-import",
        ),
        ("def f(a):\n    return _comparable(a)\n", "comparable-call"),
        ("def f(own, parent):\n    return own.schema != parent.schema\n", "type-compare"),
        ("def f(a, b):\n    return a.type == b.type\n", "type-compare"),
    ],
)
def test_a_key_core_breach_is_flagged(source: str, label: str) -> None:
    found = _keys(scan.key_core_label, source, "verification/postmask/_keys.py")
    assert [k.split("::")[2] for k in found] == [label]


def test_planting_the_breaches_in_the_real_key_module_is_flagged() -> None:
    rel = "verification/postmask/_keys.py"
    real = _real(rel)
    before = len(_keys(scan.key_core_label, real, rel))
    for planted in (
        "import duckdb\n" + real,
        real + "\n\ndef _p(own, parent):\n    return own.schema != parent.schema\n",
    ):
        assert len(_keys(scan.key_core_label, planted, rel)) == before + 1


# ----- one reason-identity serializer -------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ('def f(r):\n    return {"namespace": r.a, "code": r.b}\n', "dict-literal"),
        ("def f(r):\n    return dict(namespace=r.a, code=r.b)\n", "dict-call"),
        ("def f(c):\n    return asdict(c.spec.ref)\n", "asdict-ref"),
        ("def f(ref):\n    return asdict(ref)\n", "asdict-ref"),
    ],
)
def test_a_hand_built_identity_mapping_is_flagged(source: str, label: str) -> None:
    found = _keys(scan.serializer_label, source, "verification/postmask/_blocked.py")
    assert [k.split("::")[2] for k in found] == [label]


@pytest.mark.parametrize(
    "source",
    ['def f():\n    return {"namespace": 1}\n', "def f(finding):\n    return asdict(finding)\n"],
)
def test_other_mappings_are_not_flagged(source: str) -> None:
    assert _keys(scan.serializer_label, source, "verification/postmask/_blocked.py") == []


def test_building_the_mapping_inline_in_the_blocked_module_is_flagged() -> None:
    rel = "verification/postmask/_blocked.py"
    planted = _real(rel) + '\n\ndef _p(r):\n    return {"namespace": r.namespace, "code": r.code}\n'
    assert len(_keys(scan.serializer_label, planted, rel)) == 1


# ----- one applicability predicate ----------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "def f(config):\n    return bool(config.relationships) or integrity\n",
        'def f(c):\n    return c.get("relationships") and post_scan_integrity_requested(c)\n',
    ],
)
def test_weighing_keys_against_integrity_outside_the_predicate_is_flagged(source: str) -> None:
    found = _keys(scan.applies_label, source, "execution/_job_estimate.py")
    assert [k.split("::")[2] for k in found] == ["applies-decision"]


def test_the_predicate_itself_is_the_one_permitted_site() -> None:
    rel = "config/_post_scan.py"
    found = _keys(scan.applies_label, _real(rel), rel)
    assert found and all(k.startswith(scan.APPLIES_HOME) for k in found)


def test_setting_required_from_a_local_expression_in_the_gate_is_flagged() -> None:
    real = _real(scan.GATE)
    planted = real.replace(
        "self.required = post_scan_gate_applies(config)",
        'self.required = bool(config.get("relationships")) or post_scan_integrity_requested(config)',
    )
    assert planted != real
    found = _keys(scan.required_label, planted, scan.GATE)
    assert [k.split("::")[2] for k in found] == ["required-assign"]
    assert _keys(scan.applies_label, planted, scan.GATE)


# ----- no commit without the gate -----------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ("def f(sink):\n    sink.commit()\n", "commit-call"),
        ("def f(self):\n    self._tsink.commit()\n", "commit-call"),
        ("def f(a, b):\n    atomic_commit(a, b)\n", "atomic-commit"),
        ("def f(x):\n    return getattr(x, 'commit')\n", "getattr-commit"),
    ],
)
def test_a_commit_outside_the_observer_is_flagged(source: str, label: str) -> None:
    found = _keys(scan.commit_label, source, "execution/x.py")
    assert [k.split("::")[2] for k in found] == [label]


def test_the_publish_session_wrapper_is_not_flagged() -> None:
    assert _keys(scan.commit_label, "def f(publish):\n    publish.commit()\n", "x.py") == []


def test_calling_the_sink_commit_directly_in_the_sequential_route_is_flagged() -> None:
    real = _real("execution/_sequential.py")
    planted = real.replace("observed_commit(_tsink)", "_tsink.commit()", 1)
    assert planted != real
    found = _keys(scan.commit_label, planted, "execution/_sequential.py")
    assert any("::commit-call::" in k for k in found)


# ----- one model predicate ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ("def f(b):\n    return b.get('ner_findings')\n", "ner-findings-read"),
        ("def f(b):\n    return b.detectors != 'regex'\n", "detectors-compare"),
    ],
)
def test_a_model_check_outside_the_predicate_is_flagged(source: str, label: str) -> None:
    found = _keys(scan.model_label, source, "verification/x.py")
    assert [k.split("::")[2] for k in found] == [label]


def test_inlining_the_model_test_in_the_route_guard_is_flagged() -> None:
    planted = _real("verification/postmask/_route_guard.py") + (
        "\n\ndef _planted(block):\n    return block.get('ner_findings') == 'fail'\n"
    )
    found = _keys(scan.model_label, planted, "verification/postmask/_route_guard.py")
    assert found


# ----- one route readability decision -------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "def f(c):\n    return post_scan_resident_reason(c, output_readable=True,"
        " input_baseline_readable=True)\n",
        "def f(c):\n    return post_scan_output_readable('None')\n",
    ],
)
def test_a_readability_decision_outside_the_helper_is_flagged(source: str) -> None:
    found = _keys(scan.readability_label, source, "execution/x.py")
    assert [k.split("::")[2] for k in found] == ["decision-call"]


def test_passing_a_literal_sink_kind_to_the_helper_is_flagged() -> None:
    real = _real("execution/_job_estimate.py")
    planted = real.replace("sink_kind=NO_SINK_CLASS_LABEL", "sink_kind='None'", 1)
    assert planted != real
    found = _keys(scan.readability_label, planted, "execution/_job_estimate.py")
    assert [k.split("::")[2] for k in found] == ["literal-fact"]
