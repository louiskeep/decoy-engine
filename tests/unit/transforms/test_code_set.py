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

    def test_empty_value_fails_closed_even_if_a_chapter_is_named_xxxx(self, tmp_path: pathlib.Path):
        # Chapter-fallback else branch (code_set.py:453): an empty input value has
        # no derivable chapter, so `value[0] if value else ""` yields "", which is
        # never a bucket key -> fail closed. A mutant using a NON-empty literal
        # ("XXXX") instead of "" would collide with a corpus whose chapter IS
        # "XXXX" and MASK the empty value instead of raising -- a fail-closed ->
        # produce-output regression. (mutmut_31/32, the None/value[1] variants,
        # stay equivalent: "" and None are never bucket keys.)
        tbl = pa.table(
            {
                "code": pa.array(["A01", "A02"], type=pa.string()),
                "chapter": pa.array(["XXXX", "XXXX"], type=pa.string()),
                "description": pa.array(["a", "b"], type=pa.string()),
            }
        )
        path = tmp_path / "xxxx_chapter.parquet"
        pq.write_table(tbl, str(path))
        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "xxxx_chapter",
                "chapter_preserve": True,
                "corpus_source": f"customer:{path}",
            }
        )
        with pytest.raises(PlanCompileError) as exc:
            apply_code_set("", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc.value.code == "code_set_chapter_absent"


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

    def test_missing_code_set_name_error_fields(self):
        """The missing-name refusal carries the machine-routable code + path
        that callers key on to steer the error to the right UI field / CLI
        exit, not just human-readable prose."""
        with pytest.raises(PlanCompileError) as exc:
            validate_code_set_config({})
        assert exc.value.code == "code_set_name_missing"
        assert exc.value.path == "provider_config.code_set"

    def test_non_string_code_set_name_is_rejected(self):
        """A non-string name (e.g. a YAML list) is refused as a missing name
        rather than crashing the later membership tests with an unhashable
        TypeError."""
        with pytest.raises(PlanCompileError) as exc:
            validate_code_set_config({"code_set": ["icd10"]})
        assert exc.value.code == "code_set_name_missing"
        assert exc.value.path == "provider_config.code_set"

    @pytest.mark.parametrize("bad_version", [True, False, ["2024"], {"y": 2024}])
    def test_corpus_source_version_non_scalar_error_fields(self, bad_version):
        """A non-scalar (or bool) version pin is refused with the version-
        specific code + path; bool is refused despite being an int subclass so
        a `false` pin cannot silently disable the pin."""
        with pytest.raises(PlanCompileError) as exc:
            validate_code_set_config({"code_set": "icd10", "corpus_source_version": bad_version})
        assert exc.value.code == "code_set_corpus_source_version_invalid"
        assert exc.value.path == "provider_config.corpus_source_version"

    @pytest.mark.parametrize("good_version", ["2024", 2024])
    def test_corpus_source_version_scalar_accepted(self, good_version):
        """A string or unquoted-numeric release id is a valid scalar pin."""
        validate_code_set_config({"code_set": "icd10", "corpus_source_version": good_version})

    @pytest.mark.parametrize("reserved_name", ["cpt", "apr_drg"])
    def test_reserved_licensed_name_error_fields(self, reserved_name):
        """Each reserved licensed corpus is refused as upload-only with the
        licensing-specific code + path -- checked BEFORE the generic not-found
        gate. Parametrized per constant value so every member of
        RESERVED_LICENSED_NAMES has explicit coverage (a module-level set is
        not otherwise exercised value-by-value)."""
        with pytest.raises(PlanCompileError) as exc:
            validate_code_set_config({"code_set": reserved_name})
        assert exc.value.code == "code_set_reserved_licensed_name"
        assert exc.value.path == "provider_config.code_set"

    def test_reserved_licensed_name_allowed_via_customer_source(self):
        """A reserved name passes config validation when supplied as a customer
        upload: the licensing refusal is scoped to shipped loads only. (Config
        validation does not read the corpus; loading is checked elsewhere.)"""
        validate_code_set_config(
            {"code_set": "cpt", "corpus_source": "customer:/some/path.parquet"}
        )

    def test_unknown_shipped_corpus_error_fields(self):
        """The not-found refusal carries its own code + path so callers can
        distinguish it from the licensing and missing-name refusals."""
        with pytest.raises(PlanCompileError) as exc:
            validate_code_set_config({"code_set": "nonexistent_corpus_xyz"})
        assert exc.value.code == "code_set_corpus_not_found"
        assert exc.value.path == "provider_config.code_set"

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
            actual = _get_chapter(code, record.selection.chapter_index)
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

    def test_duplicate_code_now_fails_closed(self, tmp_path: pathlib.Path):
        """HC-2 D2c supersedes the HC-1 LOW-1 'first-wins duplicate' behavior:
        a duplicate code in ANY corpus (shipped or customer) is now rejected
        at load (`code_set_corpus_duplicate_codes`), because the UNIQUE-codes
        invariant makes HMAC-keyed candidate selection unambiguous. The old
        first-wins chapter-index resolution is therefore unreachable through
        the load path -- a duplicate never gets as far as the chapter index."""
        from decoy_engine.transforms._codeset_loader import _get_corpus_record

        tbl = pa.table(
            {
                "code": pa.array(["D01", "D01", "D02"], type=pa.string()),
                "chapter": pa.array(["FIRST", "SECOND", "D"], type=pa.string()),
            }
        )
        path = tmp_path / "dup_codes.parquet"
        pq.write_table(tbl, str(path))

        with pytest.raises(PlanCompileError) as exc_info:
            _get_corpus_record("dup_codes", path, is_shipped=False)
        assert exc_info.value.code == "code_set_corpus_duplicate_codes"


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


# ── HC-2 item 3: reserved licensed corpus names (D2b) ─────────────────────────


class TestReservedLicensedName:
    """CPT and APR-DRG are licensed, not public domain: the engine must never
    ship them under corpus_source: shipped (or absent); the only legal path
    is corpus_source: customer:<path> to the operator's own licensed copy."""

    def test_cpt_shipped_raises_reserved_licensed_name(self):
        with pytest.raises(PlanCompileError) as exc_info:
            validate_code_set_config({"code_set": "cpt"})
        assert exc_info.value.code == "code_set_reserved_licensed_name"

    def test_apr_drg_shipped_raises_reserved_licensed_name(self):
        with pytest.raises(PlanCompileError) as exc_info:
            validate_code_set_config({"code_set": "apr_drg", "corpus_source": "shipped"})
        assert exc_info.value.code == "code_set_reserved_licensed_name"

    def test_reserved_name_check_is_case_insensitive(self):
        """A reserved name must be caught regardless of case (D2b: 'case-normalised')."""
        with pytest.raises(PlanCompileError) as exc_info:
            validate_code_set_config({"code_set": "CPT"})
        assert exc_info.value.code == "code_set_reserved_licensed_name"

    def test_cpt_with_customer_path_is_allowed(self, tmp_path: pathlib.Path):
        """A reserved name WITH a customer path is the intended, allowed flow."""
        codes = [f"CPT{i:03d}" for i in range(5)]
        tbl = pa.table({"code": pa.array(codes, type=pa.string())})
        path = tmp_path / "cpt_customer.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict({"code_set": "cpt", "corpus_source": f"customer:{path}"})
        out = apply_code_set(codes[0], cfg, mode="mask", job_seed=_JOB_SEED)
        assert out in set(codes)


# ── HC-2 item 2: corpus_source_version fail-closed pin (D2a) ─────────────────


class TestCorpusSourceVersionPin:
    """`corpus_source_version`, when set, must match the loaded corpus's
    embedded `CodeSetProvenance.source_version` or the load fails closed --
    for both shipped and customer corpora. Unset (the default) is a no-op."""

    def test_matching_shipped_version_loads(self):
        cfg = CodeSetConfig.from_dict({"code_set": "icd10", "corpus_source_version": "FY2024"})
        out = apply_code_set("I10", cfg, mode="mask", job_seed=_JOB_SEED)
        assert out != "I10"

    def test_mismatched_shipped_version_raises(self):
        cfg = CodeSetConfig.from_dict({"code_set": "icd10", "corpus_source_version": "FY2023"})
        with pytest.raises(PlanCompileError) as exc_info:
            apply_code_set("I10", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc_info.value.code == "code_set_corpus_version_mismatch"
        assert "FY2023" in exc_info.value.message
        assert "FY2024" in exc_info.value.message

    def test_matching_customer_version_loads(self, tmp_path: pathlib.Path):
        tbl = pa.table(
            {"code": pa.array(["P01", "P02"], type=pa.string())},
            metadata={
                b"decoy_corpus": b"pinned_customer",
                b"source": b"Internal registry",
                b"source_version": b"2026-01",
                b"effective_date": b"2026-01-01",
                b"license": b"Proprietary",
            },
        )
        path = tmp_path / "pinned_customer.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "pinned_customer",
                "corpus_source": f"customer:{path}",
                "corpus_source_version": "2026-01",
            }
        )
        out = apply_code_set("P01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert out in {"P01", "P02"}

    def test_mismatched_customer_version_raises(self, tmp_path: pathlib.Path):
        tbl = pa.table(
            {"code": pa.array(["Q01", "Q02"], type=pa.string())},
            metadata={
                b"decoy_corpus": b"mismatched_customer",
                b"source": b"Internal registry",
                b"source_version": b"2026-01",
                b"effective_date": b"2026-01-01",
                b"license": b"Proprietary",
            },
        )
        path = tmp_path / "mismatched_customer.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "mismatched_customer",
                "corpus_source": f"customer:{path}",
                "corpus_source_version": "2099-01",
            }
        )
        with pytest.raises(PlanCompileError) as exc_info:
            apply_code_set("Q01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc_info.value.code == "code_set_corpus_version_mismatch"

    def test_pin_with_no_embedded_provenance_raises(self, tmp_path: pathlib.Path):
        """A customer corpus with NO provenance at all normally only warns
        (provenance is optional for customer corpora); a version pin still
        fails closed, since there is no source_version to satisfy the pin."""
        tbl = pa.table({"code": pa.array(["R01", "R02"], type=pa.string())})
        path = tmp_path / "no_prov_pinned.parquet"
        pq.write_table(tbl, str(path))

        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "no_prov_pinned",
                "corpus_source": f"customer:{path}",
                "corpus_source_version": "FY2024",
            }
        )
        with pytest.raises(PlanCompileError) as exc_info:
            apply_code_set("R01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc_info.value.code == "code_set_corpus_version_mismatch"

    def test_unset_pin_is_unchanged_behavior(self):
        """No corpus_source_version set -> no pin check, same as pre-HC-2."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        assert cfg.corpus_source_version is None
        out = apply_code_set("I10", cfg, mode="mask", job_seed=_JOB_SEED)
        assert out != "I10"


# ── HC-2 item 4: generic corpus-agnostic schema invariants (D2c) ─────────────


class TestSchemaInvariants:
    """Non-null, non-empty, unique codes, and (when present) a coherent
    chapter column -- factored into `_check_corpus_schema`, shared by the
    load path and `verify_corpus`. Deliberately corpus-agnostic: no
    code-system-specific regexes, no mandatory `description` (deferred to
    HC-1 slice 2)."""

    def test_null_code_raises(self, tmp_path: pathlib.Path):
        tbl = pa.table({"code": pa.array(["A01", None, "A03"], type=pa.string())})
        path = tmp_path / "null_code.parquet"
        pq.write_table(tbl, str(path))
        cfg = CodeSetConfig.from_dict({"code_set": "custom", "corpus_source": f"customer:{path}"})
        with pytest.raises(PlanCompileError) as exc_info:
            apply_code_set("A01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc_info.value.code == "code_set_corpus_null_code"

    def test_empty_string_code_raises(self, tmp_path: pathlib.Path):
        tbl = pa.table({"code": pa.array(["A01", "", "A03"], type=pa.string())})
        path = tmp_path / "empty_code.parquet"
        pq.write_table(tbl, str(path))
        cfg = CodeSetConfig.from_dict({"code_set": "custom", "corpus_source": f"customer:{path}"})
        with pytest.raises(PlanCompileError) as exc_info:
            apply_code_set("A01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc_info.value.code == "code_set_corpus_empty_code"

    def test_duplicate_codes_raise(self, tmp_path: pathlib.Path):
        tbl = pa.table({"code": pa.array(["A01", "A01", "A03"], type=pa.string())})
        path = tmp_path / "dup_code.parquet"
        pq.write_table(tbl, str(path))
        cfg = CodeSetConfig.from_dict({"code_set": "custom", "corpus_source": f"customer:{path}"})
        with pytest.raises(PlanCompileError) as exc_info:
            apply_code_set("A01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc_info.value.code == "code_set_corpus_duplicate_codes"

    def test_incoherent_chapter_raises(self, tmp_path: pathlib.Path):
        tbl = pa.table(
            {
                "code": pa.array(["A01", "A02"], type=pa.string()),
                "chapter": pa.array(["A", None], type=pa.string()),
            }
        )
        path = tmp_path / "incoherent_chapter.parquet"
        pq.write_table(tbl, str(path))
        cfg = CodeSetConfig.from_dict({"code_set": "custom", "corpus_source": f"customer:{path}"})
        with pytest.raises(PlanCompileError) as exc_info:
            apply_code_set("A01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc_info.value.code == "code_set_corpus_incoherent_chapter"


# ── HC-2 item 1: verify_corpus standalone structured-report primitive ───────


class TestVerifyCorpus:
    """`verify_corpus(path)` runs the same schema + provenance checks the
    load path runs, WITHOUT running a masking job, and never raises --
    failures become `problems` on a frozen `CorpusVerifyReport`."""

    def test_valid_corpus_is_ok(self, tmp_path: pathlib.Path):
        from decoy_engine.transforms._codeset_loader import verify_corpus

        codes = [f"V{i:02d}" for i in range(5)]
        tbl = pa.table({"code": pa.array(codes, type=pa.string())})
        path = tmp_path / "valid.parquet"
        pq.write_table(tbl, str(path))

        report = verify_corpus(path)
        assert report.ok is True
        assert report.row_count == 5
        assert report.problems == ()

    def test_missing_code_column_is_not_ok(self, tmp_path: pathlib.Path):
        from decoy_engine.transforms._codeset_loader import verify_corpus

        tbl = pa.table({"notcode": pa.array(["X01"], type=pa.string())})
        path = tmp_path / "bad.parquet"
        pq.write_table(tbl, str(path))

        report = verify_corpus(path)
        assert report.ok is False
        assert any("code_set_corpus_missing_code_column" in p for p in report.problems)

    def test_empty_corpus_is_not_ok(self, tmp_path: pathlib.Path):
        from decoy_engine.transforms._codeset_loader import verify_corpus

        tbl = pa.table({"code": pa.array([], type=pa.string())})
        path = tmp_path / "empty.parquet"
        pq.write_table(tbl, str(path))

        report = verify_corpus(path)
        assert report.ok is False
        assert any("code_set_corpus_empty" in p for p in report.problems)

    def test_duplicate_codes_is_not_ok(self, tmp_path: pathlib.Path):
        from decoy_engine.transforms._codeset_loader import verify_corpus

        tbl = pa.table({"code": pa.array(["D01", "D01"], type=pa.string())})
        path = tmp_path / "dup.parquet"
        pq.write_table(tbl, str(path))

        report = verify_corpus(path)
        assert report.ok is False
        assert any("code_set_corpus_duplicate_codes" in p for p in report.problems)

    def test_null_code_is_not_ok(self, tmp_path: pathlib.Path):
        from decoy_engine.transforms._codeset_loader import verify_corpus

        tbl = pa.table({"code": pa.array(["N01", None], type=pa.string())})
        path = tmp_path / "null.parquet"
        pq.write_table(tbl, str(path))

        report = verify_corpus(path)
        assert report.ok is False
        assert any("code_set_corpus_null_code" in p for p in report.problems)

    def test_incomplete_provenance_customer_corpus_is_not_ok(self, tmp_path: pathlib.Path):
        from decoy_engine.transforms._codeset_loader import verify_corpus

        tbl = pa.table(
            {"code": pa.array(["I01", "I02"], type=pa.string())},
            metadata={b"decoy_corpus": b"partial", b"source": b"Some Source"},
        )
        path = tmp_path / "partial_prov.parquet"
        pq.write_table(tbl, str(path))

        report = verify_corpus(path)
        assert report.ok is False
        assert any("code_set_corpus_missing_provenance" in p for p in report.problems)

    def test_provenance_summary_has_no_raw_codes(self, tmp_path: pathlib.Path):
        from decoy_engine.transforms._codeset_loader import verify_corpus

        tbl = pa.table(
            {"code": pa.array(["S01", "S02"], type=pa.string())},
            metadata={
                b"decoy_corpus": b"summary_check",
                b"source": b"Internal registry",
                b"source_version": b"2026-01",
                b"effective_date": b"2026-01-01",
                b"license": b"Proprietary",
            },
        )
        path = tmp_path / "summary_check.parquet"
        pq.write_table(tbl, str(path))

        report = verify_corpus(path)
        assert report.ok is True
        assert report.provenance is not None
        assert report.provenance["source_version"] == "2026-01"
        assert "codes" not in report.provenance
        assert "rows" not in report.provenance
        assert "S01" not in str(report.provenance)
        assert "S02" not in str(report.provenance)

    def test_verify_corpus_exported_from_code_set_module(self):
        """Re-exported from the package surface, same as load_corpus_provenance."""
        from decoy_engine.transforms.code_set import CorpusVerifyReport, verify_corpus

        assert callable(verify_corpus)
        assert CorpusVerifyReport is not None


class TestTwoModelGateRemediation:
    """Dennis + Codex adversarial-gate findings on the HC-2 build, each fixed
    at the source (fail-closed / coded, no silent bypass)."""

    def test_pin_enforced_on_supplied_record_route(self):
        # Codex HIGH: apply_code_set with a PASSED-IN record and a pinned config
        # must re-verify the pin, not trust the record. A record resolved under
        # a different (unpinned) config must not slip past a later pinned apply.
        from decoy_engine.transforms.code_set import resolve_corpus_record

        record = resolve_corpus_record(CodeSetConfig.from_dict({"code_set": "icd10"}))
        pinned = CodeSetConfig.from_dict({"code_set": "icd10", "corpus_source_version": "FY2023"})
        with pytest.raises(PlanCompileError) as exc:
            apply_code_set("I10", pinned, mode="mask", job_seed=_JOB_SEED, corpus_record=record)
        assert exc.value.code == "code_set_corpus_version_mismatch"

    @pytest.mark.parametrize("source", [None, "Shipped", " shipped ", " ", "garbage"])
    def test_reserved_name_blocked_for_all_shipped_like_sources(self, source):
        # Codex/Dennis MEDIUM: any non-`customer:` source is a shipped load, so
        # cpt/apr_drg must be refused with the licensing error, not slip to a
        # later generic "not found".
        cfg = {"code_set": "cpt"}
        if source is not None:
            cfg["corpus_source"] = source
        with pytest.raises(PlanCompileError) as exc:
            validate_code_set_config(cfg)
        assert exc.value.code == "code_set_reserved_licensed_name"

    def test_reserved_name_with_customer_path_allowed(self, tmp_path: pathlib.Path):
        tbl = pa.table({"code": pa.array(["99213", "99214"], type=pa.string())})
        path = tmp_path / "cpt.parquet"
        pq.write_table(tbl, str(path))
        cfg = CodeSetConfig.from_dict({"code_set": "cpt", "corpus_source": f"customer:{path}"})
        assert apply_code_set("99213", cfg, mode="mask", job_seed=_JOB_SEED) in {"99213", "99214"}

    def test_numeric_version_pin_coerced_and_matches(self):
        # Dennis MEDIUM: unquoted-YAML numeric release id (int) must compare
        # equal to the corpus's always-string embedded source_version.
        cfg = CodeSetConfig.from_dict({"code_set": "ndc", "corpus_source_version": 2024})
        assert cfg.corpus_source_version == "2024"
        assert apply_code_set("0002-1975", cfg, mode="mask", job_seed=_JOB_SEED) is not None

    @pytest.mark.parametrize("bad", [False, True, ["2024"], {"v": 1}])
    def test_non_scalar_version_pin_rejected_not_failed_open(self, bad):
        # Codex MEDIUM: a bool/list/dict pin must be a coded error, never
        # silently collapse to "unpinned" (a false pin disabling verification).
        with pytest.raises(PlanCompileError) as exc:
            CodeSetConfig.from_dict({"code_set": "icd10", "corpus_source_version": bad})
        assert exc.value.code == "code_set_corpus_source_version_invalid"

    @pytest.mark.parametrize("bad_name", [["icd10"], {"x": 1}, 123])
    def test_non_string_code_set_name_coded_not_typeerror(self, bad_name):
        # Codex MEDIUM: a non-string code_set must raise a coded compile error,
        # not a raw TypeError from the frozenset membership test.
        with pytest.raises(PlanCompileError) as exc:
            validate_code_set_config({"code_set": bad_name})
        assert exc.value.code == "code_set_name_missing"

    def test_verify_corpus_accepts_str_path_without_raising(self):
        # Codex LOW: the never-raises contract must hold for a path-like str too.
        from decoy_engine.transforms.code_set import verify_corpus

        report = verify_corpus("/nonexistent/does-not-exist.parquet")
        assert report.ok is False
        assert report.problems and report.problems[0].startswith("code_set_corpus_read_error")


class TestHoleArithmeticExhaustive:
    """hole_candidate_count/hole_resolve, exhaustively, against a naive
    filter -- deterministic, unlike the end-to-end sweeps below (which only
    sample whichever idx values the real HMAC/derive_index outputs happen to
    land on). Every idx in range(candidate_count), for every possible
    position (None, first, middle, last), over several sequence lengths."""

    @staticmethod
    def _naive_filtered(seq: list[int], position: int | None) -> list[int]:
        if position is None:
            return list(seq)
        return [x for i, x in enumerate(seq) if i != position]

    @pytest.mark.parametrize("seq_len", [1, 2, 5, 37])
    def test_hole_resolve_matches_naive_filter_for_every_idx_and_position(self, seq_len):
        from decoy_engine.transforms._codeset_index import hole_candidate_count, hole_resolve

        seq = list(range(seq_len))
        for position in (None, *range(seq_len)):
            naive = self._naive_filtered(seq, position)
            candidate_count = hole_candidate_count(seq_len, position)
            assert candidate_count == len(naive)
            for idx in range(candidate_count):
                real_idx = hole_resolve(idx, position)
                assert seq[real_idx] == naive[idx], (
                    f"seq_len={seq_len} position={position} idx={idx}: "
                    f"hole_resolve gave seq[{real_idx}]={seq[real_idx]}, "
                    f"naive filter gave {naive[idx]}"
                )


# ── HC-1 slice 2: index-with-hole selection parity ────────────────────────────
#
# code_set.py used to run three O(corpus)/O(bucket) list comprehensions per
# masked VALUE (candidate exclusion in _pick_mask and twice in
# _apply_chapter_preserve). HC-1 slice 2 replaces them with precomputed
# indices (code_index, chapter_buckets, bucket_code_index) plus an
# "index-with-hole" arithmetic mapping (hole_candidate_count/hole_resolve)
# that reproduces `[r for r in seq if code != value][idx]` without
# materializing the filtered list. This class is the mandatory acceptance
# gate: a NAIVE reference re-implementing the OLD filter-based selection,
# checked byte-identical to the NEW precomputed selection across a broad
# sweep of mask mode, gen mode, chapter_preserve on/off, member/non-member
# inputs, and the two fail-closed cases (single-row corpus, sole-member
# bucket). A failure here means the optimization changed behavior.


def _naive_pick_from_candidates(
    key_value: str, candidates: list, *, mask_key: bytes, namespace: str | None
) -> str:
    """Reference: the pre-HC-1-slice-2 HMAC selection over an already-
    materialized candidate list (verbatim old `_pick_from_candidates` body)."""
    from decoy_engine.determinism import derive
    from decoy_engine.internal.crypto import hmac_hex
    from decoy_engine.transforms.code_set import _KEYED_SALT

    hmac_key = derive(mask_key, namespace or "code_set", _KEYED_SALT)
    hex_digest = hmac_hex(hmac_key, key_value)
    assert hex_digest is not None
    idx = int(hex_digest[:8], 16) % len(candidates)
    return str(candidates[idx]["code"])


def _naive_pick_mask(value: str, rows: list, *, mask_key: bytes, namespace: str | None) -> str:
    """Reference: the pre-HC-1-slice-2 `_pick_mask` body, verbatim -- builds
    the candidate list by filtering, instead of the code_index hole lookup."""
    candidates = [r for r in rows if str(r["code"]) != value]
    if not candidates:
        raise PlanCompileError(
            code="code_set_single_row_corpus",
            path="provider_config.code_set",
            message="naive reference: single-row corpus equals input.",
        )
    return _naive_pick_from_candidates(value, candidates, mask_key=mask_key, namespace=namespace)


def _naive_apply_chapter_preserve(
    value: str,
    rows: list,
    chapter_index: dict,
    *,
    mode: str,
    job_seed: bytes,
    namespace: str | None,
    row_index: int,
) -> str:
    """Reference: the pre-HC-1-slice-2 `_apply_chapter_preserve` body,
    verbatim -- filters the bucket and the candidate set with list
    comprehensions instead of chapter_buckets/bucket_code_index lookups.
    `_get_chapter` itself is untouched by HC-1 slice 2 (already O(1) since
    HC-1 slice 1), so it is reused rather than re-implemented here."""
    from decoy_engine.determinism import derive_index
    from decoy_engine.transforms.code_set import _get_chapter

    if not rows or "chapter" not in rows[0]:
        raise PlanCompileError(
            code="code_set_chapter_column_missing",
            path="provider_config.chapter_preserve",
            message="naive reference: no chapter column.",
        )

    input_chapter = _get_chapter(value, chapter_index)
    if input_chapter is None:
        input_chapter = value[0] if value else ""

    bucket = [r for r in rows if str(r.get("chapter", "")) == input_chapter]
    if not bucket:
        raise PlanCompileError(
            code="code_set_chapter_absent",
            path="provider_config.chapter_preserve",
            message="naive reference: chapter absent from corpus.",
        )

    candidates = [r for r in bucket if str(r["code"]) != value]
    if not candidates:
        raise PlanCompileError(
            code="code_set_sole_member_bucket",
            path="provider_config.chapter_preserve",
            message="naive reference: sole-member bucket.",
        )

    if mode == "mask":
        return _naive_pick_from_candidates(
            value, candidates, mask_key=job_seed, namespace=namespace
        )
    idx = derive_index(
        job_seed[:8], namespace, row_index.to_bytes(8, "big"), pool_size=len(candidates)
    )
    return str(candidates[idx]["code"])


def _build_parity_corpus(tmp_path: pathlib.Path) -> pathlib.Path:
    """A few-hundred-code, multi-chapter synthetic corpus, sized and shaped so
    candidate holes land at the first, middle, and last position of both the
    full corpus and individual chapter buckets:

      A: 1 code   (sole-member bucket -- forces code_set_sole_member_bucket)
      B: 50 codes (mid-size bucket)
      C: 100 codes (largest bucket, exercises many hole positions)
      D: 2 codes  (near-sole -- exactly one valid replacement)
      E: 150 codes (last chapter in sort order -- exercises the tail)

    Codes are zero-padded so ascending string sort matches ascending numeric
    order within each chapter, and chapters sort A < B < C < D < E, so the
    first/last row of the whole corpus falls in the first/last chapter.
    """
    codes: list[str] = []
    chapters: list[str] = []
    sizes = {"A": 1, "B": 50, "C": 100, "D": 2, "E": 150}
    for chapter, n in sizes.items():
        for i in range(1, n + 1):
            codes.append(f"{chapter}{i:04d}")
            chapters.append(chapter)

    tbl = pa.table(
        {
            "code": pa.array(codes, type=pa.string()),
            "chapter": pa.array(chapters, type=pa.string()),
        }
    )
    path = tmp_path / "parity_corpus.parquet"
    pq.write_table(tbl, str(path))
    return path


class TestHoleSelectionParity:
    """Mandatory acceptance gate for HC-1 slice 2 (see module comment above)."""

    @pytest.fixture
    def corpus_path(self, tmp_path: pathlib.Path) -> pathlib.Path:
        return _build_parity_corpus(tmp_path)

    @staticmethod
    def _sweep_inputs() -> list[str]:
        """Member codes at first/middle/last position of each bucket and of
        the full corpus, plus non-member codes (absent entirely, and one
        whose derived first-char chapter is absent from the corpus)."""
        member_inputs = [
            "A0001",  # sole member of chapter A; also the first row overall
            "B0001",  # first of chapter B
            "B0025",  # middle of chapter B
            "B0050",  # last of chapter B
            "C0001",  # first of chapter C
            "C0050",  # middle of chapter C
            "C0100",  # last of chapter C
            "D0001",  # first of chapter D (2-member bucket)
            "D0002",  # last of chapter D (2-member bucket)
            "E0001",  # first of chapter E
            "E0075",  # middle of chapter E
            "E0150",  # last of chapter E; also the last row overall
        ]
        non_member_inputs = [
            "A9999",  # in an existing chapter, but not a corpus member
            "B9999",
            "C9999",
            "ZZZZ",  # first-char "Z": chapter absent from the corpus entirely
            "Q0001",  # first-char "Q": chapter absent from the corpus entirely
            "",  # empty string: _get_chapter's own edge case
        ]
        return member_inputs + non_member_inputs

    def test_mask_mode_matches_naive_reference(self, corpus_path: pathlib.Path):
        """Non-chapter-preserve mask mode: sweep every input, member and
        non-member, and assert the precomputed selection is byte-identical
        to the naive filter-based reference (or raises the same error)."""
        cfg = CodeSetConfig.from_dict(
            {"code_set": "parity", "corpus_source": f"customer:{corpus_path}"}
        )
        from decoy_engine.transforms.code_set import load_corpus

        rows = load_corpus("parity", corpus_path)
        checked = 0
        for value in self._sweep_inputs():
            new_result = _run_or_error(apply_code_set, value, cfg, mode="mask", job_seed=_JOB_SEED)
            naive_result = _run_or_error(
                _naive_pick_mask, value, rows, mask_key=_JOB_SEED, namespace=None
            )
            assert new_result == naive_result, f"mask mode mismatch for {value!r}"
            checked += 1
        assert checked == len(self._sweep_inputs())

    def test_gen_mode_no_chapter_preserve_matches_naive_reference(self, corpus_path: pathlib.Path):
        """Non-chapter-preserve gen mode draws from the full pool (unchanged
        by HC-1 slice 2), but the shared derive_index/hole_resolve plumbing
        is re-verified here too since _pick_gen's signature changed."""
        cfg = CodeSetConfig.from_dict(
            {"code_set": "parity", "corpus_source": f"customer:{corpus_path}"}
        )
        from decoy_engine.determinism import derive_index
        from decoy_engine.transforms.code_set import load_corpus

        rows = load_corpus("parity", corpus_path)
        ns = "parity.gen"
        for row_index in range(30):
            new_result = apply_code_set(
                "unused-gen-hint",
                cfg,
                mode="gen",
                job_seed=_JOB_SEED,
                namespace=ns,
                row_index=row_index,
            )
            idx = derive_index(_JOB_SEED[:8], ns, row_index.to_bytes(8, "big"), pool_size=len(rows))
            naive_result = str(rows[idx]["code"])
            assert new_result == naive_result, f"gen mode mismatch at row_index {row_index}"

    def test_chapter_preserve_mask_matches_naive_reference(self, corpus_path: pathlib.Path):
        """chapter_preserve mask mode: sweep every input, asserting
        byte-identical output (or identical raised error code) against the
        naive bucket-filter reference."""
        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "parity",
                "corpus_source": f"customer:{corpus_path}",
                "chapter_preserve": True,
            }
        )
        from decoy_engine.transforms._codeset_loader import _get_corpus_record

        record = _get_corpus_record("parity", corpus_path, is_shipped=False)
        for value in self._sweep_inputs():
            new_result = _run_or_error(apply_code_set, value, cfg, mode="mask", job_seed=_JOB_SEED)
            naive_result = _run_or_error(
                _naive_apply_chapter_preserve,
                value,
                record.rows,
                record.selection.chapter_index,
                mode="mask",
                job_seed=_JOB_SEED,
                namespace=None,
                row_index=0,
            )
            assert new_result == naive_result, f"chapter_preserve mask mismatch for {value!r}"

    def test_chapter_preserve_gen_matches_naive_reference(self, corpus_path: pathlib.Path):
        """chapter_preserve gen mode: sweep every input across several
        row_index values, asserting byte-identical output (or identical
        raised error code) against the naive bucket-filter reference. This is
        the path with the most subtle hole math (derive_index-selected index
        remapped through the bucket's hole), so it gets the widest sweep."""
        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "parity",
                "corpus_source": f"customer:{corpus_path}",
                "chapter_preserve": True,
            }
        )
        from decoy_engine.transforms._codeset_loader import _get_corpus_record

        record = _get_corpus_record("parity", corpus_path, is_shipped=False)
        ns = "parity.chapter.gen"
        checked = 0
        for value in self._sweep_inputs():
            for row_index in range(8):
                new_result = _run_or_error(
                    apply_code_set,
                    value,
                    cfg,
                    mode="gen",
                    job_seed=_JOB_SEED,
                    namespace=ns,
                    row_index=row_index,
                )
                naive_result = _run_or_error(
                    _naive_apply_chapter_preserve,
                    value,
                    record.rows,
                    record.selection.chapter_index,
                    mode="gen",
                    job_seed=_JOB_SEED,
                    namespace=ns,
                    row_index=row_index,
                )
                assert new_result == naive_result, (
                    f"chapter_preserve gen mismatch for {value!r} at row_index {row_index}"
                )
                checked += 1
        assert checked == len(self._sweep_inputs()) * 8

    def test_single_row_corpus_raises_matching_error(self, tmp_path: pathlib.Path):
        """The candidate_count == 0 fail-closed path (single-row corpus,
        mask mode): both implementations raise code_set_single_row_corpus."""
        tbl = pa.table({"code": pa.array(["ONLY1"], type=pa.string())})
        path = tmp_path / "single_row.parquet"
        pq.write_table(tbl, str(path))
        cfg = CodeSetConfig.from_dict({"code_set": "single", "corpus_source": f"customer:{path}"})

        with pytest.raises(PlanCompileError) as new_exc:
            apply_code_set("ONLY1", cfg, mode="mask", job_seed=_JOB_SEED)
        assert new_exc.value.code == "code_set_single_row_corpus"

        from decoy_engine.transforms.code_set import load_corpus

        with pytest.raises(PlanCompileError) as naive_exc:
            _naive_pick_mask(
                "ONLY1", load_corpus("single", path), mask_key=_JOB_SEED, namespace=None
            )
        assert naive_exc.value.code == "code_set_single_row_corpus"

    def test_sole_member_bucket_raises_matching_error(self, corpus_path: pathlib.Path):
        """The candidate_count == 0 fail-closed path (sole-member bucket,
        chapter_preserve): both implementations raise code_set_sole_member_bucket
        for chapter A's only code."""
        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "parity",
                "corpus_source": f"customer:{corpus_path}",
                "chapter_preserve": True,
            }
        )
        with pytest.raises(PlanCompileError) as new_exc:
            apply_code_set("A0001", cfg, mode="mask", job_seed=_JOB_SEED)
        assert new_exc.value.code == "code_set_sole_member_bucket"

        from decoy_engine.transforms._codeset_loader import _get_corpus_record

        record = _get_corpus_record("parity", corpus_path, is_shipped=False)
        with pytest.raises(PlanCompileError) as naive_exc:
            _naive_apply_chapter_preserve(
                "A0001",
                record.rows,
                record.selection.chapter_index,
                mode="mask",
                job_seed=_JOB_SEED,
                namespace=None,
                row_index=0,
            )
        assert naive_exc.value.code == "code_set_sole_member_bucket"


