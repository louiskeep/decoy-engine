"""Reason codes may only be created through the registry (F29 choke-point sentry).

Every machine-readable reason code the engine emits must come from ``decoy_engine.reasons``.
This is a construction-time ratchet, modelled on ``test_log_interpolation.py``: the scanner
(``_reason_scan``) finds each place that creates, stores or forwards a code, and classifies the
value. A literal, an f-string, a computed string or an unchecked forward is a violation.

Allowed:
  * a registry symbol (``reasons.runtime.X``), or a module constant / imported name / class
    attribute that resolves to one;
  * ``None`` for an optional code;
  * a forward of a ``code`` parameter or an already-registered exception's ``.code`` *inside a
    function listed in ``ADAPTERS``*. Each adapter has a rationale and is an exact
    (module, function) entry, not a directory exemption.

No migration allowlist remains: every built-in producer was migrated, so any violation is new.

The seeded mutants below prove the scanner fails each way a code can be smuggled in: aliased
constructor, positional code, f-string, helper return, class constant, dict payload, a
code table, and a newly added module.
"""

from __future__ import annotations

import sys
import textwrap
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _reason_scan as scan

FIXTURES = scan.ROOT / "tests" / "fixtures" / "f29"

_CTOR = "legacy string constructor input; unknown identities are normalised at the structured-output boundary"
_HELPER = "private helper; every caller passes a registry symbol, checked at the caller's site"
_REWRAP = "re-wraps the already-registered code of the exception it catches"
_KERNEL = "forwards a compiled-kernel code only after an `in` guard against known codes"

ADAPTERS: dict[tuple[str, str], str] = {
    # -- constructors that store the caller's code -------------------------------------------
    ("_reference_date.py", "PinnedModeError.__init__"): _CTOR,
    ("connectors/postgres_snapshot.py", "PostgresSnapshotError.__init__"): _CTOR,
    ("detection/ner.py", "NerUnavailableError.__init__"): _CTOR,
    ("determinism/_derive.py", "DeterminismError.__init__"): _CTOR,
    ("errors.py", "FpeChecksumError.__init__"): _CTOR,
    ("errors.py", "FpeUnencryptableError.__init__"): _CTOR,
    ("errors.py", "PipelineValidationError.__init__"): _CTOR,
    ("errors.py", "ValidationError.__init__"): _CTOR,
    ("execution/_errors.py", "ExecutionError.__init__"): _CTOR,
    ("execution/_errors.py", "StrategyError.__init__"): _CTOR,
    ("execution/_mixed_admission.py", "MixedDeclineError.__init__"): _CTOR,
    ("execution/_transforms.py", "TransformError.__init__"): _CTOR,
    ("execution/native/_draw_site_providers.py", "DrawSiteProtocolError.__init__"): _CTOR,
    ("execution/physical/_shadow_diff_codes.py", "ShadowDifference.__init__"): _CTOR,
    ("generation/_budget_ledger.py", "GenerationBudgetError.__init__"): _CTOR,
    ("generation/_window_admission.py", "GenerationAdmissionError.__init__"): _CTOR,
    ("generation/_window_admission.py", "GenerationWindowError.__init__"): _CTOR,
    ("generation/composite/_errors.py", "CompositeError.__init__"): _CTOR,
    ("generation/pool/_errors.py", "GenerationError.__init__"): _CTOR,
    ("generation/pool/_errors.py", "PoolCapacityError.__init__"): _CTOR,
    ("generation/statistical/_spec.py", "StatisticalSpecError.__init__"): _CTOR,
    ("keyprovider.py", "MaskSecretError.__init__"): _CTOR,
    ("plan/_errors.py", "PlanCompileError.__init__"): _CTOR,
    ("providers_v2/_errors.py", "ProviderError.__init__"): _CTOR,
    ("providers_v2/identifiers/_errors.py", "IdentifierError.__init__"): _CTOR,
    ("quality/carriers.py", "CarrierError.__init__"): _CTOR,
    ("quality/dp.py", "DpError.__init__"): _CTOR,
    ("quality/dp_budget.py", "DpBudgetError.__init__"): _CTOR,
    ("quality/dp_provenance.py", "ProvenanceError.__init__"): _CTOR,
    ("quality/snapshot.py", "DistributionSnapshotError.__init__"): _CTOR,
    ("subset/_errors.py", "SubsetError.__init__"): _CTOR,
    ("subset/_errors.py", "SubsetPreflightError.__init__"): _CTOR,
    ("vault.py", "VaultError.__init__"): _CTOR,
    ("validation_result.py", "ValidationResult.add_error"): _CTOR,
    ("validation_result.py", "ValidationResult.add_warning"): _CTOR,
    ("progress.py", "ProgressChannel.route"): _CTOR,
    # -- private helpers that forward a parameter --------------------------------------------
    ("execution/_positional_keys.py", "row_positions"): _HELPER,
    ("execution/_positional_keys.py", "positional_key_array"): _HELPER,
    ("execution/_transforms.py", "_reject_nested_keys"): _HELPER,
    ("execution/native/_crypto_reference.py", "_row_error"): _HELPER,
    ("generation/_pinned_compile.py", "_reject_column"): _HELPER,
    ("generation/_window_admission.py", "_refuse"): _HELPER,
    ("plan/_checks_dp.py", "_raise"): _HELPER,
    ("plan/_checks_top_code.py", "_reject_unusable_bound"): _HELPER,
    # -- re-wraps of an already-registered exception code ------------------------------------
    ("execution/_chunked_when_guard.py", "PositionalWhenGuard.check_schema"): _REWRAP,
    ("execution/native/_fpe_route.py", "fpe_fail_closed_error"): _REWRAP,
    ("execution/out_of_core/_capacity_eval.py", "enforce_ooc_memory_preflight"): _REWRAP,
    ("execution/out_of_core/_capacity_eval.py", "evaluate_capacity"): _REWRAP,
    ("execution/out_of_core/_preflight.py", "run_preflight"): _REWRAP,
    ("execution/physical/_shadow_mixed.py", "_as_shadow_difference"): _REWRAP,
    ("plan/_checks.py", "_check_ner_available_for_strategy"): _REWRAP,
    ("plan/_checks.py", "check_statistical_columns"): _REWRAP,
    ("plan/validate/_consolidator.py", "validate_plan"): _REWRAP,
    ("subset/_api.py", "_compute"): _REWRAP,
    ("unmask.py", "unmask_pipeline"): _REWRAP,
    ("execution/native/_route_decline_codes.py", "RouteDeclineRecord.to_payload"): _REWRAP,
    # -- compiled-kernel translators ---------------------------------------------------------
    ("execution/native/_crypto_ext.py", "_translate_compiled_kernel_error"): _KERNEL,
    ("execution/native/_fpe_ext.py", "_translate_compiled_fpe_error"): _KERNEL,
    ("execution/native/_group_key_ext.py", "_translate_compiled_raw_hex_kernel_error"): _KERNEL,
    ("execution/native/_index_ext.py", "_translate_compiled_index_kernel_error"): _KERNEL,
    # -- dict payloads whose code is read from a registry-backed name ------------------------
    ("execution/_pipeline_finalize.py", "compute_post_validation"): (
        "failed_checks entries carry scan names that are registry symbols in `verification`"
    ),
    ("quality/report.py", "_collect_warnings"): (
        "diagnostic check names are registry symbols in `verification`"
    ),
}
# Functions whose return value is a registry symbol chosen from a table of registry symbols.
REGISTRY_HELPERS = frozenset({"strategy_code_for_unencryptable", "strategy_code_for_checksum"})

