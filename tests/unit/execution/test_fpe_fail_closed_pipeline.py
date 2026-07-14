"""DE-01 cluster-C: FPE fails closed on un-encryptable values (real-input).

Every test here drives a real, pydantic-validated `PipelineConfig` through
`run_pipeline` (the DE-10 fixture bar), not hand-mocked internals. It pins the
three now-closed silent paths plus the two documented residual-risk warnings and
the healthy round-trip that must stay intact.

Closed (fail closed -> `StrategyError` at the execution boundary):
  - all-out-of-charset value (CJK name, or an out-of-charset orphan key): the
    pre-fix covering hash was non-invertible (verified `'---' -> '092' -> '858'`).
  - `preserve_separators=false` with any out-of-charset char: the pre-fix path
    returned the value UNCHANGED (a silent cleartext no-op).
  - too-short checksum value (`npi<10`): the pre-fix path `return`ed it unchanged.

Preserved + surfaced (documented residual risk, NOT closed this sprint):
  - a partial out-of-charset format prefix (`M` in `M000001`) still passes in the
    clear, but now rides a structured `fpe_partial_plaintext_disclosure`
    QualityWarning (fail-pre/pass-post: pre-fix the SAME leak had no signal).
  - a sub-minimum-domain column rides `fpe_sub_minimum_domain`.

Healthy: the dashed-SSN mask -> unmask round-trip stays byte-exact.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionError
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution.out_of_core._mask_group_b import fpe_array
from decoy_engine.unmask import unmask_pipeline

_ENGINE_VERSION = "de01-fail-closed-test"
_TABLE = "records"


def _config(columns: list[dict], tmp_path, *, seed: int = 42) -> dict:
    """A real, pydantic-validated PipelineConfig dump (not a toy dict)."""
    cfg = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {_TABLE: {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}},
        "tables": [{"name": _TABLE, "columns": columns}],
        "targets": {_TABLE: {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}},
    }
    return PipelineConfig.model_validate(cfg).model_dump()


def _fpe_col(name: str, charset: str, **provider_config: Any) -> dict:
    return {
        "name": name,
        "strategy": "fpe",
        "namespace": f"{name}_ns",
        "provider_config": {"charset": charset, **provider_config},
    }


def _run(cfg: dict, df: pd.DataFrame, tmp_path):
    # The planner profiles the source from its declared file path, so the CSV must
    # exist on disk; the masking run reads the passed Arrow table.
    df.to_csv(tmp_path / "in.csv", index=False)
    return run_pipeline(
        cfg,
        sources={_TABLE: pa.Table.from_pandas(df, preserve_index=False)},
        engine_version=_ENGINE_VERSION,
    )


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_all_out_of_charset_cjk_fails_closed(self, tmp_path) -> None:
        """A fully non-ASCII (CJK) value under an ASCII charset fails closed.

        This is exactly the reshaped job E kana_name hostile input.
        """
        cfg = _config([_fpe_col("name", "ALPHANUM")], tmp_path)
        df = pd.DataFrame({"name": ["Tanaka Taro", "田中 太郎", "Sato Jiro"]})
        with pytest.raises(ExecutionError) as exc:
            _run(cfg, df, tmp_path)
        assert exc.value.code == "fpe_unencryptable_value"
        assert "田中 太郎" in exc.value.message

    def test_all_out_of_charset_orphan_key_fails_closed(self, tmp_path) -> None:
        """An out-of-charset orphan-style key (all uppercase + hyphen) fails closed."""
        cfg = _config([_fpe_col("code", "alphanum")], tmp_path)  # lowercase charset
        df = pd.DataFrame({"code": ["emp00001", "EMP-ORPHAN", "emp00002"]})
        with pytest.raises(ExecutionError) as exc:
            _run(cfg, df, tmp_path)
        assert exc.value.code == "fpe_unencryptable_value"

    def test_preserve_separators_false_out_of_charset_fails_closed(self, tmp_path) -> None:
        """preserve_separators=false + any out-of-charset char fails closed.

        Pre-fix this returned the whole value unchanged (a silent cleartext no-op
        on the executed V2 path).
        """
        cfg = _config([_fpe_col("acct", "digits", preserve_separators=False)], tmp_path)
        df = pd.DataFrame({"acct": ["12345678", "12-345-678", "87654321"]})
        with pytest.raises(ExecutionError) as exc:
            _run(cfg, df, tmp_path)
        assert exc.value.code == "fpe_unencryptable_value"

    def test_short_checksum_value_fails_closed(self, tmp_path) -> None:
        """A too-short checksum value (npi < 10) fails closed instead of passthrough."""
        cfg = _config([_fpe_col("npi", "digits", checksum="npi")], tmp_path)
        df = pd.DataFrame({"npi": ["1234567893", "12345"]})  # second is too short
        with pytest.raises(ExecutionError) as exc:
            _run(cfg, df, tmp_path)
        assert exc.value.code == "fpe_checksum_unsupported"


# ---------------------------------------------------------------------------
# Preserved + surfaced (documented residual risk)
# ---------------------------------------------------------------------------


class TestResidualRiskWarnings:
    def test_partial_prefix_preserved_and_warned(self, tmp_path) -> None:
        """A partial out-of-charset prefix still leaks, but now rides a warning.

        Fail-pre/pass-post: pre-fix `M000001` masked to `M<digits>` (the `M`
        surviving in the clear) with NO signal; post-fix the SAME output carries a
        structured `fpe_partial_plaintext_disclosure` warning. The partial case is
        a documented DE-01 limitation, deliberately NOT closed this sprint.
        """
        cfg = _config([_fpe_col("member_id", "digits")], tmp_path)
        df = pd.DataFrame({"member_id": ["M000001", "M000002", "M000003"]})
        result = _run(cfg, df, tmp_path)
        out = result.outputs[_TABLE].column("member_id").to_pylist()
        assert all(v.startswith("M") for v in out), "partial prefix preserved (unchanged behavior)"
        assert all(v != s for v, s in zip(out, df["member_id"], strict=True)), "digits permuted"
        warn = next(
            (w for w in result.warnings if w.code == "fpe_partial_plaintext_disclosure"), None
        )
        assert warn is not None, "partial-prefix disclosure must be surfaced"
        assert warn.column == "member_id"
        assert warn.detail["affected_values"] == 3

    def test_sub_minimum_domain_warns(self, tmp_path) -> None:
        """A column whose in-charset domain is below the ~1M FF1 minimum warns."""
        cfg = _config([_fpe_col("tier", "ALPHANUM")], tmp_path)
        df = pd.DataFrame({"tier": ["HI", "MD", "LO", "HI"]})  # 2-char, radix 62 -> 3844 < 1M
        result = _run(cfg, df, tmp_path)
        warn = next((w for w in result.warnings if w.code == "fpe_sub_minimum_domain"), None)
        assert warn is not None
        assert warn.detail["min_length"] == 4  # 62 ** 4 >= 1_000_000 > 62 ** 3
        assert warn.detail["sub_minimum_values"] == 4


# ---------------------------------------------------------------------------
# Healthy path still round-trips exactly
# ---------------------------------------------------------------------------


class TestOutOfCoreFailClosed:
    """The out-of-core route (`fpe_array`) fails closed with the SAME taxonomy.

    DE-01 cluster-C wraps the value-level raises into `StrategyError` with the
    identical codes the full-frame handler uses, so a consumer keying on
    `StrategyError.code` sees the same fail-closed event on both routes. Real
    Arrow input + real key derivation (the OOC masking primitive), mirroring
    `TestFailClosed` above.
    """

    _SEED = (0xDECAFC0FFEE).to_bytes(8, "big")

    def test_ooc_all_out_of_charset_fails_closed(self) -> None:
        with pytest.raises(StrategyError) as exc:
            fpe_array(
                pa.array(["Tanaka", "田中 太郎", "Sato"]),
                job_seed=self._SEED,
                namespace="name_ns",
                column="name",
                cfg={"charset": "ALPHANUM"},
            )
        assert exc.value.code == "fpe_unencryptable_value"

    def test_ooc_short_checksum_fails_closed(self) -> None:
        with pytest.raises(StrategyError) as exc:
            fpe_array(
                pa.array(["1234567893", "12345"]),  # second is too short for npi
                job_seed=self._SEED,
                namespace="npi_ns",
                column="npi",
                cfg={"charset": "digits", "checksum": "npi"},
            )
        assert exc.value.code == "fpe_checksum_unsupported"

    def test_ooc_healthy_values_encrypt(self) -> None:
        """A healthy in-charset column masks cleanly on the OOC route (no false raise)."""
        out = fpe_array(
            pa.array(["123456", "654321", None]),
            job_seed=self._SEED,
            namespace="id_ns",
            column="id",
            cfg={"charset": "digits"},
        ).to_pylist()
        assert out[2] is None  # nulls preserved
        assert all(
            v is not None and v != s for v, s in zip(out[:2], ["123456", "654321"], strict=True)
        )


class TestHealthyRoundTrip:
    def test_dashed_ssn_masks_and_unmasks_byte_exact(self, tmp_path) -> None:
        """The healthy dashed-SSN path (declared separators) round-trips byte-exact."""
        cfg = _config([_fpe_col("ssn", "digits")], tmp_path)
        df = pd.DataFrame({"ssn": ["123-45-6789", "987-65-4321", "555-00-1234"]})
        result = _run(cfg, df, tmp_path)
        masked = result.outputs[_TABLE]
        masked_ssn = masked.column("ssn").to_pylist()
        # Dashes preserved in place; digits changed.
        assert all(v[3] == "-" and v[6] == "-" for v in masked_ssn)
        assert all(v != s for v, s in zip(masked_ssn, df["ssn"], strict=True))
        # Unmask recovers the source exactly.
        recovered = unmask_pipeline(cfg, {_TABLE: masked})
        rec_ssn = recovered.outputs[_TABLE].column("ssn").to_pylist()
        assert rec_ssn == df["ssn"].tolist()