def _run_or_error(fn, *args, **kwargs):
    """Call fn(*args, **kwargs); return the result, or the raised
    PlanCompileError's `.code` string if it raises. Lets the parity sweep
    assert "same result OR same failure mode" with one comparison instead of
    a try/except at every call site."""
    try:
        return fn(*args, **kwargs)
    except PlanCompileError as exc:
        return f"<raised:{exc.code}>"


# ── TQ crown-jewels: mutation-survivor kills (2026-07-26) ─────────────────────
#
# Oracle tests targeting the 108 surviving mutants of a focused mutmut run on
# transforms/code_set.py. Invariants pinned here come from the module docstring
# (mask determinism + domain exclusion, gen namespace-binding, chapter_preserve
# fail-closed guards, corpus resolution) and are graded per
# docs/quality/module-test-quality-playbook.md. See
# docs/quality/mutation-ledgers/transforms_code_set.md for the full ledger.


def _write_corpus(
    path: pathlib.Path,
    codes: list[str],
    *,
    chapters: list[str] | None = None,
    provenance: dict[bytes, bytes] | None = None,
) -> None:
    """Write a customer corpus Parquet, optionally with a chapter column and a
    provenance stamp, for the survivor-kill fixtures below."""
    cols: dict[str, pa.Array] = {"code": pa.array(codes, type=pa.string())}
    if chapters is not None:
        cols["chapter"] = pa.array(chapters, type=pa.string())
    tbl = pa.table(cols, metadata=provenance)
    pq.write_table(tbl, str(path))


