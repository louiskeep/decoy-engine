"""C1b-i: truthful determinism metadata plus the inventory-completeness gate.

Plan: docs/plans/2026-10-04-c1b-i-seeded-nondet-categorical-everywhere.md, sections 4.2, 4.7,
5.6 and 5.10. Two checks here exist because a prose edit alone cannot be trusted:

* a structural check that the counts written in `docs/native/draw-site-inventory.md` equal
  the live `DRAW_SITES` metadata (a grep cannot catch a stale number);
* a scan that no active categorical-specific text still says "unseeded" or "differs run
  to run" (the label is a live route gate elsewhere, so a stale one is a correctness risk).
"""

from __future__ import annotations

import collections
import pathlib
import re

import pytest

from decoy_engine import kernel
from decoy_engine.execution.native._determinism_protocol import (
    DRAW_SITES,
    draw_site_by_id,
)
from decoy_engine.execution.native._draw_site_providers import (
    SourceKeyedHmacProvider,
    UnseededProvider,
    all_providers,
    provider_for,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "docs" / "native" / "draw-site-inventory.md"
SITE = "mask.categorical_nondeterministic"
MK = (0x0123456789).to_bytes(8, "big")


# ---- 5.6 metadata -----------------------------------------------------------------------


class TestSiteIsSeeded:
    def test_catalogue_entry_is_seeded_row_keyed_and_partitionable(self) -> None:
        site = draw_site_by_id(SITE)
        assert site.family == "source_keyed_hmac"
        assert site.entropy_root == "mask_key"
        assert site.identity == "row_index"
        assert site.partitionable is True
        assert "unseeded" not in (site.seed_derivation + site.notes + site.call_shape).lower()
        assert "derive_index" in site.seed_derivation
        assert "encode_int" in site.seed_derivation
        assert site.provider_version.startswith("seed_protocol_v")

    def test_provider_reproduces_the_oracle_draw_and_partitions(self) -> None:
        p = provider_for(SITE)
        assert isinstance(p, SourceKeyedHmacProvider)
        assert p.partitionable is True
        # Frozen literals from derive_index(MK, "ns", encode_int(g), pool_size=4).
        got = [p.partitioned_draw(MK, "ns", kernel.encode_int(g), pool_size=4) for g in range(6)]
        assert got == [2, 3, 1, 3, 0, 0]

    def test_only_the_identifier_site_remains_unseeded(self) -> None:
        unseeded = {sid for sid, p in all_providers().items() if isinstance(p, UnseededProvider)}
        assert unseeded == {"gen.identifier_nondeterministic"}
        assert draw_site_by_id("gen.identifier_nondeterministic").entropy_root == "none"

    def test_only_the_expected_sites_still_have_no_entropy_root(self) -> None:
        none_roots = {s.draw_site_id for s in DRAW_SITES if s.entropy_root == "none"}
        assert none_roots == {"gen.identifier_nondeterministic", "mask.formula"}

    def test_capabilities_row_does_not_call_the_variant_unseeded(self) -> None:
        from decoy_engine.execution.native._capabilities import capabilities_for

        cap = capabilities_for("categorical")
        assert "unseeded" not in cap.notes.lower()
        assert "position" in cap.notes.lower()


# ---- 4.7(b) structural count check --------------------------------------------------------


def _inventory_text() -> str:
    return INVENTORY.read_text(encoding="utf-8")


def _live_counts() -> tuple[int, int, int, dict[str, int]]:
    total = len(DRAW_SITES)
    part = sum(1 for s in DRAW_SITES if s.partitionable)
    fam = collections.Counter(s.family for s in DRAW_SITES)
    return total, part, total - part, dict(fam)


class TestInventoryCountsMatchLiveMetadata:
    def test_top_summary_counts(self) -> None:
        total, part, notpart, fam = _live_counts()
        text = _inventory_text()
        m = re.search(r"- (\d+) catalogued draw sites", text)
        assert m and int(m.group(1)) == total
        m = re.search(r"- (\d+) partitionable, (\d+) not\.", text)
        assert m and (int(m.group(1)), int(m.group(2))) == (part, notpart)
        m = re.search(r"Family breakdown:(.*?)\n- ", text, re.S)
        assert m
        doc_fam = {k: int(v) for k, v in re.findall(r"`(\w+)`\s+(\d+)", m.group(1))}
        assert doc_fam == fam

    def test_registry_aggregate_counts(self) -> None:
        total, part, notpart, _fam = _live_counts()
        text = re.sub(r"\s+", " ", _inventory_text())
        m = re.search(r"\((\d+) sites; (\d+) partitionable, (\d+) not\)", text)
        assert m and tuple(int(g) for g in m.groups()) == (total, part, notpart)

    def test_every_table_row_matches_live_family_and_partitionability(self) -> None:
        rows = re.findall(
            r"^\| ((?:gen|mask)\.\w+)\s+\| (\w+)\s+\| (yes|no)\s+\|", _inventory_text(), re.M
        )
        assert {r[0] for r in rows} == {s.draw_site_id for s in DRAW_SITES}
        for site_id, family, part in rows:
            live = draw_site_by_id(site_id)
            assert live.family == family, site_id
            assert (part == "yes") == live.partitionable, site_id

    def test_nondeterministic_categorical_section_describes_the_seeded_draw(self) -> None:
        text = _inventory_text()
        start = text.index("### mask.categorical_nondeterministic")
        section = text[start : text.index("\n### ", start + 5)]
        assert "unseeded" not in section.lower()
        assert "differs run to run" not in section
        assert "derive_index" in section and "encode_int" in section


# ---- 4.7(a) / 5.10 stale-prose gate --------------------------------------------------------

_STALE = re.compile(
    r"unseeded"
    r'|entropy_root="none"|entropy-root none'
    r"|differs run to run|two runs differ"
    r"|fresh generator|fresh `?default_rng|default_rng\(\)",
    re.I,
)
_CATEGORICAL = re.compile(r"categorical")

_SCOPE_DIRS = (
    "src/decoy_engine/execution",
    "tests/unit/execution",
    "tests/native",
    "tests/physical",
    "tests/parity",
)
_SCOPE_FILES = (
    "docs/strategies.md",
    "docs/determinism.md",
    "docs/native/draw-site-inventory.md",
    "docs/quality/mutation-ledgers/execution_strategies_categorical.md",
)
_SELF = pathlib.Path(__file__).resolve()
# These keep the legitimately unseeded sites (shuffle, identifier) and mention
# categorical only to say which path replaced the old behaviour.
_NOT_CATEGORICAL_SPECIFIC = {
    "tests/unit/execution/test_categorical_seeded_nondet.py",
    "tests/unit/execution/test_c1b_i_route_regression.py",
}


def _active_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for d in _SCOPE_DIRS:
        files += [p for p in (ROOT / d).rglob("*") if p.suffix in (".py", ".md") and p != _SELF]
    files += [ROOT / f for f in _SCOPE_FILES]
    return [p for p in files if str(p.relative_to(ROOT)) not in _NOT_CATEGORICAL_SPECIFIC]


def _unreleased_changelog() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    start = text.index("## [Unreleased]")
    nxt = re.search(r"^## \[", text[start + 5 :], re.M)
    return text[start : start + 5 + nxt.start()] if nxt else text[start:]


def _windows(text: str, radius: int = 1) -> list[tuple[int, str]]:
    """One `(line_number, surrounding_text)` per line that carries a stale phrase."""
    lines = text.splitlines()
    return [
        (i + 1, "\n".join(lines[max(0, i - radius) : i + radius + 1]))
        for i, line in enumerate(lines)
        if _STALE.search(line)
    ]


def _md_section_hits(name: str, text: str) -> list[str]:
    """Markdown sections whose heading names categorical and whose body says a stale phrase."""
    hits: list[str] = []
    for part in re.split(r"(?m)^(?=#{1,6} )", text):
        heading = part.splitlines()[0] if part else ""
        stale = _STALE.search(part)
        if _CATEGORICAL.search(heading) and stale:
            hits.append(f"{name}:[{heading.strip()}]: {stale.group(0)!r}")
    return hits


def _stale_categorical_hits() -> list[str]:
    hits: list[str] = []
    sources = [(str(p.relative_to(ROOT)), p.read_text(encoding="utf-8")) for p in _active_files()]
    sources.append(("CHANGELOG.md[Unreleased]", _unreleased_changelog()))
    for name, text in sources:
        if name.endswith(".md") or name.startswith("CHANGELOG"):
            hits += _md_section_hits(name, text)
        for line_no, window in _windows(text):
            if _CATEGORICAL.search(window):
                stale = _STALE.search(window)
                assert stale is not None
                hits.append(f"{name}:{line_no}: {stale.group(0)!r}")
    return hits


def test_no_active_categorical_text_calls_the_draw_unseeded() -> None:
    hits = _stale_categorical_hits()
    assert hits == [], "stale categorical 'unseeded'/'differs run to run' prose:\n" + "\n".join(
        hits
    )


def _section(path: str, heading: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    start = text.index(heading)
    nxt = text.find("\n### ", start + len(heading))
    return text[start : nxt if nxt != -1 else len(text)]


def test_capability_row_has_no_stale_unseeded_vector_prose() -> None:
    text = (ROOT / "src/decoy_engine/execution/native/_capabilities.py").read_text()
    assert "unseeded vector" not in text
    assert "excluded from the native route by its unseeded draw" not in text


def test_public_docs_describe_categorical_as_seeded() -> None:
    mask_section = _section("docs/strategies.md", "### categorical").lower()
    assert "differs run to run" not in mask_section
    assert "row position under the job seed" in mask_section
    determinism = (ROOT / "docs" / "determinism.md").read_text(encoding="utf-8")
    not_deterministic = determinism[determinism.index("## What is NOT deterministic") :]
    bullet = not_deterministic[: not_deterministic.index("- Profiling without a seed")]
    assert "categorical" not in bullet
    assert "Non-deterministic `categorical`" in determinism


@pytest.mark.parametrize(
    "path",
    [
        "src/decoy_engine/execution/_strategies/_categorical.py",
        "src/decoy_engine/execution/out_of_core/_compat.py",
        "src/decoy_engine/execution/_pipeline_multi_table.py",
    ],
)
def test_named_modules_no_longer_describe_a_numpy_draw(path: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8").lower()
    assert "numpy rng" not in text
    assert "differs run to run" not in text
