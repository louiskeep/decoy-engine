"""F29 acceptance A1 and A2: the reason registry is append-only and covers every producer.

A1 pins a v1 snapshot of the catalog (identities, fixed safe summaries, kind, phase rules,
retry and outcome semantics). A2 derives coverage from the E0 producer inventory, so a producer
that the registry does not know cannot hide behind a literal-only search.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

from decoy_engine import reasons

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "f29"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sentry"))
import _reason_baseline as baseline_builder  # noqa: E402

_SEMANTIC_FIELDS = ("namespace", "code", "kind", "rules", "owner", "context", "since")


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return (major, minor, patch)


def manifest_violations(pinned: dict, live: dict) -> list[str]:
    """Why ``live`` is not an append-only successor of ``pinned``; empty when it is."""
    problems: list[str] = []
    live_by_id = {(e["namespace"], e["code"]): e for e in live["entries"]}
    pinned_version = _version_tuple(pinned["registry_version"])
    live_version = _version_tuple(live["registry_version"])
    if live_version < pinned_version:
        problems.append("registry_version went backwards")
    if live["reason_schema_version"] < pinned["reason_schema_version"]:
        problems.append("reason_schema_version went backwards")
    summary_changed = False
    for old in pinned["entries"]:
        ident = (old["namespace"], old["code"])
        new = live_by_id.get(ident)
        if new is None:
            problems.append(f"deleted identity {ident}")
            continue
        for field in _SEMANTIC_FIELDS:
            if new[field] != old[field]:
                problems.append(
                    f"{ident}: {field} changed (retry/outcome/phase semantics are frozen)"
                )
        if new["summary"] != old["summary"]:
            summary_changed = True
        if new["symbol"] != old["symbol"]:
            problems.append(f"{ident}: symbol changed")
        if old["deprecated_in"] is not None and new["deprecated_in"] != old["deprecated_in"]:
            problems.append(f"{ident}: tombstone changed")
    if (
        summary_changed
        and live_version[:2] == pinned_version[:2]
        and live_version == pinned_version
    ):
        problems.append("a fixed safe summary changed without a registry version bump")
    added = set(live_by_id) - {(e["namespace"], e["code"]) for e in pinned["entries"]}
    if added and live_version[:2] == pinned_version[:2] and live_version[2] == pinned_version[2]:
        problems.append("identities were added without a registry minor version bump")
    for entry in live["entries"]:
        link = entry["replaced_by"]
        if (entry["deprecated_in"] is None) != (link is None):
            problems.append(
                f"{entry['namespace']}.{entry['code']}: deprecation and replacement must pair"
            )
        if link is not None and tuple(link) not in live_by_id:
            problems.append(
                f"{entry['namespace']}.{entry['code']}: replacement {tuple(link)} is not an identity"
            )
    return problems


def test_v1_manifest_is_append_only() -> None:
    pinned = _load("reason-manifest-v1.json")
    live = json.loads(json.dumps(reasons.manifest()))
    assert manifest_violations(pinned, live) == []
    assert reasons.check_integrity() == []


def test_baseline_identities_all_registered() -> None:
    baseline = _load("reason-baseline.json")
    missing = [
        (i["namespace"], i["code"])
        for i in baseline["identities"]
        if reasons.find(i["namespace"], i["code"]) is None
    ]
    assert missing == []


def test_public_legacy_surfaces_unchanged() -> None:
    from decoy_engine.errors import MaskKeyRequiredError, PKDuplicatesError
    from decoy_engine.validation_result import CODES

    assert CODES.TOP_LEVEL_BAD_MODE == "top_level.bad_mode"
    assert PKDuplicatesError.code == "pk.duplicates"
    assert MaskKeyRequiredError.code == "mask.key_required"
    assert reasons.runtime.MISSING_MASK_SECRET == "missing_mask_secret"
    spec = reasons.runtime.MISSING_MASK_SECRET.spec
    assert spec.default_rule.retry is reasons.RetryClass.CONFIGURATION_REPAIR


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda m: m["entries"].pop(0), "deleted identity"),
        (lambda m: m["entries"][3]["rules"][0].__setitem__("retry", "never"), "rules changed"),
        (lambda m: m["entries"][3].__setitem__("kind", "warning"), "kind changed"),
        (
            lambda m: m["entries"][3].__setitem__("summary", "Edited."),
            "without a registry version bump",
        ),
        (lambda m: m["entries"][3].__setitem__("deprecated_in", "1.1.0"), "must pair"),
        (
            lambda m: m["entries"][3].update(deprecated_in="1.1.0", replaced_by=["nowhere", "x"]),
            "is not an identity",
        ),
    ],
)
def test_manifest_mutants_are_rejected(mutate, expected) -> None:
    pinned = _load("reason-manifest-v1.json")
    live = copy.deepcopy(pinned)
    mutate(live)
    assert any(expected in p for p in manifest_violations(pinned, live))


def test_added_identity_needs_a_minor_bump() -> None:
    pinned = _load("reason-manifest-v1.json")
    live = copy.deepcopy(pinned)
    live["entries"].append(
        {**live["entries"][0], "code": "brand_new_code", "symbol": "BRAND_NEW_CODE"}
    )
    assert any("minor version bump" in p for p in manifest_violations(pinned, live))
    live["registry_version"] = "1.1.0"
    assert manifest_violations(pinned, live) == []


def test_summary_edit_allowed_only_with_a_version_bump() -> None:
    pinned = _load("reason-manifest-v1.json")
    live = copy.deepcopy(pinned)
    live["entries"][5]["summary"] = "Edited."
    live["registry_version"] = "1.0.1"
    assert manifest_violations(pinned, live) == []


# -- A2 ---------------------------------------------------------------------------------------


def _family_namespace(family: str) -> str | None:
    return baseline_builder.FAMILY_NAMESPACE.get(family.split("(")[0])


def test_every_producer_resolves() -> None:
    inventory = _load("producer-inventory.json")
    unresolved: list[str] = []
    checked = 0
    for site in inventory["sites"]:
        if not site["codes"]:
            continue
        if site["kind"] in ("kw", "positional"):
            ns = _family_namespace(site["family"])
            if ns is None:
                continue
            for code in site["codes"]:
                checked += 1
                if reasons.find(ns, code) is None:
                    unresolved.append(f"{site['file']}:{site['line']} {site['family']} {code}")
        else:
            for code in site["codes"]:
                checked += 1
                if not reasons.is_registered(code):
                    unresolved.append(f"{site['file']}:{site['line']} {site['family']} {code}")
    assert checked > 1000
    assert unresolved == []


def test_dynamic_code_families_are_finite_tables() -> None:
    from decoy_engine.execution._sequential_native import _OPS

    drift = reasons.runtime.SEQUENTIAL_INPUT_TYPE_DRIFT_BY_STRATEGY
    assert set(drift) == set(_OPS)
    for strategy, code in drift.items():
        assert code == f"sequential_{strategy}_input_type_drift"
        assert reasons.find("runtime", code) is not None

    refs = reasons.plan.GROUPED_SERIES_MISSING_REF_BY_KEY
    assert set(refs) == {"group_by", "order_by"}
    for key, code in refs.items():
        assert code == f"grouped_series_missing_{key}_ref"
        assert reasons.find("plan", code) is not None


def test_every_symbol_matches_its_attribute() -> None:
    for spec in reasons.specs():
        module = getattr(reasons, spec.namespace)
        attr = getattr(module, spec.symbol, None)
        assert attr == spec.code, (spec.namespace, spec.symbol)
        assert getattr(attr, "namespace", None) == spec.namespace


def test_every_wire_code_has_a_safe_shape() -> None:
    shape = re.compile(r"^[a-z][a-z0-9_.:-]*$")
    assert [s.code for s in reasons.specs() if not shape.match(s.code)] == []


def test_registry_is_a_leaf() -> None:
    pkg = Path(reasons.__file__).parent
    forbidden = re.compile(
        r"^\s*(from|import)\s+decoy_engine\.(execution|plan|generation|providers_v2|connectors|"
        r"quality|subset|errors|progress)\b",
        re.MULTILINE,
    )
    offenders = [str(p) for p in pkg.rglob("*.py") if forbidden.search(p.read_text())]
    assert offenders == []