_COMPLETE_CUSTOMER_PROV: dict[bytes, bytes] = {
    b"decoy_corpus": b"pinned",
    b"source": b"Internal registry",
    b"source_version": b"2024",
    b"effective_date": b"2024-01-01",
    b"license": b"Proprietary",
}


class TestApplyDefaultsAndDispatch:
    """apply_code_set parameter defaults and the mask/gen dispatch surface."""

    def test_mode_defaults_to_mask(self):
        """Omitting mode= must mask (not raise unsupported-mode): the default is
        'mask'. Kills the mode-default string mutants."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        out = apply_code_set("I21.9", cfg, job_seed=_JOB_SEED)
        assert out != "I21.9"
        with_explicit = apply_code_set("I21.9", cfg, mode="mask", job_seed=_JOB_SEED)
        assert out == with_explicit

    def test_row_index_defaults_to_zero(self):
        """Omitting row_index in gen mode must equal row_index=0. Kills the
        row_index-default mutant (0 -> 1)."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        seed = b"\x07" * 8
        ns = "col.ns"
        default = apply_code_set("", cfg, mode="gen", job_seed=seed, namespace=ns)
        explicit0 = apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=0, namespace=ns)
        explicit1 = apply_code_set("", cfg, mode="gen", job_seed=seed, row_index=1, namespace=ns)
        assert default == explicit0
        # Guard: row_index 0 and 1 genuinely differ, so the check above discriminates.
        assert explicit0 != explicit1

    def test_mask_namespace_is_threaded_into_key_derivation(self):
        """The namespace binds the mask key (derive(..., namespace, ...)): a
        distinct namespace yields a distinct replacement. Kills the mask
        namespace=None dispatch mutants (apply + _pick_mask)."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        with_ns = apply_code_set("I21.9", cfg, mode="mask", job_seed=_JOB_SEED, namespace="col.a")
        without_ns = apply_code_set("I21.9", cfg, mode="mask", job_seed=_JOB_SEED, namespace=None)
        assert with_ns != without_ns

    def test_gen_without_namespace_fails_closed(self):
        """gen mode with namespace=None raises code_set_gen_requires_namespace at
        path 'namespace'. Kills the non-chapter gen-namespace guard mutants
        (code/path values, dropped kwargs -> TypeError)."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        with pytest.raises(PlanCompileError) as exc:
            apply_code_set("x", cfg, mode="gen", job_seed=_JOB_SEED, namespace=None)
        assert exc.value.code == "code_set_gen_requires_namespace"
        assert exc.value.path == "namespace"

    def test_unsupported_mode_raises_value_error(self):
        """An unknown mode is a ValueError (not a PlanCompileError)."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        with pytest.raises(ValueError):
            apply_code_set("x", cfg, mode="bogus", job_seed=_JOB_SEED)


class TestDescribeLoadedCorpusEvidence:
    """describe_loaded_corpus dict keys and no-provenance fallback values."""

    def test_shipped_reports_all_provenance_values(self):
        """Every evidence key is present with the shipped corpus's real values.
        Kills the dict-KEY mutations for source/license."""
        from decoy_engine.transforms.code_set import describe_loaded_corpus

        cfg = CodeSetConfig.from_dict({"code_set": "icd10"})
        summary = describe_loaded_corpus(cfg)
        assert summary["source"]  # non-empty
        assert summary["source_version"] == "FY2024"
        assert summary["effective_date"] == "2023-10-01"
        assert summary["license"]  # non-empty
        assert summary["is_seed"] is True

    def test_no_provenance_customer_fallback_values(self, tmp_path: pathlib.Path):
        """A customer corpus with no provenance surfaces the empty-string /
        False fallbacks under the exact evidence keys. Kills the fallback-value
        mutants (source/source_version/effective_date/license -> 'XXXX',
        is_seed -> True) and every dict-KEY mutant (a renamed key KeyErrors
        here)."""
        from decoy_engine.transforms.code_set import describe_loaded_corpus

        path = tmp_path / "no_prov.parquet"
        _write_corpus(path, ["N01", "N02", "N03"])
        cfg = CodeSetConfig.from_dict({"code_set": "np", "corpus_source": f"customer:{path}"})
        summary = describe_loaded_corpus(cfg)
        assert summary["source"] == ""
        assert summary["source_version"] == ""
        assert summary["effective_date"] == ""
        assert summary["license"] == ""
        assert summary["is_seed"] is False
        assert summary["row_count"] == 3


class TestGetChapterFallback:
    """_get_chapter unknown-code first-character fallback."""

    def test_unknown_code_uses_first_character(self):
        """An unknown code's chapter is its FIRST character (code[0]), not the
        second. Kills the code[0] -> code[1] mutant (a code whose first two
        characters differ is required to see it)."""
        from decoy_engine.transforms.code_set import _get_chapter

        assert _get_chapter("XY99", {"AA": "A"}) == "X"

    def test_empty_code_and_known_code(self):
        """Empty code -> None; a known code maps via the index."""
        from decoy_engine.transforms.code_set import _get_chapter

        assert _get_chapter("", {"AA": "A"}) is None
        assert _get_chapter("AA", {"AA": "Z"}) == "Z"


class TestChapterPreserveGuards:
    """chapter_preserve fail-closed guards: machine fields (code/path) and the
    control flow that selects each guard."""

    def test_missing_chapter_column_code_and_path(self, tmp_path: pathlib.Path):
        """A corpus with no 'chapter' column fails as code_set_chapter_column_missing
        at path provider_config.chapter_preserve. Kills the guard's or->and
        control-flow mutant (which would misroute to chapter_absent) and its
        code/path value mutants."""
        path = tmp_path / "no_chapter.parquet"
        _write_corpus(path, ["A01", "A02"])
        cfg = CodeSetConfig.from_dict(
            {"code_set": "nc", "chapter_preserve": True, "corpus_source": f"customer:{path}"}
        )
        with pytest.raises(PlanCompileError) as exc:
            apply_code_set("A01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc.value.code == "code_set_chapter_column_missing"
        assert exc.value.path == "provider_config.chapter_preserve"

    def test_single_row_chapter_corpus_is_plan_error_not_indexerror(self, tmp_path: pathlib.Path):
        """A single-row corpus with a chapter column must fail closed as a typed
        PlanCompileError (sole-member bucket), never an IndexError. Kills the
        rows[0] -> rows[1] mutant (which IndexErrors on a one-row corpus)."""
        path = tmp_path / "one_row.parquet"
        _write_corpus(path, ["Q01"], chapters=["Q"])
        cfg = CodeSetConfig.from_dict(
            {"code_set": "one", "chapter_preserve": True, "corpus_source": f"customer:{path}"}
        )
        with pytest.raises(PlanCompileError) as exc:
            apply_code_set("Q01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc.value.code == "code_set_sole_member_bucket"

    def test_chapter_from_index_not_first_character(self, tmp_path: pathlib.Path):
        """The input's chapter comes from the corpus index, not the code's first
        character: with a chapter ('CARDIO') unrelated to the code prefix, the
        replacement is drawn from that chapter. Kills the input_chapter
        derivation mutants (None / _get_chapter arg-nulling / is-None flip that
        collapse to value[0])."""
        path = tmp_path / "named_chapter.parquet"
        _write_corpus(path, ["A01", "A02"], chapters=["CARDIO", "CARDIO"])
        cfg = CodeSetConfig.from_dict(
            {"code_set": "nch", "chapter_preserve": True, "corpus_source": f"customer:{path}"}
        )
        out = apply_code_set("A01", cfg, mode="mask", job_seed=_JOB_SEED)
        assert out == "A02"

    def test_chapter_absent_path(self, tmp_path: pathlib.Path):
        """An input whose chapter is not in the corpus fails as
        code_set_chapter_absent at path provider_config.chapter_preserve. Kills
        the chapter_absent path-field mutants."""
        path = tmp_path / "two_chapters.parquet"
        _write_corpus(path, ["A01", "A02", "B01", "B02"], chapters=["A", "A", "B", "B"])
        cfg = CodeSetConfig.from_dict(
            {"code_set": "tc", "chapter_preserve": True, "corpus_source": f"customer:{path}"}
        )
        with pytest.raises(PlanCompileError) as exc:
            apply_code_set("Z99", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc.value.code == "code_set_chapter_absent"
        assert exc.value.path == "provider_config.chapter_preserve"

    def test_sole_member_bucket_code_and_path(self, tmp_path: pathlib.Path):
        """A chapter with a single code fails as code_set_sole_member_bucket at
        path provider_config.chapter_preserve. Kills the sole-member path-field
        mutants."""
        path = tmp_path / "sole.parquet"
        _write_corpus(path, ["Z99", "A01", "A02"], chapters=["Z", "A", "A"])
        cfg = CodeSetConfig.from_dict(
            {"code_set": "sm", "chapter_preserve": True, "corpus_source": f"customer:{path}"}
        )
        with pytest.raises(PlanCompileError) as exc:
            apply_code_set("Z99", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc.value.code == "code_set_sole_member_bucket"
        assert exc.value.path == "provider_config.chapter_preserve"

    def test_chapter_preserve_mask_namespace_threaded(self):
        """chapter_preserve mask also binds the namespace into the key: a
        distinct namespace yields a distinct same-chapter replacement. Kills the
        chapter_preserve mask namespace=None dispatch mutant."""
        cfg = CodeSetConfig.from_dict({"code_set": "icd10", "chapter_preserve": True})
        with_ns = apply_code_set("I21.9", cfg, mode="mask", job_seed=_JOB_SEED, namespace="col.a")
        without_ns = apply_code_set("I21.9", cfg, mode="mask", job_seed=_JOB_SEED, namespace=None)
        assert with_ns != without_ns
        assert with_ns.startswith("I")

    def test_chapter_preserve_gen_without_namespace_fails_closed(self, tmp_path: pathlib.Path):
        """chapter_preserve gen mode with namespace=None (chapter present, bucket
        non-trivial) raises code_set_gen_requires_namespace at path 'namespace'.
        Kills the chapter_preserve gen-namespace guard mutants (code/path values,
        dropped kwargs -> TypeError)."""
        path = tmp_path / "two_per_chapter.parquet"
        _write_corpus(path, ["A01", "A02"], chapters=["A", "A"])
        cfg = CodeSetConfig.from_dict(
            {"code_set": "tpc", "chapter_preserve": True, "corpus_source": f"customer:{path}"}
        )
        with pytest.raises(PlanCompileError) as exc:
            apply_code_set("A01", cfg, mode="gen", job_seed=_JOB_SEED, namespace=None)
        assert exc.value.code == "code_set_gen_requires_namespace"
        assert exc.value.path == "namespace"


class TestPickMaskSingleRow:
    """_pick_mask single-row-corpus fail-closed guard."""

    def test_single_row_corpus_code_and_path(self, tmp_path: pathlib.Path):
        """A one-code corpus whose only code equals the input fails as
        code_set_single_row_corpus at path provider_config.code_set. Kills the
        guard's path-field mutants."""
        path = tmp_path / "single.parquet"
        _write_corpus(path, ["ONLY1"])
        cfg = CodeSetConfig.from_dict({"code_set": "s1", "corpus_source": f"customer:{path}"})
        with pytest.raises(PlanCompileError) as exc:
            apply_code_set("ONLY1", cfg, mode="mask", job_seed=_JOB_SEED)
        assert exc.value.code == "code_set_single_row_corpus"
        assert exc.value.path == "provider_config.code_set"


