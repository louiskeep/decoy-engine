"""SP-09 code_set strategy tests (TDD: tests land before implementation).

Tests cover:
  CS.1 - Mask mode: keyed HMAC picks a real corpus code; output != input.
  CS.2 - Gen mode: seeded random sampling; deterministic with fixed seed.
  CS.3 - chapter_preserve: I21 -> another I-chapter code.
  CS.4 - chapter_preserve sole-member bucket: raises PlanCompileError.
  CS.5 - corpus_source: customer path loads and masks correctly.
  CS.6 - corpus_source: malformed customer corpus raises clean error.
  CS.7 - Config validation: missing code_set name raises.
  CS.8 - SP-06 keyed-access cross-version caveat documented.
  CS.9 - Every registry-listed shipped corpus loads and smoke-masks cleanly.

HC-1 slice 1 (2026-07-17) added: provenance read/validate cases
(TestCorpusProvenance), chapter-lookup dict-index parity
(TestChapterIndexParity), and made CS.9 (TestShippedCorpora) iterate
CODESET_REGISTRY instead of a hardcoded corpus-name list.

Methodology: HMAC-SHA256-keyed modular selection with domain exclusion
(RFC 2104, https://datatracker.ietf.org/doc/html/rfc2104).
Output != input is guaranteed by selecting from a candidate set that
excludes the input code (analogous to the FPE domain-exclusion idiom).
"""

from __future__ import annotations

import os
import pathlib
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms._codeset_provenance import CODESET_REGISTRY, CodeSetProvenance
from decoy_engine.transforms.code_set import (
    CodeSetConfig,
    apply_code_set,
    validate_code_set_config,
)

# ── Stable test seed ───────────────────────────────────────────────────────────

_JOB_SEED = b"\xca\xfe" * 16

# ── CS.1: Mask mode determinism + output != input ─────────────────────────────


class TestMaskMode:
    def test_mask_determinism(self):
        """Same input value produces the same masked code on repeated calls."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        out1 = apply_code_set("I21.9", cfg, mode="mask", job_seed=_JOB_SEED)
        out2 = apply_code_set("I21.9", cfg, mode="mask", job_seed=_JOB_SEED)
        assert out1 == out2, "Mask mode must be deterministic: same input -> same output."

    def test_mask_output_not_equal_input(self):
        """Masked output must never equal the input value."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        inputs = [
            "I10",
            "I21.9",
            "E11.9",
            "Z23",
            "J18.9",
            "K21.0",
            "F32.9",
            "M54.5",
            "N39.0",
            "R07.9",
        ]
        for inp in inputs:
            out = apply_code_set(inp, cfg, mode="mask", job_seed=_JOB_SEED)
            assert out != inp, f"Mask output must differ from input {inp!r}, got {out!r}."

    def test_mask_output_is_real_corpus_code(self):
        """Every masked output must be a real member of the ICD-10 corpus."""
        from decoy_engine.transforms.code_set import load_corpus

        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        corpus_codes = {row["code"] for row in load_corpus("icd10")}
        inputs = ["I10", "E11.9", "Z23", "J18.9", "A41.9", "C61", "G47.00"]
        for inp in inputs:
            out = apply_code_set(inp, cfg, mode="mask", job_seed=_JOB_SEED)
            assert out in corpus_codes, (
                f"Masked output {out!r} for input {inp!r} is not a real ICD-10 corpus code."
            )

    def test_mask_different_inputs_may_differ(self):
        """Different inputs should (statistically) produce different outputs."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        from decoy_engine.transforms.code_set import load_corpus

        rows = load_corpus("icd10")
        codes = [r["code"] for r in rows[:15]]
        outputs = {apply_code_set(c, cfg, mode="mask", job_seed=_JOB_SEED) for c in codes}
        assert len(outputs) > 1, "Different inputs should produce multiple different outputs."

    def test_mask_does_not_mutate_config(self):
        """apply_code_set must not mutate the config object."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        name_before = cfg.code_set
        apply_code_set("I10", cfg, mode="mask", job_seed=_JOB_SEED)
        assert cfg.code_set == name_before, "apply_code_set must not mutate config."


# ── CS.2: Gen mode determinism ────────────────────────────────────────────────


class TestGenMode:
    def test_gen_mode_determinism_with_seed(self):
        """Same seed + namespace -> same sequence of output codes."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        seed = b"\x01" * 8
        ns = "test.gen"
        outputs1 = [
            apply_code_set(str(i), cfg, mode="gen", job_seed=seed, namespace=ns) for i in range(10)
        ]
        outputs2 = [
            apply_code_set(str(i), cfg, mode="gen", job_seed=seed, namespace=ns) for i in range(10)
        ]
        assert outputs1 == outputs2, "Gen mode must be seed-deterministic."

    def test_gen_different_seeds_differ(self):
        """Different seeds + row_index variation should produce different sequences.

        Note: the comparison needs row_index variation so each call draws from a
        different derive_index position; without row_index the outputs are constant
        and two constant sequences may coincide by chance (hash collision mod corpus).
        """
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        ns = "test.gen"
        out1 = [
            apply_code_set(str(i), cfg, mode="gen", job_seed=b"\x01" * 8, row_index=i, namespace=ns)
            for i in range(20)
        ]
        out2 = [
            apply_code_set(str(i), cfg, mode="gen", job_seed=b"\x02" * 8, row_index=i, namespace=ns)
            for i in range(20)
        ]
        assert out1 != out2, "Different seeds should differ."

    def test_gen_output_is_real_corpus_code(self):
        """Every gen-mode output must be a real corpus code."""
        from decoy_engine.transforms.code_set import load_corpus

        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        corpus_codes = {row["code"] for row in load_corpus("icd10")}
        ns = "test.gen"
        for i in range(20):
            out = apply_code_set(str(i), cfg, mode="gen", job_seed=b"\xca\xfe" * 4, namespace=ns)
            assert out in corpus_codes, f"Gen output {out!r} not in ICD-10 corpus."


# ── CS.3: chapter_preserve: I21.9 -> another I-chapter code ──────────────────


class TestChapterPreserve:
    def test_chapter_preserved_for_icd10(self):
        """With chapter_preserve=True, I21.9 must mask to another I-chapter code."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10", "chapter_preserve": True})
        out = apply_code_set("I21.9", cfg, mode="mask", job_seed=_JOB_SEED)
        assert out.startswith("I"), (
            f"chapter_preserve=True: I21.9 must mask to an I-chapter code, got {out!r}."
        )

    def test_chapter_preserved_output_not_equal_input(self):
        """With chapter_preserve=True, output != input."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10", "chapter_preserve": True})
        # ICD-10 I-chapter has multiple codes; output must differ.
        out = apply_code_set("I10", cfg, mode="mask", job_seed=_JOB_SEED)
        assert out != "I10", "chapter_preserve must still guarantee output != input."

    def test_chapter_preserved_output_is_real_code(self):
        """chapter_preserve output must be a real corpus member."""
        from decoy_engine.transforms.code_set import load_corpus

        cfg = CodeSetConfig.from_dict({"code_set": "icd10", "chapter_preserve": True})
        corpus_codes = {row["code"] for row in load_corpus("icd10")}
        for inp in ["I10", "I21.9", "I25.10", "I48.91"]:
            out = apply_code_set(inp, cfg, mode="mask", job_seed=_JOB_SEED)
            assert out in corpus_codes, (
                f"chapter_preserve output {out!r} for {inp!r} is not in corpus."
            )

    def test_chapter_preserve_different_chapter_stays_in_chapter(self):
        """E-chapter input stays in E-chapter under chapter_preserve."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10", "chapter_preserve": True})
        for inp in ["E11.9", "E11.65", "E78.5"]:
            out = apply_code_set(inp, cfg, mode="mask", job_seed=_JOB_SEED)
            assert out.startswith("E"), (
                f"E-chapter input {inp!r} must preserve chapter, got {out!r}."
            )


