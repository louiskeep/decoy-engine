"""Coverage guard for the RNG draw-site inventory (native program, Task 0.1).

Proves the inventory is complete and internally consistent, and stays that
way: a new masking strategy, generation kind, or RNG-bearing source file that
is not catalogued fails a test here rather than silently escaping the
determinism protocol Task 0.3 builds on. The scan reads the SAME
``decoy_engine`` package that is imported, so it checks the live source tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import decoy_engine
from decoy_engine.config._tables import GENERATE_TYPES
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution.native._determinism_protocol import (
    DETERMINISTIC_NO_DRAW,
    DRAW_SITES,
    ENTROPY_ROOTS,
    FAMILIES,
    GEN_KIND_TO_SITE,
    IDENTITIES,
    MASK_STRATEGY_TO_SITE,
    PROVIDER_IDENTIFIER_SITES,
    DrawSite,
)
from decoy_engine.providers_v2._registry import get_default_registry
from decoy_engine.providers_v2.identifiers import (
    CusipAdapter,
    EinAdapter,
    IbanAdapter,
    Icd10Adapter,
    MrnAdapter,
    NdcAdapter,
    NpiAdapter,
    PanAdapter,
    SsnAdapter,
)

_IDENTIFIER_ADAPTERS = (
    SsnAdapter,
    NpiAdapter,
    EinAdapter,
    IbanAdapter,
    PanAdapter,
    Icd10Adapter,
    MrnAdapter,
    NdcAdapter,
    CusipAdapter,
)

REQUIRED_FAMILIES = {
    "numpy_pcg64",
    "python_mt19937",
    "per_row_reseed",
    "source_keyed_hmac",
    "per_group_stream",
    "faker_seed_instance",
    "gen_derive_context",
}

_ALL_IDS = {s.draw_site_id for s in DRAW_SITES}


# --------------------------------------------------------------------------
# Spec Step 1 assertions.
# --------------------------------------------------------------------------
def test_all_known_families_catalogued() -> None:
    assert {s.family for s in DRAW_SITES} >= REQUIRED_FAMILIES


def test_shuffle_and_statistical_and_faker_present() -> None:
    sites = {s.call_site.split(":")[0].split("/")[-1] for s in DRAW_SITES}
    assert "_shuffle.py" in sites  # numpy permutation
    assert "_sample.py" in sites  # statistical per-row reseed
    assert "synthesize.py" in sites  # sequential random.Random + Faker


# --------------------------------------------------------------------------
# Internal consistency of the catalog.
# --------------------------------------------------------------------------
def test_draw_site_ids_unique() -> None:
    ids = [s.draw_site_id for s in DRAW_SITES]
    assert len(ids) == len(set(ids)), "duplicate draw_site_id in DRAW_SITES"


def test_every_required_field_populated_and_from_allowed_enum() -> None:
    for s in DRAW_SITES:
        assert isinstance(s, DrawSite)
        # Non-empty text fields.
        for name in (
            "draw_site_id",
            "family",
            "call_site",
            "seed_derivation",
            "api_operation",
            "call_shape",
            "null_draw_behavior",
            "config_fingerprint_source",
            "provider_version",
        ):
            value = getattr(s, name)
            assert isinstance(value, str) and value.strip(), f"{s.draw_site_id}: empty {name}"
        # Enum-constrained fields.
        assert s.family in FAMILIES, f"{s.draw_site_id}: family {s.family!r}"
        assert s.entropy_root in ENTROPY_ROOTS, f"{s.draw_site_id}: entropy_root {s.entropy_root!r}"
        assert s.identity in IDENTITIES, f"{s.draw_site_id}: identity {s.identity!r}"
        # Typed flags.
        assert isinstance(s.consumes_variable_draws, bool)
        assert isinstance(s.partitionable, bool)
        assert isinstance(s.uncertain, bool)
        # call_site is "module:line".
        assert re.fullmatch(r".+\.py:\d+", s.call_site), f"{s.draw_site_id}: call_site shape"


def test_unseeded_sites_are_not_partitionable() -> None:
    # A genuinely unseeded (entropy_root='none') site cannot be reproduced at
    # all, so it can never be partition-reproducible.
    for s in DRAW_SITES:
        if s.entropy_root == "none":
            assert not s.partitionable, f"{s.draw_site_id}: unseeded but marked partitionable"


# --------------------------------------------------------------------------
# Spec Step 5: cross-check against the LIVE registries (not copied constants).
# --------------------------------------------------------------------------
def test_mask_strategy_map_covers_live_registry_exactly() -> None:
    live = set(SCALAR_HANDLERS)
    mapped = set(MASK_STRATEGY_TO_SITE)
    missing = live - mapped
    extra = mapped - live
    assert not missing, f"masking strategies not catalogued: {sorted(missing)}"
    assert not extra, f"MASK_STRATEGY_TO_SITE names not in the live registry: {sorted(extra)}"


def test_gen_kind_map_covers_live_registry_exactly() -> None:
    live = set(GENERATE_TYPES)
    mapped = set(GEN_KIND_TO_SITE)
    missing = live - mapped
    extra = mapped - live
    assert not missing, f"generation kinds not catalogued: {sorted(missing)}"
    assert not extra, f"GEN_KIND_TO_SITE names not in the live registry: {sorted(extra)}"


def test_every_map_target_resolves_to_a_site_or_the_no_draw_sentinel() -> None:
    for mapping, label in (
        (MASK_STRATEGY_TO_SITE, "mask"),
        (GEN_KIND_TO_SITE, "gen"),
        (PROVIDER_IDENTIFIER_SITES, "provider"),
    ):
        for name, target in mapping.items():
            if target == DETERMINISTIC_NO_DRAW:
                continue
            assert target in _ALL_IDS, f"{label} {name!r} maps to unknown draw_site_id {target!r}"


def test_provider_identifier_map_covers_live_registry_exactly() -> None:
    # Enumerate every registered provider whose adapter is a synthetic-identifier
    # adapter (each carries the derive_value + unseeded default_rng draws). A new
    # identifier adapter registered without a catalog entry fails here.
    reg = get_default_registry()
    live_identifier_providers = {
        name
        for name in reg.known_providers()
        if isinstance(reg.get_adapter(name), _IDENTIFIER_ADAPTERS)
    }
    mapped = set(PROVIDER_IDENTIFIER_SITES)
    missing = live_identifier_providers - mapped
    extra = mapped - live_identifier_providers
    assert not missing, f"identifier providers not catalogued: {sorted(missing)}"
    assert not extra, f"PROVIDER_IDENTIFIER_SITES names not registered: {sorted(extra)}"
    assert live_identifier_providers, "expected identifier providers in the default registry"


# --------------------------------------------------------------------------
# Spec Steps 3-5: a real static scan. Every RNG-bearing source file under the
# masked/generated-output directories must be represented in the catalog (as a
# call_site or a mirror) or explicitly allowlisted as non-output plumbing. A
# brand-new draw site therefore fails this test until it is catalogued.
# --------------------------------------------------------------------------
_PKG_DIR = Path(decoy_engine.__file__).resolve().parent

# Directories where a draw produces masked or generated OUTPUT. providers_v2
# is included so a new provider adapter with a new RNG draw is seen here.
_SCAN_ROOTS = (
    "execution",
    "generation",
    "generators",
    "transforms",
    "kernel",
    "reference_tables",
    "providers_v2",
)

# Call-level tokens that indicate an actual randomness draw. Includes the three
# keyed primitives (derive / derive_index / derive_value), the numpy and Python
# RNG constructors, Faker.seed_instance, the whole-column numpy ops, and the
# Mimesis Generic() seeded constructor. Prose is stripped before matching (see
# _strip_prose) so a docstring mention of `derive(...)` does not count.
_DRAW_TOKEN = re.compile(
    r"np\.random\.default_rng\(|np\.random\.RandomState\(|random\.Random\(|"
    r"\.seed_instance\(|\.permutation\(|\.shuffle\(|\.choices\(|"
    r"derive_index\(|derive_value\(|\bderive\(|Generic\("
)

_TRIPLE_QUOTED = re.compile(r"(\"\"\".*?\"\"\"|'''.*?''')", re.DOTALL)


def _strip_prose(text: str) -> str:
    """Remove triple-quoted blocks and line comments so the scan matches code.

    Bare `derive(` also appears in docstrings and comments describing the keyed
    primitive; stripping prose keeps the scan matching real call sites, not
    documentation. Single-line string literals are intentionally left in (a
    handful are handled by the allowlist), since removing them reliably needs a
    real tokenizer.
    """
    without_docstrings = _TRIPLE_QUOTED.sub("", text)
    return "\n".join(re.sub(r"#.*$", "", line) for line in without_docstrings.splitlines())


# RNG-token-bearing files that are NOT masked/generated-output draw sites:
# execution orchestration that only threads seeds, chunk-profiling that draws a
# fixed Random(0) over sampling, a diagnostic message string that quotes
# `derive(...)`, and this inventory module itself (whose data literals quote the
# very call shapes the scan looks for).
_ALLOWLIST = frozenset(
    {
        "execution/_chunked.py",
        "execution/_chunked_fk.py",  # a log-message string quotes "derive(seed, ns, value)"
        "execution/_chunked_profile.py",
        "execution/_pipeline.py",
        "execution/native/_determinism_protocol.py",
        # Task 0.3 protocol plumbing: reproduces the catalogued draws off the
        # hot path (one provider per DRAW_SITES entry). Not a new output site.
        "execution/native/_draw_site_providers.py",
        # Task 0.4 crypto contract: reference kernels call derive/fpe_encrypt_value
        # to reproduce shipped KEYED derivation, which is deterministic, not an RNG
        # output draw. Not a new draw site.
        "execution/native/_crypto_ext.py",
    }
)


def _catalogued_paths() -> set[str]:
    paths: set[str] = set()
    for s in DRAW_SITES:
        for entry in (s.call_site, *s.mirror_call_sites):
            paths.add(entry.rsplit(":", 1)[0])
    return paths


def _scan_rng_files() -> set[str]:
    found: set[str] = set()
    for root in _SCAN_ROOTS:
        base = _PKG_DIR / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if _DRAW_TOKEN.search(_strip_prose(text)):
                found.add(path.relative_to(_PKG_DIR).as_posix())
    return found


def test_static_scan_finds_no_uncatalogued_draw_site() -> None:
    known = _catalogued_paths() | _ALLOWLIST
    scanned = _scan_rng_files()
    uncatalogued = sorted(scanned - known)
    assert not uncatalogued, (
        "RNG-bearing source files not represented in DRAW_SITES (add a catalog "
        f"entry or, if not an output draw, the allowlist): {uncatalogued}"
    )


def test_scan_actually_matched_the_known_output_files() -> None:
    # Guard against a scan that silently matches nothing (e.g. a broken regex or
    # wrong root): the shuffle, statistical, and synthesize files must be seen.
    scanned = _scan_rng_files()
    for expected in (
        "execution/_strategies/_shuffle.py",
        "generation/statistical/_sample.py",
        "generation/synthesize.py",
        "providers_v2/identifiers/_ssn.py",
    ):
        assert expected in scanned, f"static scan missed {expected}"


def test_catalogued_call_sites_point_at_real_files() -> None:
    # Every call_site file (not the drifting line number) must exist in the tree.
    for s in DRAW_SITES:
        for entry in (s.call_site, *s.mirror_call_sites):
            rel = entry.rsplit(":", 1)[0]
            assert (_PKG_DIR / rel).is_file(), f"{s.draw_site_id}: missing file {rel}"