class TestCorpusResolution:
    """resolve_corpus_record: it must load the CUSTOMER corpus (not misfire to a
    shipped lookup) and run the version pin."""

    def test_customer_record_resolves_without_provenance(self, tmp_path: pathlib.Path):
        """A customer corpus (no provenance) resolves to a record with its rows.
        Kills the override_path->None mutant (which would misroute to a shipped
        lookup and raise not_found) and the is_shipped-inversion mutant (which
        would fail a no-provenance customer corpus closed)."""
        from decoy_engine.transforms.code_set import resolve_corpus_record

        path = tmp_path / "cust.parquet"
        _write_corpus(path, ["C01", "C02", "C03"])
        cfg = CodeSetConfig.from_dict({"code_set": "cust", "corpus_source": f"customer:{path}"})
        record = resolve_corpus_record(cfg)
        assert {r["code"] for r in record.rows} == {"C01", "C02", "C03"}

    def test_version_pin_mismatch_fails_closed(self, tmp_path: pathlib.Path):
        """resolve_corpus_record threads config.corpus_source_version into the
        pin check: a mismatch fails closed. Kills the expected_source_version
        ->None / dropped mutants (which would skip the check)."""
        from decoy_engine.transforms.code_set import resolve_corpus_record

        path = tmp_path / "pinned.parquet"
        _write_corpus(path, ["P01", "P02"], provenance=_COMPLETE_CUSTOMER_PROV)
        cfg = CodeSetConfig.from_dict(
            {
                "code_set": "pinned",
                "corpus_source": f"customer:{path}",
                "corpus_source_version": "9999",
            }
        )
        with pytest.raises(PlanCompileError) as exc:
            resolve_corpus_record(cfg)
        assert exc.value.code == "code_set_corpus_version_mismatch"