# ── CS.4: Sole-member bucket edge case ────────────────────────────────────────


class TestSoleMemberBucket:
    def test_sole_member_bucket_raises(self, tmp_path: pathlib.Path):
        """When the chapter bucket has only the input code, raise PlanCompileError."""
        # Build a tiny corpus where chapter 'Z' has only one code.
        tbl = pa.table(
            {
                "code": pa.array(["Z99", "A01", "A02"], type=pa.string()),
                "chapter": pa.array(["Z", "A", "A"], type=pa.string()),
                "description": pa.array(["Sole Z code", "A code 1", "A code 2"], type=pa.string()),
            }
        )
        path = tmp_path / "sole_member.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "sole_member",
                "chapter_preserve": True,
                "corpus_source": f"customer:{path}",
            }
        )
        with pytest.raises(PlanCompileError, match="sole"):
            apply_code_set("Z99", cfg, mode="mask", job_seed=_JOB_SEED)


# ── CS.5: corpus_source customer path ────────────────────────────────────────


class TestCustomerCorpus:
    def test_customer_corpus_loads_and_masks(self, tmp_path: pathlib.Path):
        """A customer corpus at a valid path must load and produce a real code."""
        codes = [f"C{i:03d}" for i in range(10)]
        tbl = pa.table(
            {
                "code": pa.array(codes, type=pa.string()),
                "chapter": pa.array(["C"] * 10, type=pa.string()),
                "description": pa.array([f"Code {c}" for c in codes], type=pa.string()),
            }
        )
        path = tmp_path / "custom.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "custom",
                "corpus_source": f"customer:{path}",
            }
        )
        out = apply_code_set("C000", cfg, mode="mask", job_seed=_JOB_SEED)
        assert out in set(codes), f"Customer corpus output {out!r} not in code set."
        assert out != "C000", "Customer corpus mask mode: output != input."

    def test_customer_corpus_gen_mode(self, tmp_path: pathlib.Path):
        """Customer corpus gen mode produces real codes deterministically."""
        codes = [f"X{i:02d}" for i in range(5)]
        tbl = pa.table(
            {
                "code": pa.array(codes, type=pa.string()),
                "chapter": pa.array(["X"] * 5, type=pa.string()),
                "description": pa.array([f"Desc {c}" for c in codes], type=pa.string()),
            }
        )
        path = tmp_path / "custom_gen.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "custom",
                "corpus_source": f"customer:{path}",
            }
        )
        seed = b"\xab" * 8
        ns = "test.gen"
        out1 = apply_code_set("any", cfg, mode="gen", job_seed=seed, namespace=ns)
        out2 = apply_code_set("any", cfg, mode="gen", job_seed=seed, namespace=ns)
        assert out1 == out2, "Gen mode must be deterministic for same seed."
        assert out1 in set(codes), "Gen output must be a real corpus code."


# ── CS.6: Malformed customer corpus ──────────────────────────────────────────


class TestMalformedCustomerCorpus:
    def test_missing_code_column_raises_plan_compile_error(self, tmp_path: pathlib.Path):
        """A customer corpus missing the 'code' column must raise PlanCompileError."""
        tbl = pa.table(
            {
                "notcode": pa.array(["X01", "X02"], type=pa.string()),
                "description": pa.array(["a", "b"], type=pa.string()),
            }
        )
        path = tmp_path / "bad.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "custom",
                "corpus_source": f"customer:{path}",
            }
        )
        with pytest.raises(PlanCompileError, match="code"):
            apply_code_set("X01", cfg, mode="mask", job_seed=_JOB_SEED)

    def test_nonexistent_customer_path_raises_plan_compile_error(self):
        """A customer path that does not exist must raise PlanCompileError."""
        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "custom",
                "corpus_source": "customer:/no/such/file.parquet",
            }
        )
        with pytest.raises(PlanCompileError, match="not found|path"):
            apply_code_set("X01", cfg, mode="mask", job_seed=_JOB_SEED)

    def test_empty_corpus_raises_plan_compile_error(self, tmp_path: pathlib.Path):
        """An empty customer corpus (0 rows) must raise PlanCompileError."""
        tbl = pa.table(
            {
                "code": pa.array([], type=pa.string()),
                "description": pa.array([], type=pa.string()),
            }
        )
        path = tmp_path / "empty.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "custom",
                "corpus_source": f"customer:{path}",
            }
        )
        with pytest.raises(PlanCompileError, match="empty|0 rows"):
            apply_code_set("X01", cfg, mode="mask", job_seed=_JOB_SEED)


# ── CS.7: Config validation ───────────────────────────────────────────────────


class TestConfigValidation:
    def test_missing_code_set_name_raises(self):
        """A config without 'code_set' must raise PlanCompileError."""
        with pytest.raises(PlanCompileError, match="code_set"):
            validate_code_set_config({})

    def test_unknown_shipped_corpus_raises(self):
        """An unknown shipped corpus name must raise PlanCompileError."""
        with pytest.raises(PlanCompileError, match="not found|corpus"):
            validate_code_set_config({"code_set": "nonexistent_corpus_xyz"})

    def test_valid_icd10_config_does_not_raise(self):
        """A correct config must pass validation without raising."""
        validate_code_set_config({"code_set": "icd10"})  # must not raise

    def test_chapter_preserve_without_chapter_column_raises(self, tmp_path: pathlib.Path):
        """chapter_preserve with a corpus lacking 'chapter' column must raise."""
        tbl = pa.table(
            {
                "code": pa.array(["X01", "X02"], type=pa.string()),
                "description": pa.array(["a", "b"], type=pa.string()),
            }
        )
        path = tmp_path / "no_chapter.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "custom",
                "chapter_preserve": True,
                "corpus_source": f"customer:{path}",
            }
        )
        with pytest.raises(PlanCompileError, match="chapter"):
            apply_code_set("X01", cfg, mode="mask", job_seed=_JOB_SEED)


