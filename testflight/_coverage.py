"""Suite-level strategy-coverage guard for the test-flight suite.

Implements check_suite_strategy_coverage (plan section 6.10) and the
per-job check_job_strategy_coverage (validates declared keys are real).

Every coverage axis reads a LIVE engine registry, not a static snapshot
(TH-1.2 / P0-2):
  - strategies   -> execution._strategies.SCALAR_HANDLERS
  - validators   -> validators._registry._REGISTRY
  - checksums    -> checksums._VALIDATORS
  - generate types -> config._tables.GENERATE_TYPES
Adding an entry to any of these registries without adding it to a job manifest
OR the matching allowlist below FAILS the suite (anti-rot).

Allowlisted strategies (each with a specific, reviewable reason):
  formula   -- superseded by the `derived` strategy (SP-10). The `derived`
               strategy uses the same closed-grammar expression language and is
               the documented path for row-context expressions. No job uses the
               older `formula` handler; all expression-based masking uses
               `derived`.
  nested    -- requires a struct-typed source column. None of the three jobs in
               scope carry struct-typed columns. Will be added when a
               struct-column test data model is introduced.

All other SCALAR_HANDLERS keys must be exercised by at least one job manifest
OR this module will raise AssertionError naming the uncovered strategy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Documented allowlists
# ---------------------------------------------------------------------------

# Strategy keys in SCALAR_HANDLERS that are intentionally NOT exercised by
# the current three jobs. Each entry has a specific, reviewable reason.
# This is NOT a catch-all: every entry was deliberated.
_STRATEGY_ALLOWLIST: dict[str, str] = {
    "formula": (
        "superseded by the `derived` strategy (SP-10); all expression-based "
        "masking in the current jobs uses `derived` with the same closed-grammar "
        "language; `formula` is a legacy handler retained for compatibility"
    ),
    "nested": (
        "requires a struct-typed (nested) source column; none of the current "
        "three test jobs carry struct columns; will be added when a "
        "struct-column test data model is introduced"
    ),
    "group_key": (
        "SP-10c group-aware strategy; exercised end-to-end by "
        "tests/unit/transforms/test_group_key.py; a dedicated test-flight job "
        "exercising the group-aware strategies (group_key/grouped_series/"
        "windowed_date) is a tracked backlog item"
    ),
    "grouped_series": (
        "SP-10c group-aware strategy; exercised by "
        "tests/unit/transforms/test_grouped_series.py; dedicated test-flight "
        "coverage is a tracked backlog item (see group_key)"
    ),
    "windowed_date": (
        "SP-10c group-aware strategy; exercised by "
        "tests/unit/transforms/test_windowed_date.py; dedicated test-flight "
        "coverage is a tracked backlog item (see group_key)"
    ),
}

# Generate-column types (from the live config._tables.GENERATE_TYPES set,
# derived from the GenerateColumnConfig.type Literal) that are intentionally
# not exercised by the current three jobs.
_GENERATE_TYPE_ALLOWLIST: dict[str, str] = {
    "reference": (
        "requires a reference pool table configured alongside the generate table; "
        "no current job has a reference-pool generate column; will be added in "
        "a future job that exercises the lookup-reference generation path"
    ),
    "statistical": (
        "similar distribution coverage is provided by the formula+gauss path in "
        "Job C synthetic_events; the statistical type uses SDV-style parameterized "
        "distributions and will be explicitly exercised in a future job (TH-3.2)"
    ),
    "group_key": (
        "SP-10c group-aware generate type; exercised end-to-end by "
        "tests/unit/transforms/test_group_key.py; a dedicated test-flight job "
        "composing the group-aware generate trio inside run_pipeline is a tracked "
        "backlog item (TH-3.2)"
    ),
    "grouped_series": (
        "SP-10c group-aware generate type; exercised by "
        "tests/unit/transforms/test_grouped_series.py; dedicated test-flight "
        "coverage is a tracked backlog item (TH-3.2; see group_key)"
    ),
    "windowed_date": (
        "SP-10c group-aware generate type; exercised by "
        "tests/unit/transforms/test_windowed_date.py; dedicated test-flight "
        "coverage is a tracked backlog item (TH-3.2; see group_key)"
    ),
}

# Checksum schemes from decoy_engine.checksums that are not exercised by a job.
_CHECKSUM_SCHEME_ALLOWLIST: dict[str, str] = {
    "iban": (
        "IBAN is a banking identifier not present in any of the three current test "
        "domains (healthcare, retail, HR); will be added in a future financial job"
    ),
    "vin": (
        "VIN is a vehicle identifier not present in the current test domains; "
        "will be added in a future automotive or fleet-management job"
    ),
    "isbn13": (
        "ISBN-13 is a book/media identifier not present in the current test "
        "domains; will be added in a future media-catalog job"
    ),
    "ean13": (
        "EAN-13 is a retail barcode scheme not used in the current three jobs "
        "(Job B retail uses luhn for PAN, not EAN-13); will be added when a "
        "barcode or product-catalog column is introduced"
    ),
    "gtin": (
        "GTIN is a global trade item number; not present in the current test "
        "domains; will be added in a future supply-chain or product-catalog job"
    ),
}

# Validator names from validators/_registry.py that are not exercised end-to-end.
# leak_check is NOT allowlisted: it is exercised by Job A (members ssn/npi/mrn),
# the strongest structural guard against the value-changing-passthrough class.
_VALIDATOR_ALLOWLIST: dict[str, str] = {
    "npi": (
        "npi validator would fire on an NPI column with invalid check-digit rows; "
        "NPI is already tested at the checksum level (fpe+npi checksum in Job A); "
        "the full validator end-to-end path will be added in a future job that "
        "plants invalid-NPI rows for quarantine testing"
    ),
    "iban": (
        "iban validator requires an IBAN column not present in the current test "
        "domains; will be added alongside the IBAN checksum scheme in a future "
        "financial job"
    ),
    "vin": (
        "vin validator requires a VIN column not present in the current test "
        "domains; will be added alongside the VIN checksum scheme in a future "
        "automotive job"
    ),
    "regex_match": (
        "generic operator-supplied validator (params.pattern); asserts output "
        "values match an operator regex. Not tied to a domain identifier in the "
        "current three jobs; end-to-end coverage (a job declaring a pattern that "
        "the masked output must satisfy) is a tracked backlog item (TH-3)"
    ),
    "column_in_set": (
        "generic operator-supplied validator (params.allowed_values); asserts "
        "output values belong to an allowed set. Not tied to a domain identifier "
        "in the current three jobs; end-to-end coverage is a tracked backlog item "
        "(TH-3)"
    ),
    "parent_window_respected": (
        "relationship validator that pairs with the windowed_date generate "
        "strategy (child date within parent window). No current job drives a "
        "windowed_date parent/child edge; covered end-to-end by the group-aware "
        "Job D backlog item (TH-3.2)"
    ),
    "reconciliation_holds": (
        "relationship validator that reconciles a parent aggregate against its "
        "child rows across an FK edge. Job A's derived_aggregate is a single-table "
        "sum (no parent/child reconciliation edge); a job driving cross-table "
        "reconciliation is a tracked backlog item (TH-3.2)"
    ),
}

# Expression builtins that are intentionally NOT checked for coverage.
# Only case_when and gauss are in-scope for the current three jobs.
_BUILTINS_IN_SCOPE: frozenset[str] = frozenset({"case_when", "gauss"})

# Code-set corpora that must all be covered (icd10/hcpcs/ndc/mcc) -- no allowlist.
_REQUIRED_CORPORA: frozenset[str] = frozenset({"icd10", "hcpcs", "ndc", "mcc"})


# ---------------------------------------------------------------------------
# Live-registry reads (P0-2 / TH-1.2)
# ---------------------------------------------------------------------------
# The coverage axes are derived from the engine's LIVE registries, not static
# snapshots, exactly as the scalar-strategy axis reads SCALAR_HANDLERS. Adding
# a validator / checksum scheme / generate type to its registry without a job
# or an allowlist entry therefore fails the suite guard (anti-rot). Each read
# is a function (not a module-load-time constant) so a test can monkeypatch the
# underlying registry and prove the guard reads it at call time.


def _live_validators() -> set[str]:
    """Return the live validator-name set from validators._registry._REGISTRY."""
    from decoy_engine.validators._registry import _REGISTRY

    return set(_REGISTRY.keys())


def _live_checksum_schemes() -> set[str]:
    """Return the live checksum-scheme set from checksums._VALIDATORS."""
    from decoy_engine.checksums import _VALIDATORS

    return set(_VALIDATORS.keys())


def _live_generate_types() -> set[str]:
    """Return the live generate-type set from config._tables.GENERATE_TYPES.

    GENERATE_TYPES is derived from the GenerateColumnConfig.type Literal (the
    validation authority the generation.synthesize dispatch mirrors), so it is
    the live source for the generate-type axis.
    """
    from decoy_engine.config import _tables

    return set(_tables.GENERATE_TYPES)


# ---------------------------------------------------------------------------
# Per-job check: validate declared strategy keys exist in SCALAR_HANDLERS
# ---------------------------------------------------------------------------


def check_job_strategy_coverage(job_name: str, declared: list[str]) -> None:
    """Assert all declared strategies exist in SCALAR_HANDLERS (no typos).

    This per-job check runs at suite start for each job. It validates that
    every strategy name in strategy_coverage is a real key in SCALAR_HANDLERS
    (not a typo) so a manifest typo fails loudly rather than silently passing.

    Note: "from_parent" is a column marker (not a SCALAR_HANDLER key); it is
    skipped if present in the declared list.

    Args:
        job_name: Job name for error messages.
        declared: List of strategy names from InvariantSpec.strategy_coverage.

    Raises:
        AssertionError: If any declared strategy is not in SCALAR_HANDLERS.
    """
    from decoy_engine.execution._strategies import SCALAR_HANDLERS

    known = set(SCALAR_HANDLERS.keys())
    # "from_parent" is a FK marker, not a registered strategy handler.
    non_handlers = {"from_parent"}
    unknown = [s for s in declared if s not in known and s not in non_handlers]
    assert not unknown, (
        f"[{job_name}] strategy_coverage: declared strategy/strategies not in "
        f"SCALAR_HANDLERS: {sorted(unknown)}. "
        f"Check for typos in the manifest strategy_coverage list. "
        f"Live SCALAR_HANDLERS keys: {sorted(known)}."
    )


# ---------------------------------------------------------------------------
# Suite-level guard: coverage union == SCALAR_HANDLERS - allowlist
# ---------------------------------------------------------------------------


def check_suite_strategy_coverage(
    all_manifests: list[Any],
) -> str:
    """Suite-level coverage guard: union of declared strategies vs live registry.

    Reads SCALAR_HANDLERS live, computes the union of strategy_coverage across
    all manifests, and asserts it equals SCALAR_HANDLERS.keys() minus the
    documented allowlist. Also checks generate-column type coverage, checksum
    scheme coverage, code_set corpora coverage, validator coverage, and
    expression builtin coverage.

    Because the guard reads the LIVE registry, adding a new strategy without
    adding it to a job manifest or the allowlist FAILS the suite.

    Args:
        all_manifests: List of FlightManifest objects (one per discovered job).

    Returns:
        A short summary string of covered vs allowlisted strategies.

    Raises:
        AssertionError: If any live strategy is uncovered and not allowlisted,
            or if any covered entity is unknown to the registry.
    """
    from decoy_engine.execution._strategies import SCALAR_HANDLERS

    live_keys: set[str] = set(SCALAR_HANDLERS.keys())

    # -----------------------------------------------------------------------
    # 1. SCALAR_HANDLERS strategy coverage
    # -----------------------------------------------------------------------
    declared_union: set[str] = set()
    for m in all_manifests:
        for s in m.invariants.strategy_coverage:
            if s != "from_parent":  # FK marker, not a handler
                declared_union.add(s)

    expected_covered = live_keys - set(_STRATEGY_ALLOWLIST.keys())
    uncovered = expected_covered - declared_union
    extra_declared = declared_union - live_keys

    assert not uncovered, (
        f"strategy_coverage guard: {len(uncovered)} live strategy/strategies "
        f"uncovered and not allowlisted: {sorted(uncovered)}. "
        f"Either add to a job's strategy_coverage list OR add to "
        f"_STRATEGY_ALLOWLIST in testflight/_coverage.py with a specific reason. "
        f"This guard reads the LIVE SCALAR_HANDLERS registry; a new strategy was "
        f"added without being exercised or allowlisted."
    )
    assert not extra_declared, (
        f"strategy_coverage guard: {len(extra_declared)} declared strategy/strategies "
        f"not in SCALAR_HANDLERS: {sorted(extra_declared)}. "
        f"Check for typos in manifest strategy_coverage lists or "
        f"stale entries from a deleted handler."
    )

    # -----------------------------------------------------------------------
    # 2. Generate-column type coverage
    # -----------------------------------------------------------------------
    gen_declared: set[str] = set()
    for m in all_manifests:
        for t in m.config.get("tables", []):
            if isinstance(t, dict):
                for gcol in t.get("generate_columns", []):
                    if isinstance(gcol, dict) and "type" in gcol:
                        gen_declared.add(gcol["type"])

    gen_expected = _live_generate_types() - set(_GENERATE_TYPE_ALLOWLIST.keys())
    gen_uncovered = gen_expected - gen_declared
    assert not gen_uncovered, (
        f"strategy_coverage guard: generate-column type(s) uncovered and not "
        f"allowlisted: {sorted(gen_uncovered)}. "
        f"Either add a job that uses this generate type or add to "
        f"_GENERATE_TYPE_ALLOWLIST with a specific reason."
    )

    # -----------------------------------------------------------------------
    # 3. Checksum scheme coverage
    # -----------------------------------------------------------------------
    checksum_declared: set[str] = set()
    for m in all_manifests:
        for cs in m.invariants.checksums:
            checksum_declared.add(cs.scheme)

    checksum_expected = _live_checksum_schemes() - set(_CHECKSUM_SCHEME_ALLOWLIST.keys())
    checksum_uncovered = checksum_expected - checksum_declared
    assert not checksum_uncovered, (
        f"strategy_coverage guard: checksum scheme(s) uncovered and not "
        f"allowlisted: {sorted(checksum_uncovered)}. "
        f"Either add a job with this checksum scheme or add to "
        f"_CHECKSUM_SCHEME_ALLOWLIST with a specific reason."
    )

    # -----------------------------------------------------------------------
    # 4. Code-set corpora coverage (all 4 required: icd10, hcpcs, ndc, mcc)
    # -----------------------------------------------------------------------
    corpora_declared: set[str] = set()
    for m in all_manifests:
        for t in m.config.get("tables", []):
            if isinstance(t, dict):
                for col in t.get("columns", []):
                    if isinstance(col, dict):
                        pc = col.get("provider_config", {})
                        if isinstance(pc, dict) and "code_set" in pc:
                            corpora_declared.add(pc["code_set"])

    corpora_uncovered = _REQUIRED_CORPORA - corpora_declared
    assert not corpora_uncovered, (
        f"strategy_coverage guard: code_set corpus/corpora uncovered: "
        f"{sorted(corpora_uncovered)}. "
        f"All four corpora (icd10, hcpcs, ndc, mcc) must appear in at least "
        f"one job's code_set column. Add a column or job that uses each corpus."
    )

    # -----------------------------------------------------------------------
    # 5. Validator coverage
    # -----------------------------------------------------------------------
    validators_declared: set[str] = set()
    for m in all_manifests:
        for v in m.config.get("validators", []):
            if isinstance(v, dict) and "name" in v:
                validators_declared.add(v["name"])

    val_expected = _live_validators() - set(_VALIDATOR_ALLOWLIST.keys())
    val_uncovered = val_expected - validators_declared
    assert not val_uncovered, (
        f"strategy_coverage guard: validator(s) uncovered and not allowlisted: "
        f"{sorted(val_uncovered)}. "
        f"Either add a job that fires this validator or add to "
        f"_VALIDATOR_ALLOWLIST with a specific reason."
    )

    # -----------------------------------------------------------------------
    # 6. Expression builtin coverage (case_when + gauss, in-scope only)
    # -----------------------------------------------------------------------
    builtins_declared: set[str] = set()
    for m in all_manifests:
        for cs in m.invariants.computed_columns:
            if "case_when" in cs.formula:
                builtins_declared.add("case_when")
            if "gauss" in cs.formula:
                builtins_declared.add("gauss")
        # Also scan generate_columns formulas in the config.
        for t in m.config.get("tables", []):
            if isinstance(t, dict):
                for gcol in t.get("generate_columns", []):
                    if isinstance(gcol, dict):
                        f_str = gcol.get("formula", "")
                        if "case_when" in f_str:
                            builtins_declared.add("case_when")
                        if "gauss" in f_str:
                            builtins_declared.add("gauss")

    builtins_uncovered = _BUILTINS_IN_SCOPE - builtins_declared
    assert not builtins_uncovered, (
        f"strategy_coverage guard: expression builtin(s) not exercised in any "
        f"computed_column formula: {sorted(builtins_uncovered)}. "
        f"Add a job that uses this builtin in a derived or generate column."
    )

    # -----------------------------------------------------------------------
    # Build summary
    # -----------------------------------------------------------------------
    covered = sorted(declared_union)
    allowlisted = sorted(_STRATEGY_ALLOWLIST.keys())
    gen_covered = sorted(gen_declared)
    gen_allowlisted = sorted(_GENERATE_TYPE_ALLOWLIST.keys())
    val_covered = sorted(validators_declared)
    val_allowlisted = sorted(_VALIDATOR_ALLOWLIST.keys())

    # Live registry totals (P0-2 / TH-1.2): the guard reports the true live
    # counts so a summary reader can confirm coverage+allowlist == live.
    live_gen = len(_live_generate_types())
    live_checksums = len(_live_checksum_schemes())
    live_validators = len(_live_validators())

    return (
        f"strategies live={len(live_keys)} covered={len(covered)} "
        f"allowlisted={len(allowlisted)} ({','.join(covered)}) | "
        f"generate_types live={live_gen} covered={len(gen_covered)} "
        f"allowlisted={len(gen_allowlisted)} | "
        f"checksums live={live_checksums} covered={len(checksum_declared)} "
        f"allowlisted={len(_CHECKSUM_SCHEME_ALLOWLIST)} | "
        f"corpora covered={len(corpora_declared)}/4 | "
        f"validators live={live_validators} covered={len(val_covered)} "
        f"allowlisted={len(val_allowlisted)} | "
        f"builtins covered={sorted(builtins_declared)}"
    )
