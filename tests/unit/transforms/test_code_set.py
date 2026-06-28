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
  CS.9 - All four shipped corpora load and smoke-mask cleanly.

Methodology: HMAC-SHA256-keyed modular selection with domain exclusion
(RFC 2104, https://datatracker.ietf.org/doc/html/rfc2104).
Output != input is guaranteed by selecting from a candidate set that
excludes the input code (analogous to the FPE domain-exclusion idiom).
"""

from __future__ import annotations

import pathlib

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.plan._errors import PlanCompileError
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
        """Same seed -> same sequence of output codes."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        seed = b"\x01" * 32
        outputs1 = [apply_code_set(str(i), cfg, mode="gen", job_seed=seed) for i in range(10)]
        outputs2 = [apply_code_set(str(i), cfg, mode="gen", job_seed=seed) for i in range(10)]
        assert outputs1 == outputs2, "Gen mode must be seed-deterministic."

    def test_gen_different_seeds_differ(self):
        """Different seeds should produce different outputs."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        out1 = [apply_code_set(str(i), cfg, mode="gen", job_seed=b"\x01" * 32) for i in range(20)]
        out2 = [apply_code_set(str(i), cfg, mode="gen", job_seed=b"\x02" * 32) for i in range(20)]
        assert out1 != out2, "Different seeds should differ."

    def test_gen_output_is_real_corpus_code(self):
        """Every gen-mode output must be a real corpus code."""
        from decoy_engine.transforms.code_set import load_corpus

        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        corpus_codes = {row["code"] for row in load_corpus("icd10")}
        for i in range(20):
            out = apply_code_set(str(i), cfg, mode="gen", job_seed=_JOB_SEED)
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
        seed = b"\xab" * 32
        out1 = apply_code_set("any", cfg, mode="gen", job_seed=seed)
        out2 = apply_code_set("any", cfg, mode="gen", job_seed=seed)
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


# ── CS.9: All four shipped corpora smoke-test ─────────────────────────────────


class TestShippedCorpora:
    @pytest.mark.parametrize("corpus_name", ["icd10", "hcpcs", "ndc", "mcc"])
    def test_corpus_loads(self, corpus_name: str):
        """Each shipped corpus must load without error."""
        from decoy_engine.transforms.code_set import load_corpus

        rows = load_corpus(corpus_name)
        assert len(rows) > 0, f"Corpus {corpus_name!r} loaded 0 rows."
        assert all("code" in r for r in rows), "Every row must have a 'code' key."

    @pytest.mark.parametrize("corpus_name", ["icd10", "hcpcs", "ndc", "mcc"])
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

    @pytest.mark.parametrize("corpus_name", ["icd10", "hcpcs", "ndc", "mcc"])
    def test_corpus_gen_mode_cleanly(self, corpus_name: str):
        """Each shipped corpus must support gen mode end-to-end."""
        from decoy_engine.transforms.code_set import load_corpus

        cfg = CodeSetConfig.from_dict({"code_set": corpus_name})
        rows = load_corpus(corpus_name)
        codes = {r["code"] for r in rows}

        out = apply_code_set("any_value", cfg, mode="gen", job_seed=_JOB_SEED)
        assert out in codes, f"Gen output {out!r} not in {corpus_name!r} corpus."


# ── H1: gen mode must vary per row (intra-column variation) ───────────────────


class TestGenModePerRowVariation:
    def test_gen_mode_varies_across_row_indices(self):
        """H1: gen mode must produce >1 distinct code across a column of 10 rows.
        The original defect: np.random.default_rng(same_seed) always picks the
        same index, so a 10-row column is a constant. Per-row variation requires
        row_index to be threaded into the RNG seed.
        """
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        seed = b"\xca\xfe" * 16
        outputs = [
            apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=i) for i in range(10)
        ]
        distinct = len(set(outputs))
        assert distinct > 1, (
            f"gen mode produced a constant column: all 10 rows are {outputs[0]!r}. "
            "gen mode must vary per row_index (same seed + different row -> different code)."
        )

    def test_gen_mode_determinism_per_row_index(self):
        """Same seed + same row_index -> same code (determinism preserved)."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        seed = b"\x11" * 32
        for i in range(5):
            out1 = apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=i)
            out2 = apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=i)
            assert out1 == out2, f"row_index={i}: same seed + same row_index must give same code."


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
