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
from scripts.release.reason_manifest_policy import append_only_violations

from decoy_engine import reasons

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "f29"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sentry"))
import _reason_baseline as baseline_builder  # noqa: E402


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


manifest_violations = append_only_violations


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
        (
            lambda m: m["entries"][3].__setitem__("replaced_by", ["runtime", "x"]),
            "without deprecation",
        ),
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


def test_a_deprecated_entry_with_no_replacement_is_a_valid_tombstone() -> None:
    pinned = _load("reason-manifest-v1.json")
    live = copy.deepcopy(pinned)
    live["entries"][3]["deprecated_in"] = "1.1.0"
    live["registry_version"] = "1.1.0"
    # The deprecation itself is a change to the entry's metadata, which is allowed; its meaning is not.
    assert manifest_violations(pinned, live) == []


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


def test_every_registered_code_resolves_through_its_family_without_fallback() -> None:
    """Runtime coverage: a trusted engine family reports each code of its namespace as itself.

    `T` skips the family's own constructor (their signatures differ: positional, keyword-only,
    wrapped) and sets only `.code`, so this exercises the adapter's trust path for every
    (family, code) pair, not just the codes a unit test happens to raise."""
    import importlib

    from decoy_engine.reason_adapter import _CODED, reason_from_exception

    fallbacks: list[str] = []
    for module, name, namespace in _CODED:
        family = getattr(importlib.import_module(module), name)
        shell = type(
            "T", (family,), {"__init__": lambda self, code: object.__setattr__(self, "code", code)}
        )
        for spec in reasons.specs():
            if spec.namespace != namespace:
                continue
            record = reason_from_exception(shell(spec.code))
            if (record.namespace, record.code) != (namespace, spec.code):
                fallbacks.append(f"{name}:{namespace}.{spec.code}")
    assert fallbacks == []


def test_class_level_and_default_codes_are_registered_symbols() -> None:
    import importlib

    from decoy_engine.reason_adapter import _CODED

    unregistered = []
    for module, name, namespace in _CODED:
        family = getattr(importlib.import_module(module), name)
        code = family.__dict__.get("code")
        if isinstance(code, str) and reasons.find(namespace, code) is None:
            unregistered.append(f"{name}.code={code!r}")
        if isinstance(code, str):
            assert isinstance(code, reasons.ReasonCode), name  # a symbol, not a bare literal
    assert unregistered == []


def test_the_baseline_fixture_is_frozen() -> None:
    """The baseline is the registry's reference for 'a code never disappears'. It is rebuilt only
    from the baseline commit's tree (see `_reason_baseline.py`), never from migrated source."""
    import hashlib

    digest = hashlib.sha256((FIXTURES / "reason-baseline.json").read_bytes()).hexdigest()
    assert digest == "8b3345c2d8e5eb3541cde2f238b7048d712e5fc3903806f7ec79642520fed118"


@pytest.mark.parametrize("code", ["derived_ref_missing", "derived_evaluation_failed"])
def test_derived_visibility_codes_are_registered_execution_errors(code: str) -> None:
    """T7 (slice 0 item 3): the two additive `runtime` identities exist, owned by
    `ExecutionError`, with an exported symbol whose value is the wire string."""
    from decoy_engine import reasons

    spec = reasons.find("runtime", code)
    assert spec is not None
    assert spec.owner == "ExecutionError"
    assert getattr(reasons.runtime, code.upper()) == code


FK_FANOUT_CODES = [
    ("quality", "fanout_column_unknown", "DistributionSnapshotError"),
    ("plan", "reference_fanout_missing", "PlanCompileError"),
    ("plan", "reference_fanout_invalid", "PlanCompileError"),
    ("plan", "reference_fanout_conflict", "PlanCompileError"),
    ("plan", "reference_parent_pool_empty", "StatisticalSpecError"),
    ("plan", "dp_generate_fitted_fanout_unsupported", "PlanCompileError"),
]


@pytest.mark.parametrize(("namespace", "code", "owner"), FK_FANOUT_CODES)
def test_fk_fanout_codes_are_registered_with_their_owner(
    namespace: str, code: str, owner: str
) -> None:
    """E0 (QA-FK-FANOUT): six additive identities, each with an exported symbol whose
    value is the wire string and `since` equal to the minor that introduced them."""
    from decoy_engine import reasons

    spec = reasons.find(namespace, code)
    assert spec is not None
    assert spec.owner == owner
    assert spec.since == "1.14.0"
    assert getattr(getattr(reasons, namespace), code.upper()) == code


def test_fk_fanout_minor_has_one_fixture_with_exactly_its_identities() -> None:
    pinned = _load("reason-manifest-v1.14-fk-fanout.json")
    got = sorted((e["namespace"], e["code"]) for e in pinned["entries"])
    assert got == sorted((ns, c) for ns, c, _ in FK_FANOUT_CODES)
    assert pinned["registry_version"] == "1.14.0"


CELL_FORMAT_FIXTURE = "reason-manifest-v1.16-gen-fit-shape.json"


def test_cell_format_code_is_registered_with_its_owner() -> None:
    """E0 (GEN-FIT-SHAPE): one additive identity owned by StatisticalSpecError, `since` 1.16.0."""
    from decoy_engine import reasons

    spec = reasons.find("plan", "statistical_cell_format_invalid")
    assert spec is not None
    assert spec.owner == "StatisticalSpecError"
    assert spec.since == "1.16.0"
    assert reasons.plan.STATISTICAL_CELL_FORMAT_INVALID == "statistical_cell_format_invalid"