# ── CS.8: SP-06 keyed-access cross-version caveat ────────────────────────────


class TestKeyedAccessCaveat:
    def test_code_set_config_documents_caveat(self):
        """CodeSetConfig must carry the SP-06 cross-version caveat in its docstring."""
        assert CodeSetConfig.__doc__ is not None
        lower = CodeSetConfig.__doc__.lower()
        assert "cross-version" in lower or "row_count" in lower or "corpus version" in lower, (
            "CodeSetConfig docstring must reference the SP-06 keyed_row "
            "cross-version stability caveat."
        )


# ── CS.9: All shipped corpora smoke-test (registry-driven, HC-1 item 5) ───────


class TestShippedCorpora:
    """HC-1 slice 1 item 5: parametrized off CODESET_REGISTRY, not a
    hardcoded name list. Adding a corpus to the registry makes it show up
    here automatically."""

    @pytest.mark.parametrize("corpus_name", sorted(CODESET_REGISTRY))
    def test_corpus_loads(self, corpus_name: str):
        """Each shipped corpus must load without error."""
        from decoy_engine.transforms.code_set import load_corpus

        rows = load_corpus(corpus_name)
        assert len(rows) > 0, f"Corpus {corpus_name!r} loaded 0 rows."
        assert all("code" in r for r in rows), "Every row must have a 'code' key."

    @pytest.mark.parametrize("corpus_name", sorted(CODESET_REGISTRY))
    def test_corpus_masks_cleanly(self, corpus_name: str):
        """Each shipped corpus must support mask mode end-to-end."""
        from decoy_engine.transforms.code_set import load_corpus

        cfg = CodeSetConfig.from_dict({"code_set": corpus_name})
        rows = load_corpus(corpus_name)
        codes = {r["code"] for r in rows}

        # Pick the first code from the corpus as a test input.
        first_code = rows[0]["code"]
        out = apply_code_set(first_code, cfg, mode="mask", job_seed=_JOB_SEED)
        assert out in codes, f"Mask output {out!r} not in {corpus_name!r} corpus."
        assert out != first_code, f"Mask output equals input for {corpus_name!r}."

    @pytest.mark.parametrize("corpus_name", sorted(CODESET_REGISTRY))
    def test_corpus_gen_mode_cleanly(self, corpus_name: str):
        """Each shipped corpus must support gen mode end-to-end."""
        from decoy_engine.transforms.code_set import load_corpus

        cfg = CodeSetConfig.from_dict({"code_set": corpus_name})
        rows = load_corpus(corpus_name)
        codes = {r["code"] for r in rows}

        out = apply_code_set(
            "any_value", cfg, mode="gen", job_seed=b"\xca\xfe" * 4, namespace="test.gen"
        )
        assert out in codes, f"Gen output {out!r} not in {corpus_name!r} corpus."

    def test_shipped_corpora_derived_from_registry(self):
        """`_SHIPPED_CORPORA` must be exactly the registry's keys (single
        source of truth, HC-1 slice 1 item 5)."""
        from decoy_engine.transforms.code_set import _SHIPPED_CORPORA

        assert frozenset(CODESET_REGISTRY) == _SHIPPED_CORPORA

    def test_codesets_docstring_lists_every_registry_corpus(self):
        """Drift guard: codesets/__init__.py's docstring is hand-maintained
        prose (not runtime-generated, so sphinx-autoapi's static source
        parsing renders it correctly). LOW-2 remediation: bidirectional SET
        equality against the docstring's "Shipped corpora" section, with
        exact-token matching on that section's ``name   -- description``
        lines -- not a whole-docstring substring `corpus_name in doc` check.
        The prior one-directional substring check could never catch a STALE
        entry left behind for a corpus removed from the registry, and could
        false-pass on a coincidental substring appearing anywhere in the
        ~50-line module docstring outside the actual corpus list."""
        import re

        import decoy_engine.codesets as codesets_pkg

        doc = codesets_pkg.__doc__ or ""
        section = re.search(r"Shipped corpora\n-+\n(.*?)\n\n", doc, re.DOTALL)
        assert section, "codesets/__init__.py docstring is missing its 'Shipped corpora' section."
        documented = set(re.findall(r"^(\w+)\s+--", section.group(1), re.MULTILINE))
        assert documented == frozenset(CODESET_REGISTRY), (
            f"codesets/__init__.py docstring's Shipped corpora entries {sorted(documented)} "
            f"and CODESET_REGISTRY {sorted(CODESET_REGISTRY)} have drifted "
            "(a corpus was added to or removed from one but not the other)."
        )

    def test_docs_strategies_md_lists_every_registry_corpus(self):
        """Drift guard: docs/strategies.md's Shipped corpora bullet list must
        name EXACTLY the registry's corpora (HC-1 slice 1 item 6). LOW-2
        remediation: bidirectional SET equality on the section's
        ``- `name` -- ...`` bullets, not a whole-file substring
        (backtick + name + backtick) `in text` check -- e.g. `icd10` is ALSO
        backtick-quoted elsewhere in
        this doc as a PII-detector id (the TIER 1 detector table), unrelated
        to the code_set corpus registry, so the old check could false-pass
        even with the actual corpus-list bullet missing or stale."""
        import pathlib
        import re

        docs_path = pathlib.Path(__file__).resolve().parents[3] / "docs" / "strategies.md"
        text = docs_path.read_text(encoding="utf-8")
        section = re.search(r"Shipped corpora \(under.*?:\n\n(.*?)\n\n\*\*HC-1", text, re.DOTALL)
        assert section, "docs/strategies.md is missing its 'Shipped corpora' bullet section."
        documented = set(re.findall(r"^- `(\w+)` --", section.group(1), re.MULTILINE))
        assert documented == frozenset(CODESET_REGISTRY), (
            f"docs/strategies.md Shipped corpora bullets {sorted(documented)} and "
            f"CODESET_REGISTRY {sorted(CODESET_REGISTRY)} have drifted "
            "(a corpus was added to or removed from one but not the other)."
        )


# ── H1: gen mode must vary per row (intra-column variation) ───────────────────


