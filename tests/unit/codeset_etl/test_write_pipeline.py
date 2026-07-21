"""write_normalized_corpus: validation, atomic write, and REAL-loader conformance.

Provenance-format conformance is asserted by loading the ETL's OWN output
back through decoy_engine's real slice-1 loader (customer-corpus path,
``corpus_source: customer:<path>``) -- not a re-implementation of the
loader's checks. If this module ever drifts from what the loader actually
validates, these tests fail against the real code, not a mirror of it.
"""

from __future__ import annotations

import pyarrow.parquet as pq
import pytest
from codeset_etl._errors import CodesetValidationError
from codeset_etl._write import write_normalized_corpus

from decoy_engine.transforms.code_set import CodeSetConfig, apply_code_set, describe_loaded_corpus

_PROVENANCE_KWARGS = dict(
    source="FDA National Drug Code (NDC) Directory",
    source_url="https://www.accessdata.fda.gov/cder/ndctext.zip",
    license="Public domain (United States Federal Government work; 17 U.S.C. 105)",
    citation="U.S. Food and Drug Administration. National Drug Code Directory.",
    source_version="pulled-2026-07-21",
    effective_date="2026-07-21",
)

_ROWS = [
    {"code": "10014000107", "description": "Oxygen 990mL/L GAS"},
    {"code": "00002015201", "description": "Zepbound 2.5mg/.5mL INJECTION, SOLUTION"},
    {"code": "72043250001", "description": "EltaMD UV Clear SPF46 LOTION"},
    {"code": "10018899901", "description": "Oxygen 990mL/L GAS"},
]


class TestWriteValidation:
    def test_empty_rows_rejected(self, tmp_path):
        with pytest.raises(CodesetValidationError, match="empty corpus"):
            write_normalized_corpus("ndc", [], out_dir=tmp_path, **_PROVENANCE_KWARGS)

    def test_duplicate_codes_rejected(self, tmp_path):
        rows = [{"code": "X1", "description": "a"}, {"code": "X1", "description": "b"}]
        with pytest.raises(CodesetValidationError, match="duplicate"):
            write_normalized_corpus("ndc", rows, out_dir=tmp_path, **_PROVENANCE_KWARGS)

    def test_empty_code_rejected(self, tmp_path):
        rows = [{"code": "", "description": "a"}]
        with pytest.raises(CodesetValidationError, match="empty"):
            write_normalized_corpus("ndc", rows, out_dir=tmp_path, **_PROVENANCE_KWARGS)

    def test_ragged_columns_rejected(self, tmp_path):
        rows = [{"code": "X1", "description": "a"}, {"code": "X2"}]
        with pytest.raises(CodesetValidationError, match="columns"):
            write_normalized_corpus("ndc", rows, out_dir=tmp_path, **_PROVENANCE_KWARGS)

    def test_blank_provenance_field_rejected(self, tmp_path):
        kwargs = dict(_PROVENANCE_KWARGS, license="")
        with pytest.raises(CodesetValidationError, match="license"):
            write_normalized_corpus("ndc", _ROWS, out_dir=tmp_path, **kwargs)

    def test_null_chapter_rejected_when_chapter_column_present(self, tmp_path):
        rows = [{"code": "X1", "chapter": "A"}, {"code": "X2", "chapter": ""}]
        with pytest.raises(CodesetValidationError, match="chapter"):
            write_normalized_corpus("ndc", rows, out_dir=tmp_path, **_PROVENANCE_KWARGS)

    def test_no_tmp_file_left_behind_after_success(self, tmp_path):
        write_normalized_corpus("ndc", _ROWS, out_dir=tmp_path, **_PROVENANCE_KWARGS)
        leftover = list(tmp_path.glob("*.tmp"))
        assert leftover == []


class TestWriteDeterminism:
    def test_output_is_sorted_ascending_by_code_on_disk(self, tmp_path):
        path = write_normalized_corpus("ndc", _ROWS, out_dir=tmp_path, **_PROVENANCE_KWARGS)
        tbl = pq.read_table(str(path))
        codes = tbl.column("code").to_pylist()
        assert codes == sorted(codes)

    def test_output_order_independent_of_input_order(self, tmp_path_factory):
        dir_a = tmp_path_factory.mktemp("a")
        dir_b = tmp_path_factory.mktemp("b")
        path_a = write_normalized_corpus("ndc", _ROWS, out_dir=dir_a, **_PROVENANCE_KWARGS)
        path_b = write_normalized_corpus(
            "ndc", list(reversed(_ROWS)), out_dir=dir_b, **_PROVENANCE_KWARGS
        )
        codes_a = pq.read_table(str(path_a)).column("code").to_pylist()
        codes_b = pq.read_table(str(path_b)).column("code").to_pylist()
        assert codes_a == codes_b


class TestRealLoaderConformance:
    """Load the ETL's own output through decoy_engine's real slice-1 loader."""

    def test_loads_via_customer_corpus_path_and_masks(self, tmp_path):
        path = write_normalized_corpus("ndc", _ROWS, out_dir=tmp_path, **_PROVENANCE_KWARGS)
        cfg = CodeSetConfig.from_dict({"code_set": "ndc", "corpus_source": f"customer:{path}"})

        out = apply_code_set("10014000107", cfg, mode="mask", job_seed=b"\x01" * 8)
        assert out in {row["code"] for row in _ROWS}
        assert out != "10014000107"

    def test_evidence_reports_is_seed_false_and_full_provenance(self, tmp_path):
        path = write_normalized_corpus("ndc", _ROWS, out_dir=tmp_path, **_PROVENANCE_KWARGS)
        cfg = CodeSetConfig.from_dict({"code_set": "ndc", "corpus_source": f"customer:{path}"})

        evidence = describe_loaded_corpus(cfg)
        assert evidence["is_seed"] is False
        assert evidence["source_version"] == "pulled-2026-07-21"
        assert evidence["effective_date"] == "2026-07-21"
        assert evidence["row_count"] == len(_ROWS)
        assert evidence["license"] == _PROVENANCE_KWARGS["license"]

    def test_shipped_seed_default_is_unaffected(self):
        """Sanity pin: the DEFAULT ("shipped") ndc corpus is still the tiny
        seed, proving the ETL's customer-path output never touches it."""
        cfg = CodeSetConfig.from_dict({"code_set": "ndc"})
        evidence = describe_loaded_corpus(cfg)
        assert evidence["is_seed"] is True
        assert evidence["row_count"] < 100  # seed is documented as 38 rows
