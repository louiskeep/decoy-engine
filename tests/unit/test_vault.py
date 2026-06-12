"""decoy_engine.vault: the token vault (deferred follow-up 1, 2026-06-12).

The vault records (namespace, masked) -> source for `vault: true`
columns at mask time, Fernet-encrypted under a key derived from the job
seed (`derive(job_seed, "vault", b"vault-key/v1")`), so one-way
strategies (hash, deterministic faker) become reversible at unmask
time. Every test except the absent-dep subprocess one needs the
`cryptography` extra and skips without it.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_pipeline_chunked, run_pipeline, unmask_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionError
from decoy_engine.plan import PlanCompileError, compile_plan, run_config_only_checks
from decoy_engine.vault import (
    VaultError,
    VaultWriter,
    collect_vault_entries,
    load_vault,
    vault_writer_for_config,
)

_ENGINE_VERSION = "vault-test"

_HAS_CRYPTO = True
try:  # the absent-dep contract is covered by the subprocess test below
    import cryptography  # noqa: F401
except ImportError:  # pragma: no cover
    _HAS_CRYPTO = False

needs_crypto = pytest.mark.skipif(not _HAS_CRYPTO, reason="needs the vault extra (cryptography)")


def _validated(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


def _config(tmp_path, columns: list[dict], seed: int = 42) -> dict:
    return _validated(
        {
            "version": 1,
            "global_settings": {"seed": seed},
            "sources": {
                "accounts": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "accounts.csv"),
                },
            },
            "tables": [{"name": "accounts", "columns": columns}],
            "targets": {
                "accounts": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "out.csv"),
                },
            },
        }
    )


_VAULT_COLUMNS = [
    {"name": "email", "strategy": "hash", "namespace": "email_ns", "vault": True},
    {
        "name": "contact",
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "contact_ns",
        "provider_config": {"pool_size": 500},
        "vault": True,
    },
    {"name": "memo", "strategy": "redact"},
]


def _frame(n: int = 40) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "email": [f"user{i}@example.com" for i in range(n)],
            "contact": [f"person{i}@source.example" for i in range(n)],
            "memo": [f"note {i}" for i in range(n)],
        }
    )


def _mask_with_vault(tmp_path, cfg: dict, df: pd.DataFrame):
    df.to_csv(tmp_path / "accounts.csv", index=False)
    sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}
    writer = vault_writer_for_config(cfg)
    result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, vault_writer=writer)
    vault_path = tmp_path / "vault.bin"
    writer.write(vault_path)
    return dict(result.outputs), vault_path


@needs_crypto
class TestRoundTrip:
    def test_hash_column_recovers_sources_exactly(self, tmp_path):
        """hash is collision-free per masked value (HMAC), so the round trip
        is exact: every source byte comes back."""
        df = _frame()
        cfg = _config(tmp_path, _VAULT_COLUMNS)
        outputs, vault_path = _mask_with_vault(tmp_path, cfg, df)
        assert outputs["accounts"].column("email").to_pylist() != df["email"].tolist()

        result = unmask_pipeline(cfg, outputs, vault_path=str(vault_path))
        out = result.outputs["accounts"]
        assert out.column("email").to_pylist() == df["email"].tolist()
        statuses = {(r.column, r.status) for r in result.columns}
        assert ("email", "vault_reversed") in statuses
        assert ("contact", "vault_reversed") in statuses
        assert ("memo", "irreversible") in statuses

    def test_pooled_faker_recovers_unambiguous_values_only(self, tmp_path):
        """Deterministic REUSE faker can map two sources to one masked value
        (derive_index collisions); those keys are dropped at write time and
        the affected rows keep the masked value. Everything unambiguous
        round-trips exactly -- the documented pooled-strategy contract."""
        df = _frame()
        cfg = _config(tmp_path, _VAULT_COLUMNS)
        outputs, vault_path = _mask_with_vault(tmp_path, cfg, df)
        masked_contacts = outputs["accounts"].column("contact").to_pylist()

        job_seed = vault_writer_for_config(cfg)._job_seed
        vault_map, _ = load_vault(vault_path, job_seed)

        result = unmask_pipeline(cfg, outputs, vault_path=str(vault_path))
        recovered = result.outputs["accounts"].column("contact").to_pylist()
        for i, masked in enumerate(masked_contacts):
            expected = vault_map.get(("contact_ns", masked))
            if expected is not None:
                assert recovered[i] == expected == df["contact"][i]
            else:
                assert recovered[i] == masked  # ambiguous: left masked
        # The fixed seed makes the collision set deterministic; most values
        # must still recover or the vault is not doing its job.
        exact = sum(1 for i, v in enumerate(recovered) if v == df["contact"][i])
        assert exact >= len(df) // 2

    def test_nulls_stay_null(self, tmp_path):
        df = _frame(10)
        df.loc[3, "email"] = None
        cfg = _config(tmp_path, _VAULT_COLUMNS)
        outputs, vault_path = _mask_with_vault(tmp_path, cfg, df)
        result = unmask_pipeline(cfg, outputs, vault_path=str(vault_path))
        assert result.outputs["accounts"].column("email").to_pylist()[3] is None

    def test_vaulted_column_without_vault_path_stays_irreversible(self, tmp_path):
        df = _frame(10)
        cfg = _config(tmp_path, _VAULT_COLUMNS)
        outputs, _ = _mask_with_vault(tmp_path, cfg, df)
        result = unmask_pipeline(cfg, outputs)
        report = next(r for r in result.columns if r.column == "email")
        assert report.status == "irreversible"
        assert "vault" in report.detail

    def test_wrong_vault_reports_miss(self, tmp_path):
        df = _frame(10)
        cfg = _config(tmp_path, _VAULT_COLUMNS)
        outputs, _ = _mask_with_vault(tmp_path, cfg, df)
        # A vault from different data under the same seed: zero hits.
        other_writer = vault_writer_for_config(cfg)
        other_writer.add([("email_ns", "not-a-real-masked-value", "x@example.com")])
        other_path = tmp_path / "other.bin"
        other_writer.write(other_path)
        result = unmask_pipeline(cfg, outputs, vault_path=str(other_path))
        report = next(r for r in result.columns if r.column == "email")
        assert report.status == "vault_miss"


@needs_crypto
class TestChunkedParity:
    def test_chunked_vault_equals_full_frame_vault(self, tmp_path):
        df = _frame(30)
        df.to_csv(tmp_path / "accounts.csv", index=False)
        cfg = _config(tmp_path, _VAULT_COLUMNS)

        _, full_path = _mask_with_vault(tmp_path, cfg, df)

        chunked_writer = vault_writer_for_config(cfg)
        chunks = [
            pa.Table.from_pandas(df.iloc[i : i + 7], preserve_index=False)
            for i in range(0, len(df), 7)
        ]
        list(
            run_mask_pipeline_chunked(
                cfg,
                chunks,
                table="accounts",
                engine_version=_ENGINE_VERSION,
                vault_writer=chunked_writer,
            )
        )
        chunked_path = tmp_path / "vault-chunked.bin"
        chunked_writer.write(chunked_path)

        job_seed = vault_writer_for_config(cfg)._job_seed
        full_map, full_ambiguous = load_vault(full_path, job_seed)
        chunked_map, chunked_ambiguous = load_vault(chunked_path, job_seed)
        assert chunked_map == full_map
        assert chunked_ambiguous == full_ambiguous
        # All 30 hash entries are collision-free; faker entries may drop
        # ambiguous pool collisions, identically on both paths.
        assert sum(1 for ns, _ in chunked_map if ns == "email_ns") == 30


@needs_crypto
class TestVaultFile:
    def test_wrong_seed_raises_key_mismatch(self, tmp_path):
        writer = VaultWriter(b"\x01" * 8)
        writer.add([("ns", "masked", "source")])
        path = tmp_path / "v.bin"
        writer.write(path)
        with pytest.raises(VaultError) as exc:
            load_vault(path, b"\x02" * 8)
        assert exc.value.code == "vault_key_mismatch"

    def test_not_a_vault_file_raises_unreadable(self, tmp_path):
        path = tmp_path / "v.bin"
        path.write_bytes(b"not a vault")
        with pytest.raises(VaultError) as exc:
            load_vault(path, b"\x01" * 8)
        assert exc.value.code == "vault_unreadable"

    def test_missing_file_raises_unreadable(self, tmp_path):
        with pytest.raises(VaultError) as exc:
            load_vault(tmp_path / "nope.bin", b"\x01" * 8)
        assert exc.value.code == "vault_unreadable"

    def test_ambiguous_keys_dropped_and_counted(self, tmp_path):
        writer = VaultWriter(b"\x01" * 8)
        writer.add(
            [
                ("ns", "MASKED-A", "source-1"),
                ("ns", "MASKED-A", "source-2"),  # conflict: dropped
                ("ns", "MASKED-B", "source-3"),
            ]
        )
        path = tmp_path / "v.bin"
        count = writer.write(path)
        assert count == 1
        mapping, ambiguous = load_vault(path, b"\x01" * 8)
        assert mapping == {("ns", "MASKED-B"): "source-3"}
        assert ambiguous == 1

    def test_unmask_maps_vault_errors_to_execution_error(self, tmp_path):
        df = _frame(5)
        cfg = _config(tmp_path, _VAULT_COLUMNS)
        outputs, vault_path = _mask_with_vault(tmp_path, cfg, df)
        wrong_cfg = _config(tmp_path, _VAULT_COLUMNS, seed=99)
        with pytest.raises(ExecutionError) as exc:
            unmask_pipeline(wrong_cfg, outputs, vault_path=str(vault_path))
        assert exc.value.code == "vault_key_mismatch"


class TestCompileChecks:
    def test_vault_requires_namespace(self, tmp_path):
        cfg = _config(tmp_path, [{"name": "email", "strategy": "hash", "vault": True}])
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "vault_requires_namespace"

    def test_vault_on_fpe_rejected(self, tmp_path):
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "acct",
                    "strategy": "fpe",
                    "namespace": "n",
                    "vault": True,
                    "provider_config": {"charset": "digits"},
                }
            ],
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "vault_strategy_reversible"

    def test_check_in_config_only_names(self, tmp_path):
        cfg = _config(tmp_path, [{"name": "email", "strategy": "hash", "namespace": "n"}])
        assert "vault_columns" in run_config_only_checks(cfg)

    def test_clean_vault_config_compiles(self, tmp_path):
        df = _frame(5)
        df.to_csv(tmp_path / "accounts.csv", index=False)
        cfg = _config(tmp_path, _VAULT_COLUMNS)
        from decoy_engine.execution._chunked import _first_chunk_profile

        profile = _first_chunk_profile(
            pa.Table.from_pandas(df, preserve_index=False),
            table="accounts",
            engine_version=_ENGINE_VERSION,
        )
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION, no_profile=True)
        assert "vault_columns" in plan.plan_compile.checks_passed

    def test_pydantic_accepts_vault_field(self):
        from decoy_engine.config._tables import ColumnConfig

        col = ColumnConfig(name="x", strategy="hash", namespace="n", vault=True)
        assert col.vault is True
        assert ColumnConfig(name="x", strategy="hash").vault is False


class TestCollectEntries:
    def test_positional_pairing_skips_nulls(self):
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "c", "strategy": "hash", "namespace": "ns", "vault": True}
                    ],
                }
            ]
        }
        sources = {"t": pa.table({"c": ["a", None, "b"]})}
        outputs = {"t": pa.table({"c": ["MA", None, "MB"]})}
        entries = collect_vault_entries(cfg, sources, outputs)
        assert entries == [("ns", "MA", "a"), ("ns", "MB", "b")]

    def test_non_vaulted_columns_ignored(self):
        cfg = {
            "tables": [
                {"name": "t", "columns": [{"name": "c", "strategy": "hash", "namespace": "ns"}]}
            ]
        }
        sources = {"t": pa.table({"c": ["a"]})}
        outputs = {"t": pa.table({"c": ["MA"]})}
        assert collect_vault_entries(cfg, sources, outputs) == []


# Subprocess that simulates the `cryptography` package being absent via a
# meta-path finder (the established optional-dep pattern, see
# tests/unit/providers_v2/mimesis/test_optional_dep.py). Imports must
# succeed (vault.py imports cryptography function-locally); only the
# write/load calls raise, naming the extra.
_ABSENT_SCRIPT = """
import json
import sys