class TestGenModePerRowVariation:
    def test_gen_mode_varies_across_row_indices(self):
        """H1: gen mode must produce >1 distinct code across a column of 10 rows.
        The original defect: np.random.default_rng(same_seed) always picks the
        same index, so a 10-row column is a constant. Per-row variation requires
        row_index to be threaded into the derive_index source.
        """
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        seed = b"\xca\xfe" * 4  # 8 bytes
        ns = "test.variation"
        outputs = [
            apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=i, namespace=ns)
            for i in range(10)
        ]
        distinct = len(set(outputs))
        assert distinct > 1, (
            f"gen mode produced a constant column: all 10 rows are {outputs[0]!r}. "
            "gen mode must vary per row_index (same seed + different row -> different code)."
        )

    def test_gen_mode_determinism_per_row_index(self):
        """Same seed + same namespace + same row_index -> same code."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        seed = b"\x11" * 8
        ns = "test.determinism"
        for i in range(5):
            out1 = apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=i, namespace=ns)
            out2 = apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=i, namespace=ns)
            assert out1 == out2, f"row_index={i}: same seed + same row_index must give same code."


# ── MEDIUM: gen mode cross-column decorrelation (namespace-blind defect) ──────


class TestGenModeDecorrelation:
    """MEDIUM: gen selection must be namespace-bound via derive_index.

    The original defect: two gen columns sharing the same job_seed produced
    byte-identical output row-for-row because the RNG seed was computed from
    job_seed + row_index with no namespace component. Two columns with
    different namespaces must produce different sequences (decorrelation).
    """

    def test_cross_column_decorrelation(self):
        """Two gen columns with different namespaces, same seed and corpus,
        must produce different output sequences (NOT byte-identical).

        Before fix: col_a == col_b because derive_index is bypassed.
        After fix: col_a != col_b because each column's namespace feeds HKDF.
        """
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        seed = b"\xca\xfe" * 4  # 8 bytes (canonical StrategyContext length)
        n = 20
        col_a = [
            apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=i, namespace="ns.col_a")
            for i in range(n)
        ]
        col_b = [
            apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=i, namespace="ns.col_b")
            for i in range(n)
        ]
        assert col_a != col_b, (
            "Different namespaces must produce different gen sequences. "
            "Both columns produced identical output, indicating gen selection is namespace-blind."
        )

    def test_gen_determinism_with_namespace(self):
        """Same namespace + seed -> identical column on two independent runs."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        seed = b"\xca\xfe" * 4
        ns = "ns.determinism"
        run1 = [
            apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=i, namespace=ns)
            for i in range(10)
        ]
        run2 = [
            apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=i, namespace=ns)
            for i in range(10)
        ]
        assert run1 == run2, "Same namespace + seed must produce identical column on rerun."

    def test_chapter_preserve_gen_draws_from_candidates(self, tmp_path):
        """LOW: chapter_preserve gen mode must draw from candidates (bucket minus
        input), not from the full bucket. With a two-code chapter the output must
        never equal the input code.
        """
        import pyarrow as pa
        import pyarrow.parquet as pq

        tbl = pa.table(
            {
                "code": pa.array(["A01", "A02", "B01", "B02"], type=pa.string()),
                "chapter": pa.array(["A", "A", "B", "B"], type=pa.string()),
            }
        )
        path = tmp_path / "two_codes_per_chapter.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "two_codes",
                "chapter_preserve": True,
                "corpus_source": f"customer:{path}",
            }
        )
        seed = b"\xca\xfe" * 4
        # A chapter has only A01 and A02; input is A01 so candidates = [A02].
        for i in range(5):
            out = apply_code_set(
                "A01", cfg, mode="gen", job_seed=seed, row_index=i, namespace="ns.test"
            )
            assert out != "A01", (
                f"row {i}: chapter_preserve gen mode returned the input code A01. "
                "gen must draw from candidates (bucket minus input), not from bucket."
            )
            assert out.startswith("A"), f"row {i}: chapter_preserve violated, got {out!r}."


# ── H2: chapter_preserve with unknown chapter must fail closed ────────────────


class TestChapterPreserveUnknownChapter:
    def test_unknown_chapter_raises_plan_compile_error(self, tmp_path: pathlib.Path):
        """H2: when chapter_preserve=True and the input's chapter is not present
        in the corpus, raise PlanCompileError (fail-closed). The old behavior
        (fall through to full-corpus selection) silently breaks the invariant.
        """
        tbl = pa.table(
            {
                "code": pa.array(["A01", "A02", "B01", "B02"], type=pa.string()),
                "chapter": pa.array(["A", "A", "B", "B"], type=pa.string()),
            }
        )
        path = tmp_path / "two_chapters.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "two_chapters",
                "chapter_preserve": True,
                "corpus_source": f"customer:{path}",
            }
        )
        with pytest.raises(PlanCompileError, match="chapter"):
            # U chapter is absent from the corpus (only A and B exist).
            apply_code_set("U07.1", cfg, mode="mask", job_seed=_JOB_SEED)

    def test_unknown_chapter_gen_mode_raises_plan_compile_error(self, tmp_path: pathlib.Path):
        """H2: same fail-closed behavior in gen mode."""
        tbl = pa.table(
            {
                "code": pa.array(["A01", "A02"], type=pa.string()),
                "chapter": pa.array(["A", "A"], type=pa.string()),
            }
        )
        path = tmp_path / "one_chapter.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "one_chapter",
                "chapter_preserve": True,
                "corpus_source": f"customer:{path}",
            }
        )
        with pytest.raises(PlanCompileError, match="chapter"):
            apply_code_set("Z99", cfg, mode="gen", job_seed=_JOB_SEED)


# ── HC-1 slice 1: provenance read/validate + evidence surfacing ──────────────


