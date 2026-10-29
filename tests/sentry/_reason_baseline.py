"""Builds the frozen F29 reason baseline and the producer inventory from source.

``python tests/sentry/_reason_baseline.py --write`` regenerates the two fixtures under
``tests/fixtures/f29/``. The baseline (``reason-baseline.json``) is frozen at the F29 baseline
commit: the registry may add identities, never remove or re-home one. The inventory
(``producer-inventory.json``) is the live site list and may only shrink as producers migrate.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _reason_scan as scan

FIXTURES = scan.ROOT / "tests" / "fixtures" / "f29"

# Exception / record family -> registry namespace. A namespace names the surface a consumer
# sees the code on; the same wire string may exist in two namespaces without renaming either.
FAMILY_NAMESPACE: dict[str, str] = {
    "PipelineValidationError": "config",
    "ValidationError": "config",
    "ValidationMessage": "config",
    "SubsetConfigError": "config",
    "NamespaceConfigError": "config",
    "PlanCompileError": "plan",
    "PlanCheckError": "plan",
    "StatisticalSpecError": "plan",
    "CompositeError": "plan",
    "PinnedModeError": "plan",
    "GenerationAdmissionError": "plan",
    "_reject_unusable_bound": "plan",
    "_reject_nested_keys": "plan",
    "ExecutionError": "runtime",
    "StrategyError": "runtime",
    "GenerationError": "runtime",
    "PoolCapacityError": "runtime",
    "TransformError": "runtime",
    "_transforms.TransformError": "runtime",
    "DeterminismError": "runtime",
    "FpeUnencryptableError": "runtime",
    "FpeChecksumError": "runtime",
    "FpeRowError": "runtime",
    "MixedDeclineError": "runtime",
    "DrawSiteProtocolError": "runtime",
    "VaultError": "runtime",
    "MaskSecretError": "runtime",
    "KeyedStrategyRequiresSecret": "runtime",
    "MissingMaskSecret": "runtime",
    "WeakMaskSecret": "runtime",
    "NativeChunkSchemaDriftError": "runtime",
    "SubsetBudgetExceededError": "subset",
    "GenerationBudgetExceeded": "runtime",
    "GenerationUnboundedError": "runtime",
    "GenerationWindowError": "runtime",
    "GenerationWindowTimeoutError": "runtime",
    "SubsetInternalError": "runtime",
    "row_positions": "runtime",
    "positional_key_array": "runtime",
    "_apply_sub_floor_span_policy": "runtime",
    "CapacityEstimate": "runtime",
    "PostgresSnapshotError": "connector",
    "PreflightFailure": "subset",
    "SubsetPreflightError": "subset",
    "ProviderError": "provider",
    "AdapterError": "provider",
    "IdentifierError": "provider",
    "NerUnavailableError": "provider",
    "CarrierError": "quality",
    "ProvenanceError": "quality",
    "DpBudgetError": "quality",
    "DpError": "quality",
    "DistributionSnapshotError": "quality",
    "QualityWarning": "warning",
    "ShadowDifference": "physical",
    "RouteEvent": "route",
}
# Closed vocabularies that are not reasons, with the reason each is excluded from the registry.
EXCLUDED_ENUMS: dict[str, str] = {
    "RowErrorCompleteness": "completeness label on a row-error summary, not a failure reason",
    "CapacityVerdict": "capacity verdict vocabulary; the failure reason is CapacityEstimate.code",
    "DriverId": "execution driver identity",
    "ExecutionScope": "physical-plan scope vocabulary",
    "Residency": "physical-plan residency vocabulary",
    "PublicationMode": "physical-plan publication vocabulary",
    "Substrate": "execution substrate identity",
    "OperatorFamily": "physical-plan operator vocabulary",
    "CardinalityMode": "config enum",
    "PIIClass": "detector classification, not a reason",
    "OrphanPolicy": "config enum",
}


# Code spaces the AST cannot enumerate (an f-string, a dict value read from a name). E0 resolved
# each one to a finite table; the registry carries every member as an explicit entry.
_NATIVE_STRATEGIES = (
    "hash", "fpe", "categorical", "bucket_perturb", "date_shift", "faker",
    "text_redact", "text_mask", "group_key",
)  # fmt: skip
_POST_VALIDATION_SCANS = (
    "cardinality", "composite_coherence", "determinism_sample", "fk_validity", "format_rules",
    "leakage", "null_audit", "pk_uniqueness", "sampled_values",
)  # fmt: skip
_DIAGNOSTIC_CHECKS = ("column_survival", "kind_drift", "null_drift", "row_count")
FINITE_TABLES: list[tuple[str, str, str, str]] = (
    [
        ("plan", f"grouped_series_missing_{k}_ref", "PlanCompileError", "plan/_checks_grouped_series.py#table:ref_key")
        for k in ("group_by", "order_by")
    ]
    + [
        ("runtime", f"sequential_{s}_input_type_drift", "ExecutionError", "execution/_sequential_native.py#table:strategy")
        for s in _NATIVE_STRATEGIES
    ]
    + [
        ("verification", n, "PostValidationScan", "validation/post/_checks#table:scan_name")
        for n in _POST_VALIDATION_SCANS
    ]
    + [("verification", n, "QualityDiagnostic", "quality/diagnostic.py#table:check") for n in _DIAGNOSTIC_CHECKS]
)  # fmt: skip


def _namespace_for_const(site: scan.Site) -> str:
    if site.kind == "enum_member":
        return "route"
    if site.kind == "class_attr":
        return "config" if site.family.startswith("CODES.") else "runtime"
    if site.file.startswith("execution/physical/_shadow_diff"):
        return "physical"
    if site.file.startswith("execution/physical/"):
        return "route"
    if "REASON" in site.family.upper() and site.file.startswith(
        ("execution/native", "execution/out_of_core")
    ):
        return "route"
    if site.family.startswith(("REASON_", "_REASON_", "DRIVER_REASON")):
        return "route"
    return "runtime"


def build() -> tuple[dict[str, object], dict[str, object]]:
    sites = scan.scan()
    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    used_by_call: dict[str, set[str]] = defaultdict(set)  # code -> namespaces seen at call sites
    unmapped: set[str] = set()
    for s in sites:
        if s.kind in ("kw", "positional") and s.codes:
            fam = s.family.split("(")[0]
            if fam == "super().__init__":
                continue
            ns = FAMILY_NAMESPACE.get(fam)
            if ns is None:
                unmapped.add(fam)
                continue
            for code in s.codes:
                used_by_call[code].add(ns)
                owners[(ns, code)].add(fam)
                sources[(ns, code)].add(f"{s.file}#{s.kind}:{fam}")
    for s in sites:
        if s.kind == "dict_payload" and s.codes:
            for code in s.codes:
                used_by_call[code].add("verification")
                owners[("verification", code)].add("QualityReport")
                sources[("verification", code)].add(f"{s.file}#dict_payload:code")
    for ns, code, owner, src in FINITE_TABLES:
        used_by_call[code].add(ns)
        owners[(ns, code)].add(owner)
        sources[(ns, code)].add(src)
    for s in sites:
        if s.kind in ("module_const", "class_attr", "enum_member") and s.codes:
            for code in s.codes:
                if code in used_by_call:
                    for ns in used_by_call[code]:
                        sources[(ns, code)].add(f"{s.file}#{s.kind}:{s.family}")
                    continue
                ns = _namespace_for_const(s)
                owners[(ns, code)].add(s.family.split(".")[0])
                sources[(ns, code)].add(f"{s.file}#{s.kind}:{s.family}")
    # Public exception classes whose ``code`` is set from a class attribute: owners are the classes.
    baseline = {
        "baseline_commit": "2a44723ff26ffd16c69f026582ec93998d67ca5d",
        "note": "append-only; a code may be added, never removed, re-homed or re-interpreted",
        "identities": [
            {
                "namespace": ns,
                "code": code,
                "owners": sorted(owners[(ns, code)]),
                "sources": sorted(sources[(ns, code)]),
            }
            for ns, code in sorted(owners)
        ],
        "excluded_enums": dict(sorted(EXCLUDED_ENUMS.items())),
        "unmapped_families": sorted(unmapped),
    }
    inventory = {
        "baseline_commit": baseline["baseline_commit"],
        "sites": scan.to_rows(sites),
        "consumers": [
            {"file": c.file, "line": c.line, "function": c.function, "expr": c.expr}
            for c in scan.scan_consumers()
        ],
    }
    return baseline, inventory


def _dump(obj: dict[str, object]) -> str:
    """JSON with one list row per line: small diffs, reviewable in a PR."""
    lines = ["{"]
    items = list(obj.items())
    for i, (key, val) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        if isinstance(val, list):
            rows = ",\n".join("  " + json.dumps(r, sort_keys=True) for r in val)
            lines.append(f" {json.dumps(key)}: [\n{rows}\n ]{comma}")
        else:
            lines.append(f" {json.dumps(key)}: {json.dumps(val, sort_keys=True)}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> None:
    baseline, inventory = build()
    if "--write" in sys.argv:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        (FIXTURES / "reason-baseline.json").write_text(_dump(baseline))
        (FIXTURES / "producer-inventory.json").write_text(_dump(inventory))
    idents = baseline["identities"]
    print(len(idents), "identities;", len(inventory["sites"]), "sites")  # type: ignore[arg-type]
    print("unmapped:", baseline["unmapped_families"])


if __name__ == "__main__":
    main()