Key = tuple[str, str, str, str, str]


def _is_violation(site: scan.Site) -> bool:
    if site.value_class in ("registry", "none"):
        return False
    if site.value_class == "forward":
        return (site.file, site.function) not in ADAPTERS
    if site.value_class == "dynamic" and not site.codes:
        helper = site.expr.split("(")[0]
        if helper in REGISTRY_HELPERS and site.expr.endswith(")"):
            return False
        return (site.file, site.function) not in ADAPTERS
    return True


def violations(sites: list[scan.Site]) -> Counter[Key]:
    return Counter(
        (s.file, s.function, s.kind, s.family, s.value_class) for s in sites if _is_violation(s)
    )


def test_no_ad_hoc_reason_codes() -> None:
    found = violations(scan.scan())
    assert not found, (
        "ad-hoc reason code sites (use a decoy_engine.reasons symbol, or list a forwarding "
        "adapter with its rationale):\n" + "\n".join(f"  {k}" for k in sorted(found))
    )


def test_adapters_exist_and_are_used() -> None:
    sites = scan.scan()
    used = {(s.file, s.function) for s in sites if s.value_class in ("forward", "dynamic")}
    unused = sorted(set(ADAPTERS) - used)
    assert not unused, f"adapters that no longer forward a code: {unused}"
    for path, _ in ADAPTERS:
        assert (scan.SRC / path).is_file(), path


def test_registry_symbols_exist() -> None:
    """A registry chain must name a real symbol, not a typo that only fails at import."""
    import ast

    from decoy_engine import reasons

    bad: list[str] = []
    for path in scan.source_files():
        rel = path.relative_to(scan.SRC).as_posix()
        if rel.startswith(scan.REGISTRY_PREFIX):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                chain = scan._chain(node)
                if (
                    chain
                    and len(chain) == 3
                    and chain[0] == "reasons"
                    and chain[1] in set(reasons.namespaces())
                ):
                    if not hasattr(getattr(reasons, chain[1]), chain[2]):
                        bad.append(f"{rel}:{node.lineno} reasons.{chain[1]}.{chain[2]}")
    assert not bad, bad


# -- seeded mutants ---------------------------------------------------------------------------