class TestCorpusProvenance:
    """HC-1 slice 1 items 1-3: provenance schema, fail-closed/warn loading,
    and evidence surfacing (describe_loaded_corpus)."""

    def test_load_corpus_provenance_shipped_icd10(self):
        """A shipped corpus's provenance parses with every required field
        present and no missing_required_fields()."""
        from decoy_engine.transforms.code_set import load_corpus_provenance

        prov = load_corpus_provenance("icd10")
        assert isinstance(prov, CodeSetProvenance)
        assert prov.source_version == "FY2024"
        assert prov.effective_date == "2023-10-01"
        assert prov.source
        assert prov.license
        assert prov.is_seed is True, "HC-1 slice 1 shipped corpora are abbreviated seeds."
        assert prov.missing_required_fields() == []

    def test_describe_loaded_corpus_evidence_shape(self):
        """describe_loaded_corpus returns counts + identifiers only -- the
        exact shape CodeSetHandler stamps into
        ExecutionResult.quality_metrics['code_set_corpora']."""
        from decoy_engine.transforms.code_set import describe_loaded_corpus

        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        summary = describe_loaded_corpus(cfg)
        assert summary["code_set"] == "icd10"
        assert summary["source_version"] == "FY2024"
        assert summary["effective_date"] == "2023-10-01"
        assert summary["is_seed"] is True
        assert summary["row_count"] > 0
        # No raw codes leak into the evidence summary (SubsetManifest
        # no-raw-data contract).
        assert "codes" not in summary
        assert "rows" not in summary

    def test_shipped_corpus_missing_provenance_fails_closed(self, tmp_path: pathlib.Path):
        """A SHIPPED corpus with NO provenance metadata at all is job-fatal
        (code_set_corpus_missing_provenance), even though the file itself is
        otherwise well-formed. Drives the loader's internal is_shipped=True
        branch directly (there is no public way to make a real shipped
        corpus lack provenance without mutating the repo's own files)."""
        from decoy_engine.transforms._codeset_loader import _get_corpus_record

        tbl = pa.table({"code": pa.array(["A01", "A02"], type=pa.string())})
        path = tmp_path / "no_provenance.parquet"
        pq.write_table(tbl, str(path))

        with pytest.raises(PlanCompileError) as exc_info:
            _get_corpus_record("no_provenance", path, is_shipped=True)
        assert exc_info.value.code == "code_set_corpus_missing_provenance"

    def test_shipped_corpus_partial_provenance_fails_closed(self, tmp_path: pathlib.Path):
        """A SHIPPED corpus with an INCOMPLETE provenance stamp (some
        required fields present, others missing) also fails closed."""
        from decoy_engine.transforms._codeset_loader import _get_corpus_record

        tbl = pa.table(
            {"code": pa.array(["A01", "A02"], type=pa.string())},
            metadata={b"decoy_corpus": b"partial", b"source": b"Some Source"},
        )
        path = tmp_path / "partial_provenance.parquet"
        pq.write_table(tbl, str(path))

        with pytest.raises(PlanCompileError) as exc_info:
            _get_corpus_record("partial", path, is_shipped=True)
        assert exc_info.value.code == "code_set_corpus_missing_provenance"

    def test_shipped_corpus_provenance_identity_mismatch_fails_closed(self, tmp_path: pathlib.Path):
        """Codex round-5 P2 SHIPPED PROVENANCE IDENTITY UNVERIFIED remediation:
        a SHIPPED corpus with a COMPLETE provenance stamp still fails closed
        when the embedded `decoy_corpus` id does not match the requested
        corpus name -- e.g. `icd10.parquet` packaged with `mcc`'s provenance
        metadata. Completeness alone is not identity; a packaging/metadata
        swap must not silently attribute one corpus's provenance to
        another's evidence trail."""
        from decoy_engine.transforms._codeset_loader import _get_corpus_record

        tbl = pa.table(
            {"code": pa.array(["A01", "A02"], type=pa.string())},
            metadata={
                b"decoy_corpus": b"mcc",
                b"decoy_corpus_version": b"2.0",
                b"source": b"ISO 18245 Merchant Category Codes",
                b"source_version": b"2003",
                b"effective_date": b"2003-01-01",
                b"license": b"Public reference enumeration",
            },
        )
        path = tmp_path / "icd10.parquet"
        pq.write_table(tbl, str(path))

        with pytest.raises(PlanCompileError) as exc_info:
            _get_corpus_record("icd10", path, is_shipped=True)
        assert exc_info.value.code == "code_set_corpus_provenance_identity_mismatch"
        assert "icd10" in exc_info.value.message
        assert "mcc" in exc_info.value.message

    def _complete_shipped_meta(self, **overrides: bytes) -> dict[bytes, bytes]:
        """A shipped stamp with all four REQUIRED fields + matching identity.

        Callers override `is_seed` / `decoy_corpus_version` to exercise the
        Codex round-7 shipped-stamp strictness in isolation (identity and
        required-field checks already pass, so validation reaches the new
        gate). `decoy_corpus` is 'seedcorpus' to match the requested name.
        """
        meta: dict[bytes, bytes] = {
            b"decoy_corpus": b"seedcorpus",
            b"decoy_corpus_version": b"2.0",
            b"source": b"Some Public Source",
            b"source_version": b"2024",
            b"effective_date": b"2024-01-01",
            b"license": b"Public domain",
            b"is_seed": b"true",
        }
        # Override keys arrive as str (**kwargs); the metadata dict is
        # byte-keyed, so encode them to actually REPLACE the base entry rather
        # than add a colliding str key that pyarrow silently drops.
        for key, value in overrides.items():
            meta[key.encode()] = value
        return meta

    def test_shipped_corpus_missing_is_seed_fails_closed(self, tmp_path: pathlib.Path):
        """Codex round-7 P2 remediation: a SHIPPED corpus with complete
        required fields and matching identity but NO is_seed key fails closed.
        Previously `from_parquet_metadata` silently coerced the absent key to
        is_seed=False, so evidence reported an unknown seed status as a full
        corpus. is_seed is not a REQUIRED_PROVENANCE_FIELD, so the pre-round-7
        completeness check let it through."""
        from decoy_engine.transforms._codeset_loader import _get_corpus_record

        meta = self._complete_shipped_meta()
        del meta[b"is_seed"]
        tbl = pa.table({"code": pa.array(["A01", "A02"], type=pa.string())}, metadata=meta)
        path = tmp_path / "seedcorpus.parquet"
        pq.write_table(tbl, str(path))

        with pytest.raises(PlanCompileError) as exc_info:
            _get_corpus_record("seedcorpus", path, is_shipped=True)
        assert exc_info.value.code == "code_set_corpus_provenance_malformed_stamp"
        assert "is_seed" in exc_info.value.message

    def test_shipped_corpus_invalid_is_seed_fails_closed(self, tmp_path: pathlib.Path):
        """Codex round-7 P2 remediation: a non-boolean is_seed value (e.g.
        'yes') on a SHIPPED corpus fails closed rather than silently parsing to
        False (anything != 'true' collapsed to False pre-round-7)."""
        from decoy_engine.transforms._codeset_loader import _get_corpus_record

        tbl = pa.table(
            {"code": pa.array(["A01", "A02"], type=pa.string())},
            metadata=self._complete_shipped_meta(is_seed=b"yes"),
        )
        path = tmp_path / "seedcorpus.parquet"
        pq.write_table(tbl, str(path))

        with pytest.raises(PlanCompileError) as exc_info:
            _get_corpus_record("seedcorpus", path, is_shipped=True)
        assert exc_info.value.code == "code_set_corpus_provenance_malformed_stamp"

    def test_shipped_corpus_stale_metadata_version_fails_closed(self, tmp_path: pathlib.Path):
        """Codex round-7 P2 remediation: a SHIPPED corpus stamped with a
        superseded metadata format version (corpus_version != current) fails
        closed -- the format is our own and a stale one is a packaging defect,
        not something to accept silently."""
        from decoy_engine.transforms._codeset_loader import _get_corpus_record

        tbl = pa.table(
            {"code": pa.array(["A01", "A02"], type=pa.string())},
            metadata=self._complete_shipped_meta(decoy_corpus_version=b"1.0"),
        )
        path = tmp_path / "seedcorpus.parquet"
        pq.write_table(tbl, str(path))

        with pytest.raises(PlanCompileError) as exc_info:
            _get_corpus_record("seedcorpus", path, is_shipped=True)
        assert exc_info.value.code == "code_set_corpus_provenance_malformed_stamp"
        assert "corpus_version" in exc_info.value.message

    def test_customer_corpus_missing_is_seed_and_version_is_exempt(self, tmp_path: pathlib.Path):
        """Codex round-7 P2 remediation is SHIPPED-ONLY: a CUSTOMER corpus may
        legitimately omit is_seed (defaults False, surfaced as-is) and never
        carries our corpus_version. It must still load with complete required
        fields, not trip the new shipped-stamp gate."""
        from decoy_engine.transforms.code_set import load_corpus_provenance

        tbl = pa.table(
            {"code": pa.array(["C01", "C02"], type=pa.string())},
            metadata={
                b"decoy_corpus": b"customer_no_seed",
                b"source": b"Internal registry",
                b"source_version": b"2026-01",
                b"effective_date": b"2026-01-01",
                b"license": b"Proprietary",
            },
        )
        path = tmp_path / "customer_no_seed.parquet"
        pq.write_table(tbl, str(path))

        prov = load_corpus_provenance("customer_no_seed", path)
        assert prov is not None
        assert prov.is_seed is False
        assert prov.raw_is_seed is None  # absent, but exempt for customer corpora

    def test_customer_corpus_missing_provenance_warns_not_fails(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ):
        """A CUSTOMER corpus with no provenance metadata at all only warns;
        it must still load and mask successfully (provenance is optional
        for customer corpora)."""
        import logging

        codes = ["Y01", "Y02", "Y03"]
        tbl = pa.table({"code": pa.array(codes, type=pa.string())})
        path = tmp_path / "customer_no_prov.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {"code_set": "customer_no_prov", "corpus_source": f"customer:{path}"}
        )
        with caplog.at_level(logging.WARNING, logger="decoy_engine.transforms._codeset_loader"):
            out = apply_code_set("Y01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert out in set(codes)
        assert any("no provenance metadata" in rec.message for rec in caplog.records), (
            "a customer corpus without provenance must log a warning, not raise."
        )

    def test_customer_corpus_partial_provenance_fails_closed(self, tmp_path: pathlib.Path):
        """A CUSTOMER corpus with a PARTIAL provenance stamp still fails
        closed: 'if present it must validate' (HC-1 slice 1 item 2) -- a
        half-filled provenance block is worse than none."""
        tbl = pa.table(
            {"code": pa.array(["Z01", "Z02"], type=pa.string())},
            metadata={b"decoy_corpus": b"partial_customer", b"source": b"Some Source"},
        )
        path = tmp_path / "customer_partial_prov.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {"code_set": "partial_customer", "corpus_source": f"customer:{path}"}
        )
        with pytest.raises(PlanCompileError) as exc_info:
            apply_code_set("Z01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc_info.value.code == "code_set_corpus_missing_provenance"

    def test_customer_corpus_complete_provenance_validates(self, tmp_path: pathlib.Path):
        """A CUSTOMER corpus with a COMPLETE provenance stamp loads cleanly
        (no warning, no raise) -- 'if present it must validate' passes when
        it actually does."""
        tbl = pa.table(
            {"code": pa.array(["W01", "W02"], type=pa.string())},
            metadata={
                b"decoy_corpus": b"complete_customer",
                b"source": b"Internal customer registry",
                b"source_version": b"2026-01",
                b"effective_date": b"2026-01-01",
                b"license": b"Proprietary, customer-owned",
            },
        )
        path = tmp_path / "customer_complete_prov.parquet"
        pq.write_table(tbl, str(path))

        from decoy_engine.transforms.code_set import load_corpus_provenance

        prov = load_corpus_provenance("complete_customer", path)
        assert prov is not None
        assert prov.missing_required_fields() == []
        assert prov.source_version == "2026-01"

    def test_from_parquet_metadata_ignores_non_utf8_unrelated_key_no_marker(self) -> None:
        """Codex P2 PROVENANCE METADATA DECODE CRASH remediation (unit level):
        PyArrow schema metadata is byte-valued and may legally carry opaque
        binary bytes on a key that has nothing to do with provenance. With no
        `decoy_corpus` marker present at all, `from_parquet_metadata` must
        return None (no provenance) instead of raising UnicodeDecodeError
        while blindly decoding every metadata key."""
        tbl = pa.table(
            {"code": pa.array(["Q01", "Q02"], type=pa.string())},
            metadata={b"some_unrelated_binary_key": b"\xff\xfe\x00\x01\x02\xc0\xc1"},
        )
        prov = CodeSetProvenance.from_parquet_metadata(tbl)
        assert prov is None

    def test_from_parquet_metadata_ignores_non_utf8_unrelated_key_with_marker(self) -> None:
        """Same binary-metadata hazard, but WITH a valid provenance marker
        present alongside the unrelated binary key: the known provenance
        fields must still parse correctly and the unrelated binary key must
        never be touched (it is not one of the known provenance keys)."""
        tbl = pa.table(
            {"code": pa.array(["Q01", "Q02"], type=pa.string())},
            metadata={
                b"decoy_corpus": b"binary_meta_corpus",
                b"source": b"Some Source",
                b"source_version": b"2026-01",
                b"effective_date": b"2026-01-01",
                b"license": b"Proprietary",
                b"some_unrelated_binary_key": b"\xff\xfe\x00\x01\x02\xc0\xc1",
            },
        )
        prov = CodeSetProvenance.from_parquet_metadata(tbl)
        assert prov is not None
        assert prov.corpus == "binary_meta_corpus"
        assert prov.source_version == "2026-01"
        assert prov.missing_required_fields() == []

    def test_customer_corpus_binary_metadata_no_marker_loads_via_optional_path(
        self, tmp_path: pathlib.Path
    ) -> None:
        """End-to-end: a customer corpus whose Parquet schema metadata
        contains a non-UTF-8 binary key/value AND no provenance marker must
        load and mask successfully via the optional-provenance (warn, no
        crash) path -- not raise UnicodeDecodeError."""
        codes = ["V01", "V02"]
        tbl = pa.table(
            {"code": pa.array(codes, type=pa.string())},
            metadata={b"some_binary_key": b"\xff\xfe\x00\x01\x02"},
        )
        path = tmp_path / "customer_binary_meta.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {"code_set": "binary_meta", "corpus_source": f"customer:{path}"}
        )
        out = apply_code_set("V01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert out in set(codes)

        from decoy_engine.transforms.code_set import load_corpus_provenance

        prov = load_corpus_provenance("binary_meta", path)
        assert prov is None, "no provenance marker present -> no provenance, not a crash."


# ── HC-1 slice 1 item 4: dict-index parity for chapter lookup ────────────────


class TestChapterIndexParity:
    """The memoized code->chapter dict index must produce byte-identical
    results to the pre-HC-1 linear scan, for every case the old _get_chapter
    handled (exact code match, unknown code fallback, no-chapter-column)."""

    @staticmethod
    def _naive_get_chapter(code: str, rows: list) -> str | None:
        """Reference implementation: the exact pre-HC-1 linear-scan logic."""
        if not rows:
            return None
        if "chapter" not in rows[0]:
            return None
        for row in rows:
            if str(row["code"]) == code:
                return str(row["chapter"])
        return code[0] if code else None

    def test_dict_index_matches_naive_scan_icd10(self):
        from decoy_engine.transforms._codeset_loader import _get_corpus_record
        from decoy_engine.transforms.code_set import _get_chapter

        record = _get_corpus_record("icd10", None, is_shipped=True)
        known_codes = [r["code"] for r in record.rows[:10]]
        test_codes = [*known_codes, "ZZZ.UNKNOWN", ""]
        for code in test_codes:
            expected = self._naive_get_chapter(code, record.rows)
            actual = _get_chapter(code, record.chapter_index)
            assert actual == expected, (
                f"chapter mismatch for {code!r}: dict-index gave {actual!r}, "
                f"naive scan gave {expected!r}."
            )

    def test_dict_index_none_when_no_chapter_column(self):
        """chapter_index is None (no per-corpus dict built) when the corpus
        has no 'chapter' column; _get_chapter returns None, matching the
        pre-HC-1 behavior."""
        from decoy_engine.transforms.code_set import _get_chapter

        assert _get_chapter("ANY", None) is None

    def test_dict_index_duplicate_code_resolves_first_wins(self, tmp_path: pathlib.Path):
        """LOW-1 remediation: a duplicate code in a customer corpus must
        resolve to its FIRST occurrence in code-sorted (rows) order, matching
        the pre-HC-1 linear scan's `for row in rows: if match: return` (which
        stops at the first hit). A naive dict comprehension over the
        code-sorted rows is LAST-write-wins, which would silently diverge
        from the old scan's answer for a duplicate code."""
        from decoy_engine.transforms._codeset_loader import _get_corpus_record
        from decoy_engine.transforms.code_set import _get_chapter

        tbl = pa.table(
            {
                "code": pa.array(["D01", "D01", "D02"], type=pa.string()),
                "chapter": pa.array(["FIRST", "SECOND", "D"], type=pa.string()),
            }
        )
        path = tmp_path / "dup_codes.parquet"
        pq.write_table(tbl, str(path))

        record = _get_corpus_record("dup_codes", path, is_shipped=False)
        expected = self._naive_get_chapter("D01", record.rows)
        assert expected == "FIRST", "test fixture sanity: naive scan must pick the first row."
        actual = _get_chapter("D01", record.chapter_index)
        assert actual == "FIRST", (
            f"dict-index resolved duplicate code 'D01' to {actual!r}; expected the FIRST "
            "code-sorted occurrence ('FIRST'), matching the pre-HC-1 linear scan."
        )


# ── MEDIUM-1: customer corpus cache invalidation + bounded growth ────────────


class TestCustomerCorpusCacheInvalidation:
    """MEDIUM-1 remediation: a customer corpus cache keyed only on resolved
    path is a dual defect in a long-lived platform worker -- a file replaced
    at the same path is served stale forever (correctness), and the cache
    grows one entry per distinct path ever seen with no eviction (memory).
    Both are fixed in `transforms/_codeset_loader.py::_customer_cache`: keyed
    on (resolved_path, mtime_ns, ctime_ns, size) -- ctime closes the case
    where a same-size replacement is deliberately re-stamped with the
    original mtime (Codex P2 remediation) -- so a same-path replacement
    mints a new entry, in a bounded LRU so the cache cannot grow without
    bound. SHIPPED corpora are unaffected (bundled, immutable, simple path
    key; see `_shipped_cache`)."""

    @staticmethod
    def _write(path: pathlib.Path, codes: list[str]) -> None:
        tbl = pa.table(
            {
                "code": pa.array(codes, type=pa.string()),
                "description": pa.array([f"desc-{c}" for c in codes], type=pa.string()),
            }
        )
        pq.write_table(tbl, str(path))

    def test_stat_race_after_existence_check_raises_typed_error(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex R3 P2: a customer corpus that disappears between the
        existence check in `_resolve_read_path` and the cache-key `stat()` in
        `_get_corpus_record` must surface the loader's typed PlanCompileError
        (load_corpus's documented contract), not a bare FileNotFoundError."""
        from decoy_engine.transforms import _codeset_loader

        path = tmp_path / "gone.parquet"
        self._write(path, ["A1", "B2"])  # real file so setup is realistic

        # `_resolve_read_path` passes (returns the path); the cache-key stat()
        # then fails as if the file was removed in the TOCTOU window.
        monkeypatch.setattr(_codeset_loader, "_resolve_read_path", lambda name, p: path)
        monkeypatch.setattr(pathlib.Path, "resolve", lambda self, *a, **k: path)

        def _raise_stat(self, *a, **k):
            raise FileNotFoundError("removed after existence check")

        monkeypatch.setattr(pathlib.Path, "stat", _raise_stat)

        with pytest.raises(PlanCompileError, match="unavailable|not found"):
            _codeset_loader._get_corpus_record("gone", path, is_shipped=False)

    def test_replaced_customer_corpus_at_same_path_is_not_served_stale(
        self, tmp_path: pathlib.Path
    ):
        """Replacing a customer corpus file at the same path between
        load_corpus calls must return the NEW rows, not the cached old ones."""
        from decoy_engine.transforms.code_set import load_corpus

        path = tmp_path / "replaceable.parquet"
        self._write(path, ["OLD1", "OLD2", "OLD3"])
        first = {r["code"] for r in load_corpus("replaceable", path)}
        assert first == {"OLD1", "OLD2", "OLD3"}

        # A real file replacement at the SAME path (new content -> new size;
        # the sleep also guards mtime-based filesystems with coarse
        # resolution, belt-and-suspenders with the size difference).
        time.sleep(0.01)
        self._write(path, ["NEW1", "NEW2"])
        second = {r["code"] for r in load_corpus("replaceable", path)}
        assert second == {"NEW1", "NEW2"}, (
            f"load_corpus served stale cached rows {second!r} after the file at "
            f"{path} was replaced; expected the new content."
        )

    def test_replaced_customer_corpus_with_same_mtime_and_size_is_not_served_stale(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex P2 CUSTOMER CACHE SAME-MTIME+SAME-SIZE STALENESS
        remediation: a replacement file that keeps the EXACT same mtime and
        size -- a coarse-timestamp filesystem, or tooling that explicitly
        restores mtime after writing (e.g. `rsync --times`) -- must still be
        detected. `ctime` (inode change time) updates on any metadata-
        changing operation (write, rename, utime), even when mtime is
        deliberately re-stamped, because the re-stamping call is itself a
        metadata change.

        Real stat() results cannot be forced to collide on mtime_ns+size
        across two genuinely different writes on every filesystem/CI
        runner, so this test patches `Path.stat` for the corpus path only:
        both loads see byte-identical mtime_ns and size, and only ctime_ns
        differs on the second -- exactly what a real same-path replacement
        with a preserved mtime leaves behind. This isolates the assertion
        to the one variable the fix actually added to the cache key.

        The fake ctime is driven by a `state` dict the test flips explicitly
        BETWEEN the two `load_corpus` calls, not by an internal call
        counter: `_get_corpus_record` calls `.stat()` (directly or via
        `Path.exists()`/`.resolve()`) an unspecified, possibly-multiple
        number of times per invocation, so a counter that advances the
        return value on every call cannot reliably land on a different
        value for the cache-key-determining call each time.
        """
        from decoy_engine.transforms.code_set import load_corpus

        path = tmp_path / "ctime_replaceable.parquet"
        self._write(path, ["OLD1", "OLD2"])
        real_stat = pathlib.Path.stat
        frozen = real_stat(path)
        state = {"ctime_ns": frozen.st_ctime_ns}

        def _fake_stat(self: pathlib.Path, *args: object, **kwargs: object) -> os.stat_result:
            if self != path:
                return real_stat(self, *args, **kwargs)
            # Same mtime_ns and size every call; ctime_ns is whatever the
            # test has currently set (flipped once, between the two loads).
            return os.stat_result(
                (
                    frozen.st_mode,
                    frozen.st_ino,
                    frozen.st_dev,
                    frozen.st_nlink,
                    frozen.st_uid,
                    frozen.st_gid,
                    frozen.st_size,
                    int(frozen.st_atime),
                    int(frozen.st_mtime),
                    int(frozen.st_ctime),
                ),
                {
                    "st_atime_ns": frozen.st_atime_ns,
                    "st_mtime_ns": frozen.st_mtime_ns,
                    "st_ctime_ns": state["ctime_ns"],
                },
            )

        monkeypatch.setattr(pathlib.Path, "stat", _fake_stat)

        first = {r["code"] for r in load_corpus("ctime_replaceable", path)}
        assert first == {"OLD1", "OLD2"}

        # Replace the file's CONTENT on disk and bump only the fake ctime;
        # mtime_ns and size stay byte-identical to the first load.
        self._write(path, ["NEW1", "NEW2"])
        state["ctime_ns"] = frozen.st_ctime_ns + 1
        second = {r["code"] for r in load_corpus("ctime_replaceable", path)}
        assert second == {"NEW1", "NEW2"}, (
            f"load_corpus served stale cached rows {second!r} for a replacement "
            "that kept an identical mtime_ns and size and differed only in "
            "ctime_ns; the cache key must include ctime to catch this."
        )

    def test_replaced_customer_corpus_provenance_also_refreshes(self, tmp_path: pathlib.Path):
        """`load_corpus_provenance` shares the same cache; it must not go
        stale either after a same-path file replacement."""
        from decoy_engine.transforms.code_set import load_corpus_provenance

        path = tmp_path / "replaceable_prov.parquet"
        tbl1 = pa.table(
            {"code": pa.array(["A1"], type=pa.string())},
            metadata={
                b"decoy_corpus": b"replaceable_prov",
                b"source": b"First Source",
                b"source_version": b"v1",
                b"effective_date": b"2020-01-01",
                b"license": b"Public",
            },
        )
        pq.write_table(tbl1, str(path))
        prov1 = load_corpus_provenance("replaceable_prov", path)
        assert prov1 is not None and prov1.source == "First Source"

        time.sleep(0.01)
        tbl2 = pa.table(
            {"code": pa.array(["B1"], type=pa.string())},
            metadata={
                b"decoy_corpus": b"replaceable_prov",
                b"source": b"A Completely Different Second Source",
                b"source_version": b"v2",
                b"effective_date": b"2021-01-01",
                b"license": b"Public",
            },
        )
        pq.write_table(tbl2, str(path))
        prov2 = load_corpus_provenance("replaceable_prov", path)
        assert prov2 is not None and prov2.source == "A Completely Different Second Source", (
            "load_corpus_provenance served the stale cached provenance after the "
            "underlying file was replaced at the same path."
        )

    def test_customer_cache_is_bounded(self, tmp_path: pathlib.Path):
        """The customer corpus cache must never grow past its LRU cap:
        loading more distinct customer files than the cap must always leave
        at most `cap` entries resident, regardless of how many distinct
        customer corpora this process has ever loaded."""
        from decoy_engine.transforms import _codeset_loader
        from decoy_engine.transforms.code_set import load_corpus

        cap = _codeset_loader._CUSTOMER_CACHE_MAX_ENTRIES
        for i in range(cap + 10):
            path = tmp_path / f"bounded_{i}.parquet"
            self._write(path, [f"C{i}"])
            load_corpus(f"bounded_{i}", path)
            assert len(_codeset_loader._customer_cache) <= cap, (
                f"customer corpus cache grew to {len(_codeset_loader._customer_cache)} "
                f"entries after loading corpus #{i}, exceeding the {cap}-entry bound."
            )

    def test_load_corpus_returned_rows_do_not_share_cache_identity(self) -> None:
        """NIT-2 remediation: mutating a row dict returned by `load_corpus`
        must never corrupt the shared cached record another caller reads
        next (each call returns fresh row dicts, not references into the
        cache)."""
        from decoy_engine.transforms.code_set import load_corpus

        rows = load_corpus("icd10")
        original_code = rows[0]["code"]
        rows[0]["code"] = "MUTATED"

        rows_again = load_corpus("icd10")
        assert rows_again[0]["code"] == original_code, (
            "mutating a row returned by load_corpus() corrupted the shared cache; "
            f"expected {original_code!r} on the next call, got {rows_again[0]['code']!r}."
        )

    def test_shipped_corpus_cache_unaffected_by_customer_invalidation(self) -> None:
        """SHIPPED corpora keep the simple path-only cache (bundled files are
        immutable at runtime; re-loading the same shipped corpus must hit the
        cache, not re-read the file, and must not consult mtime/size at all)."""
        from decoy_engine.transforms._codeset_loader import _get_corpus_record

        first = _get_corpus_record("icd10", None, is_shipped=True)
        second = _get_corpus_record("icd10", None, is_shipped=True)
        assert first is second, "shipped corpus reload must hit the cache (same object)."
