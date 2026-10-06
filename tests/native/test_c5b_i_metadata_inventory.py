"""C5b-i: truthful determinism metadata for non-deterministic Faker.

Plan: docs/plans/2026-10-05-c5b-i-positional-nondet-faker.md, sections 3d, 3e and 5 (test 9).

Non-deterministic REUSE Faker is position-keyed on `job_seed`: it gets its own catalogued
site, `mask.faker_nondeterministic`, while the strategy-level mapping `faker -> mask.faker`
stays on the deterministic site (it drives the capability `key_source` and the native
Faker key binding). UNIQUE, MATCH and SCALE keep the whole-column numpy draw and say so.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from decoy_engine import kernel
from decoy_engine.determinism import derive_index
from decoy_engine.execution.native._determinism_protocol import (
    DRAW_SITES,
    MASK_STRATEGY_TO_SITE,
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
SITE = "mask.faker_nondeterministic"
JOB = (0x0123456789).to_bytes(8, "big")


class TestSiteIsSeededAndPositionKeyed:
    def test_catalogue_entry(self) -> None:
        site = draw_site_by_id(SITE)
        assert site.family == "source_keyed_hmac"
        assert site.entropy_root == "job_seed"
        assert site.identity == "row_index"
        assert site.partitionable is True
        assert site.provider_version.startswith("seed_protocol_v")
        text = (site.seed_derivation + site.notes + site.call_shape).lower()
        assert "unseeded" not in text
        assert "derive_index" in site.seed_derivation
        assert "encode_int" in site.seed_derivation
        assert "faker-nd" in site.seed_derivation
        assert "job_seed" in site.seed_derivation

    def test_provider_reproduces_the_oracle_index_and_partitions(self) -> None:
        p = provider_for(SITE)
        assert isinstance(p, SourceKeyedHmacProvider)
        assert p.partitionable is True
        ns = "faker-nd/1:t/1:c"
        got = [p.partitioned_draw(JOB, ns, kernel.encode_int(g), pool_size=64) for g in range(12)]
        assert got == [25, 4, 53, 24, 41, 33, 42, 28, 54, 47, 35, 19]
        assert got == [derive_index(JOB, ns, kernel.encode_int(g), pool_size=64) for g in range(12)]

    def test_the_site_is_not_unseeded(self) -> None:
        unseeded = {sid for sid, p in all_providers().items() if isinstance(p, UnseededProvider)}
        assert SITE not in unseeded

    def test_strategy_mapping_stays_on_the_deterministic_site(self) -> None:
        assert MASK_STRATEGY_TO_SITE["faker"] == "mask.faker"

    def test_the_deterministic_site_no_longer_claims_to_cover_the_numpy_draw(self) -> None:
        site = draw_site_by_id("mask.faker")
        text = (site.seed_derivation + site.api_operation + site.notes).lower()
        assert SITE in text
        assert "default_rng off job_seed" not in text
        # The call site moved with the handler edit; it must name a real line of the file.
        path, line = site.call_site.rsplit(":", 1)
        lines = (ROOT / "src" / "decoy_engine" / path).read_text().splitlines()
        assert "PoolSampler" in lines[int(line) - 1] or "sample(" in lines[int(line) - 1]

    def test_new_site_call_site_names_a_real_line(self) -> None:
        site = draw_site_by_id(SITE)
        path, line = site.call_site.rsplit(":", 1)
        lines = (ROOT / "src" / "decoy_engine" / path).read_text().splitlines()
        assert 1 <= int(line) <= len(lines)

    def test_capabilities_row_describes_both_selections(self) -> None:
        from decoy_engine.execution.native._capabilities import capabilities_for

        notes = capabilities_for("faker").notes.lower()
        assert "unseeded" not in notes
        assert "position" in notes
        assert "deterministic" in notes


class TestInventoryDocument:
    def test_a_section_and_a_table_row_exist_for_the_new_site(self) -> None:
        text = INVENTORY.read_text(encoding="utf-8")
        start = text.index(f"### {SITE}")
        nxt = text.find("\n### ", start + 5)
        section = text[start : nxt if nxt != -1 else len(text)]
        assert "unseeded" not in section.lower()
        assert "differs run to run" not in section
        assert "derive_index" in section and "encode_int" in section
        assert "faker-nd" in section
        rows = re.findall(r"^\| ((?:gen|mask)\.\w+)\s+\| (\w+)\s+\| (yes|no)\s+\|", text, re.M)
        by_id = {r[0]: r for r in rows}
        assert by_id[SITE][1:] == ("source_keyed_hmac", "yes")

    def test_mask_faker_section_no_longer_claims_the_numpy_draw_for_every_nondeterministic_mode(
        self,
    ) -> None:
        text = INVENTORY.read_text(encoding="utf-8")
        start = text.index("### mask.faker\n")
        section = text[start : text.index("\n### ", start + 5)]
        assert (
            "Non-deterministic mode\nselects with `np.random.default_rng` off `job_seed` and is not partitionable."
            not in section
        )
        assert SITE in section

    def test_counts_match_the_live_catalogue(self) -> None:
        # The generic structural checks live in test_c1b_i_metadata_inventory.py; this pins
        # the new site into them explicitly.
        assert SITE in {s.draw_site_id for s in DRAW_SITES}
        assert SITE in all_providers()


# ---- stale-prose gate (Faker) ---------------------------------------------------------------

# `unseeded` as a word; the `UnseededProvider` class name is not a claim about Faker.
_STALE = re.compile(r"unseeded(?![a-z])|differs run to run|two runs differ|chunk-variant", re.I)
_FAKER = re.compile(r"faker", re.I)
_SCOPE_DIRS = ("src/decoy_engine/execution",)
_SCOPE_FILES = (
    "docs/strategies.md",
    "docs/determinism.md",
    "docs/native/draw-site-inventory.md",
)
_SELF = pathlib.Path(__file__).resolve()

# Lines that mention Faker and an unseeded generator for reasons that have nothing to do with
# the Faker masking draw: the identifier adapters (their own unseeded site), the text-mask and
# generation Faker seed-instance sites, and the shuffle strategy. Each is matched by exact
# substring so a stale Faker claim cannot hide behind a broad allowance.
_ALLOWED = (
    "gen.identifier_nondeterministic",
    "identifier adapter",
    "provider adapters",
    "unseeded `np.random.default_rng()` per call",
)


def _hits() -> list[str]:
    hits: list[str] = []
    paths = [p for d in _SCOPE_DIRS for p in (ROOT / d).rglob("*.py") if p != _SELF] + [
        ROOT / f for f in _SCOPE_FILES
    ]
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not _STALE.search(line):
                continue
            window = "\n".join(lines[max(0, i - 1) : i + 2])
            if not _FAKER.search(window):
                continue
            if any(a in window for a in _ALLOWED):
                continue
            hits.append(f"{path.relative_to(ROOT)}:{i + 1}: {line.strip()[:110]}")
    return hits


def test_no_active_faker_text_calls_the_draw_unseeded_or_chunk_variant() -> None:
    hits = _hits()
    assert hits == [], "stale Faker 'unseeded'/'differs run to run' prose:\n" + "\n".join(hits)


def _section(path: str, heading: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    start = text.index(heading)
    nxt = re.search(r"^#{1,3} ", text[start + len(heading) :], re.M)
    return text[start : start + len(heading) + nxt.start()] if nxt else text[start:]


def test_public_docs_describe_nondeterministic_faker_as_position_keyed() -> None:
    strategies = _section("docs/strategies.md", "### faker").lower()
    assert "differs run to run" not in strategies
    assert "row position" in strategies
    determinism = (ROOT / "docs" / "determinism.md").read_text(encoding="utf-8")
    not_det = determinism[determinism.index("## What is NOT deterministic") :]
    bullet = not_det[: not_det.index("- Profiling without a seed")]
    assert "faker" not in bullet.lower()
    assert "Non-deterministic `faker`" in determinism


def test_program_doc_no_longer_treats_c5b_as_unseeded() -> None:
    text = (ROOT / "docs" / "plans" / "2026-09-30-rust-engine-program.md").read_text()
    assert "cannot match an unseeded oracle byte for byte" not in text
    parity = next(ln for ln in text.splitlines() if ln.startswith("**Parity.**"))
    assert "C5b" in parity and "position-keyed" in parity


@pytest.mark.parametrize(
    "path",
    [
        "src/decoy_engine/execution/_strategies/_faker.py",
        "src/decoy_engine/execution/_chunked.py",
    ],
)
def test_named_modules_use_truthful_faker_chunk_prose(path: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    assert "draws per-row randomness, which is chunk-variant" not in text
    assert "faker_positional" in text or "position-keyed" in text


def test_chunked_faker_rejection_prose_is_truthful_per_mode() -> None:
    from decoy_engine.execution._chunked import _conditional_admission_failures

    def failures(mode: str, **extra: object) -> str:
        return " ".join(
            _conditional_admission_failures(
                {
                    "name": "f",
                    "strategy": "faker",
                    "provider": "person_first_name",
                    "namespace": "ns",
                    "cardinality_mode": mode,
                    **extra,
                }
            )
        )

    # C5b-ii admits a complete REUSE entry (explicit pool_size), so the prose is read from an
    # entry that still fails stage A; the deferral wording is gone.
    reuse = failures("reuse")
    assert "position-keyed" in reuse and "C5b-ii" not in reuse
    unique = failures("unique", pool_size=10)
    assert "whole-column draw" in unique and "not chunk-safe" in unique
    assert "chunk-variant" not in reuse + unique