_STUBS = {
    "errors.py": """
        class ExecutionError(Exception):
            def __init__(self, *, code, message=""):
                self.code = code
    """,
    "plan_errors.py": """
        class PlanCompileError(Exception):
            def __init__(self, code, path, message):
                self.code = code
    """,
}

_MUTANTS: dict[str, str] = {
    "literal": """
        from decoy_engine.errors import ExecutionError
        def f():
            raise ExecutionError(code="adhoc_literal")
    """,
    "aliased_constructor": """
        from decoy_engine.errors import ExecutionError as Boom
        def f():
            raise Boom(code="adhoc_alias")
    """,
    "positional_code": """
        from decoy_engine.plan_errors import PlanCompileError
        def f():
            raise PlanCompileError("adhoc_positional", None, "m")
    """,
    "f_string": """
        from decoy_engine.errors import ExecutionError
        def f(k):
            raise ExecutionError(code=f"adhoc_{k}")
    """,
    "helper_return": """
        from decoy_engine.errors import ExecutionError
        def pick():
            return "adhoc_helper"
        def f():
            raise ExecutionError(code=pick())
    """,
    "class_constant": """
        from decoy_engine.errors import ExecutionError
        class Mine(ExecutionError):
            code = "adhoc_class"
    """,
    "dict_payload": """
        def f():
            return {"code": "adhoc_payload"}
    """,
    "code_table": """
        _ROUTE_CODES = {"adhoc_a": "adhoc_b"}
    """,
    "module_constant": """
        MY_CODE = "adhoc_constant"
    """,
    "unchecked_forward": """
        from decoy_engine.errors import ExecutionError
        def f(code):
            raise ExecutionError(code=code)
    """,
}

_CLEAN = """
    from decoy_engine import reasons
    from decoy_engine.errors import ExecutionError
    MY_CODE = reasons.runtime.CYCLIC_WORK_ORDERING
    def f():
        raise ExecutionError(code=reasons.runtime.CYCLIC_WORK_ORDERING)
    def g():
        raise ExecutionError(code=MY_CODE)
    def h():
        return {"code": reasons.runtime.CYCLIC_WORK_ORDERING}
"""


def _tree(tmp_path: Path, name: str, body: str) -> Path:
    root = tmp_path / "decoy_engine"
    root.mkdir()
    (root / "__init__.py").write_text("")
    for stub_name, stub in _STUBS.items():
        (root / stub_name).write_text(textwrap.dedent(stub))
    (root / f"{name}.py").write_text(textwrap.dedent(body))
    return root


@pytest.mark.parametrize("name", sorted(_MUTANTS))
def test_each_smuggling_route_is_caught(tmp_path: Path, name: str) -> None:
    root = _tree(tmp_path, f"new_module_{name}", _MUTANTS[name])
    found = violations(scan.scan(root))
    assert any(k[0] == f"new_module_{name}.py" for k in found), (name, dict(found))


def test_registry_backed_code_is_clean(tmp_path: Path) -> None:
    root = _tree(tmp_path, "clean_module", _CLEAN)
    found = violations(scan.scan(root))
    assert not [k for k in found if k[0] == "clean_module.py"], dict(found)


def test_a_newly_added_module_is_scanned(tmp_path: Path) -> None:
    root = _tree(tmp_path, "brand_new", _MUTANTS["literal"])
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "deep.py").write_text(textwrap.dedent(_MUTANTS["f_string"]))
    files = {k[0] for k in violations(scan.scan(root))}
    assert {"brand_new.py", "pkg/deep.py"} <= files


def test_code_namespace_matches_the_raising_family() -> None:
    """A code raised by a family must be that family's registry namespace, not a neighbour's.

    The same wire string can exist in two namespaces (``plan`` and ``runtime``); a shared module
    constant would silently attach the wrong one to one of its users.
    """
    import _reason_baseline as baseline

    mismatched = []
    for site in scan.scan():
        if site.kind not in ("kw", "positional") or site.value_class != "registry":
            continue
        expected = baseline.FAMILY_NAMESPACE.get(site.family.split("(")[0])
        if expected and site.registry_ns not in (expected, ""):
            mismatched.append(f"{site.file}:{site.line} {site.family} -> {site.registry_ns}")
    assert not mismatched, mismatched


_REQUIRED_MUTANTS = {
    "aliased_constructor",
    "positional_code",
    "f_string",
    "helper_return",
    "class_constant",
    "dict_payload",
    "code_table",
}


def test_ci_collects_the_registry_guards() -> None:
    """The regression job collects ``tests/sentry``; the guards and every seeded mutant must exist."""
    ci = (scan.ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "tests/sentry" in ci
    guards = {
        "test_no_ad_hoc_reason_codes",
        "test_each_smuggling_route_is_caught",
        "test_a_newly_added_module_is_scanned",
        "test_code_namespace_matches_the_raising_family",
    }
    assert guards <= set(globals())
    assert set(_MUTANTS) >= _REQUIRED_MUTANTS