class TestPrivateRowIndexDefaults:
    """Private-helper row_index defaults (reached only by direct call: the public
    path always threads row_index explicitly)."""

    def test_pick_gen_row_index_defaults_to_zero(self):
        """_pick_gen's row_index defaults to 0. Kills its default mutant."""
        from decoy_engine.transforms._codeset_loader import _get_corpus_record
        from decoy_engine.transforms.code_set import _pick_gen

        rows = _get_corpus_record("icd10", None, is_shipped=True).rows
        seed = b"\x09" * 8
        default = _pick_gen(rows, job_seed=seed, namespace="ns")
        explicit0 = _pick_gen(rows, job_seed=seed, namespace="ns", row_index=0)
        explicit1 = _pick_gen(rows, job_seed=seed, namespace="ns", row_index=1)
        assert default == explicit0
        assert explicit0 != explicit1

    def test_apply_chapter_preserve_row_index_defaults_to_zero(self, tmp_path: pathlib.Path):
        """_apply_chapter_preserve's row_index defaults to 0. Kills its default
        mutant (a chapter with >=3 candidates makes row_index observable)."""
        from decoy_engine.transforms._codeset_loader import _get_corpus_record
        from decoy_engine.transforms.code_set import _apply_chapter_preserve

        path = tmp_path / "big_chapter.parquet"
        _write_corpus(path, ["A01", "A02", "A03", "A04"], chapters=["A"] * 4)
        record = _get_corpus_record("bc", path, is_shipped=False)
        seed = b"\x0a" * 8
        default = _apply_chapter_preserve("A01", record, mode="gen", job_seed=seed, namespace="ns")
        explicit0 = _apply_chapter_preserve(
            "A01", record, mode="gen", job_seed=seed, namespace="ns", row_index=0
        )
        explicit1 = _apply_chapter_preserve(
            "A01", record, mode="gen", job_seed=seed, namespace="ns", row_index=1
        )
        assert default == explicit0
        assert explicit0 != explicit1