class _BlockCrypto:
    def find_spec(self, name, path=None, target=None):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ModuleNotFoundError("No module named 'cryptography' (blocked for test)")
        return None

sys.meta_path.insert(0, _BlockCrypto())
for mod in list(sys.modules):
    if mod == "cryptography" or mod.startswith("cryptography."):
        del sys.modules[mod]

import decoy_engine
from decoy_engine.vault import VaultError, VaultWriter, load_vault

result = {"import_ok": True}
writer = VaultWriter(b"\\x01" * 8)
writer.add([("ns", "m", "s")])
try:
    writer.write("/tmp/decoy-vault-absent-test.bin")
    result["write_raised"] = False
except VaultError as exc:
    result["write_raised"] = True
    result["write_code"] = exc.code
    result["names_extra"] = "decoy-engine[vault]" in exc.message
try:
    load_vault("/tmp/decoy-vault-absent-test.bin", b"\\x01" * 8)
    result["load_raised"] = False
except VaultError as exc:
    result["load_raised"] = True
    result["load_code"] = exc.code
print(json.dumps(result))
"""


class TestCryptoAbsent:
    def test_absent_behavior_in_subprocess(self) -> None:
        proc = subprocess.run(  # noqa: S603 -- args are test literals, not untrusted input
            [sys.executable, "-c", _ABSENT_SCRIPT],
            capture_output=True,
            text=True,
            check=True,
        )
        result = json.loads(proc.stdout.strip())
        assert result["import_ok"] is True
        assert result["write_raised"] is True
        assert result["write_code"] == "vault_crypto_not_installed"
        assert result["names_extra"] is True
        assert result["load_raised"] is True
        assert result["load_code"] == "vault_crypto_not_installed"