def test_cell_format_minor_has_one_fixture_with_exactly_its_identity() -> None:
    pinned = _load(CELL_FORMAT_FIXTURE)
    got = [(e["namespace"], e["code"]) for e in pinned["entries"]]
    assert got == [("plan", "statistical_cell_format_invalid")]
    assert pinned["registry_version"] == "1.16.0"


GEN_UNIQUE_FIXTURE = "reason-manifest-v1.17-gen-unique.json"
GEN_UNIQUE = [
    ("config", "generate_unique_conflict", "PipelineValidationError"),
    ("config", "generate_unique_domain_too_small", "PipelineValidationError"),
    ("config", "generate_unique_unsupported", "PipelineValidationError"),
    ("plan", "pinned_unique_pair_unsupported", "PinnedModeError"),
]


@pytest.mark.parametrize(("namespace", "code", "owner"), GEN_UNIQUE)
def test_gen_unique_codes_are_registered_configuration_repairs(
    namespace: str, code: str, owner: str
) -> None:
    """E0 (GEN-UNIQUE): four additive identities, `since` 1.17.0, none raised yet."""
    spec = reasons.find(namespace, code)
    assert spec is not None
    assert spec.since == "1.17.0"
    assert spec.owner == owner
    assert spec.kind is reasons.Kind.FAILURE
    assert [r.retry for r in spec.rules] == [reasons.RetryClass.CONFIGURATION_REPAIR]
    assert getattr(getattr(reasons, namespace), code.upper()) == code
    assert not re.search(r"[{}%]", spec.summary)


def test_gen_unique_minor_has_one_fixture_with_exactly_its_identities() -> None:
    pinned = _load(GEN_UNIQUE_FIXTURE)
    assert [(e["namespace"], e["code"]) for e in pinned["entries"]] == sorted(
        (n, c) for n, c, _ in GEN_UNIQUE
    )
    assert pinned["registry_version"] == "1.17.0"


GEN_STRICT_FIXTURE = "reason-manifest-v1.13-gen-strict.json"
GEN_STRICT = [
    ("config", "generate_column_duplicate", "PipelineValidationError"),
    ("config", "generate_formula_invalid", "PipelineValidationError"),
    ("config", "generate_option_range", "PipelineValidationError"),
    ("config", "generate_option_type", "PipelineValidationError"),
    ("config", "generate_option_unknown", "PipelineValidationError"),
    ("config", "generate_option_value_unknown", "PipelineValidationError"),
    ("config", "generate_weights_invalid", "PipelineValidationError"),
    ("plan", "generate_categories_mixed", "PlanCompileError"),
    ("plan", "generate_column_forward_reference", "PlanCompileError"),
    ("plan", "generate_faker_kwargs_unknown", "PlanCompileError"),
    ("plan", "generate_faker_type_unknown", "PlanCompileError"),
    ("plan", "generate_formula_name_unknown", "PlanCompileError"),
    ("plan", "generate_formula_reference_missing", "PlanCompileError"),
    ("plan", "generate_formula_reference_unknown", "PlanCompileError"),
    ("plan", "generate_formula_template_syntax", "PlanCompileError"),
    ("plan", "generate_locale_unknown", "PlanCompileError"),
    ("plan", "generate_option_invalid", "PlanCompileError"),
    ("plan", "grouped_series_group_by_equals_order_by", "PlanCompileError"),
    ("plan", "reference_weights_length_mismatch", "PlanCompileError"),
]


@pytest.mark.parametrize(("namespace", "code", "owner"), GEN_STRICT)
def test_gen_strict_codes_are_registered_configuration_repairs(
    namespace: str, code: str, owner: str
) -> None:
    spec = reasons.find(namespace, code)
    assert spec is not None
    assert spec.since == "1.13.0"
    assert spec.owner == owner
    assert spec.kind is reasons.Kind.FAILURE
    assert [r.retry for r in spec.rules] == [reasons.RetryClass.CONFIGURATION_REPAIR]
    assert getattr(getattr(reasons, namespace), code.upper()) == code


def test_gen_strict_minor_adds_exactly_these_codes_and_is_pinned() -> None:
    version = tuple(int(p) for p in str(reasons.manifest()["registry_version"]).split("."))
    assert version >= (1, 11, 0)
    live = [
        e for e in json.loads(json.dumps(reasons.manifest()))["entries"] if e["since"] == "1.13.0"
    ]
    assert {(e["namespace"], e["code"]) for e in live} == {(n, c) for n, c, _ in GEN_STRICT}
    pinned = _load(GEN_STRICT_FIXTURE)
    assert pinned["entries"] == live


def test_gen_strict_summaries_carry_no_placeholder() -> None:
    # The summary is the refusal text, so a format slot would invite a value into it.
    for namespace, code, _ in GEN_STRICT:
        spec = reasons.find(namespace, code)
        assert spec is not None
        assert not re.search(r"[{}%]", spec.summary), (namespace, code)


def test_fit_redact_code_is_a_registered_plan_error_with_a_pinned_fixture() -> None:
    """The one 1.11.0 identity: owned by `StatisticalSpecError`, exported under its wire string,
    and the only entry of its minor fixture."""
    from decoy_engine import reasons

    spec = reasons.find("plan", "statistical_snapshot_values_redacted")
    assert spec is not None
    assert spec.owner == "StatisticalSpecError"
    assert spec.code == reasons.plan.STATISTICAL_SNAPSHOT_VALUES_REDACTED
    fixture = _load("reason-manifest-v1.11-fit-redact.json")
    assert fixture["registry_version"] == "1.11.0"
    assert [(e["namespace"], e["code"]) for e in fixture["entries"]] == [
        ("plan", "statistical_snapshot_values_redacted")
    ]
