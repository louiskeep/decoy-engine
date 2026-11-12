"""Vault key floor at every run entry that accepts a vault writer.

A non-keyed run's own key is the 8-byte job_seed, so the key-match compare alone would accept a
writer keyed on it; the entry guard revalidates the writer's key against the vault floor before
any route work. Labels: FAIL-BEFORE (red before the floor) or CHARACTERIZATION (green before and
after).
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine import run_pipeline
from decoy_engine.keyprovider import SecretKeyProvider
from tests._declared_schema import declared_schema

_EV = "vault-run-entries"
_JOB_SEED = (42).to_bytes(8, "big")
_SECRET = SecretKeyProvider(b"a-strong-32B+-managed-secret-value!!", key_version="v1")

try:
    import cryptography  # noqa: F401

    _HAS_CRYPTO = True
except ImportError:  # pragma: no cover
    _HAS_CRYPTO = False

needs_crypto = pytest.mark.skipif(not _HAS_CRYPTO, reason="needs the vault extra (cryptography)")


_UNKEYED_FK_COLUMNS = {
    "P": [{"name": "t", "strategy": "redact"}, {"name": "k", "strategy": "passthrough"}],
    "C": [{"name": "k", "strategy": "passthrough"}, {"name": "m", "strategy": "redact"}],
}


def _unkeyed_fk_job(tmp_path) -> tuple[dict, dict[str, pa.Table]]:
    import pyarrow.parquet as pq

    tables = {
        "P": pa.table({"t": ["a", "b", "c", "d", "e"], "k": ["k1", "k2", "k3", "k4", "k5"]}),
        "C": pa.table({"k": ["k1", "k2", "k3"], "m": ["x", "y", "z"]}),
    }
    for name, table in tables.items():
        pq.write_table(table, tmp_path / f"{name}.parquet")
    file = {"type": "file", "format": "parquet"}
    cfg = {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": {t: {**file, "path": str(tmp_path / f"{t}.parquet")} for t in tables},
        "targets": {t: {**file, "path": str(tmp_path / f"{t}.out.parquet")} for t in tables},
        "tables": [{"name": t, "columns": cols} for t, cols in _UNKEYED_FK_COLUMNS.items()],
        "relationships": [
            {
                "parent": {"table": "P", "columns": ["k"]},
                "children": [{"table": "C", "columns": ["k"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_k",
            }
        ],
    }
    return cfg, tables


def _run_route(route: str, cfg: dict, tables: dict, writer, key_provider=None):
    from decoy_engine import run_mask_pipeline_chunked

    if route == "run_mask_pipeline_chunked":
        single = {
            **cfg,
            "tables": [cfg["tables"][0]],
            "relationships": [],
            "sources": {"P": cfg["sources"]["P"]},
            "targets": {"P": cfg["targets"]["P"]},
        }
        return list(
            run_mask_pipeline_chunked(
                single,
                [tables["P"]],
                table="P",
                source_schema=declared_schema(single, "P"),
                engine_version=_EV,
                vault_writer=writer,
                key_provider=key_provider,
            )
        )
    routing = {
        "full_frame": {"execution_mode": "full_frame"},
        "sequential": {"execution_mode": "sequential"},
        # Byte-estimate routing is what admits a vault job to the bounded route.
        "out_of_core": {"out_of_core_budget_bytes": 1 << 30},
    }[route]
    result = run_pipeline(
        cfg,
        tables,
        engine_version=_EV,
        vault_writer=writer,
        key_provider=key_provider,
        **routing,
    )
    assert result.quality_metrics["execution"]["execution_mode"] == route
    return result


def _route_work_spy(monkeypatch) -> list[str]:
    """Records entry into route work: the first step each entry takes after its vault-key
    guard (source preparation on `run_pipeline`, corpus pinning on the chunked entry), which
    precedes every strategy handler."""
    from decoy_engine.execution import _chunked_code_set, _pipeline

    calls: list[str] = []
    for module, name in (
        (_pipeline, "prepare_transform_sources"),
        (_chunked_code_set, "resolve_pinned_code_set_records"),
    ):
        real = getattr(module, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, name, spy)
    return calls


@needs_crypto
@pytest.mark.no_default_secret
class TestEveryRunEntryRefusesWeakVaultWriter:
    @pytest.mark.parametrize(
        "route",
        [
            pytest.param("full_frame", id="full_frame-FAIL_BEFORE"),
            pytest.param("sequential", id="sequential-FAIL_BEFORE"),
            pytest.param("out_of_core", id="out_of_core-FAIL_BEFORE"),
            pytest.param("run_mask_pipeline_chunked", id="run_mask_pipeline_chunked-FAIL_BEFORE"),
        ],
    )
    def test_every_run_entry_refuses_weak_vault_writer_before_masking(
        self, tmp_path, monkeypatch, route
    ):
        from decoy_engine.plan import job_seed_for_config
        from decoy_engine.vault import VaultError, VaultWriter

        cfg, tables = _unkeyed_fk_job(tmp_path)
        writer = VaultWriter(b"\x07" * 32)
        writer._mask_key = job_seed_for_config(cfg)  # the run's own key on a non-keyed job
        calls = _route_work_spy(monkeypatch)
        with pytest.raises(VaultError) as exc:
            _run_route(route, cfg, tables, writer)
        assert exc.value.code == "weak_mask_secret"
        assert calls == []

    def test_direct_out_of_core_guard_refuses_weak_writer_FAIL_BEFORE(self):
        from decoy_engine.execution.out_of_core._vault_collect import assert_vault_inputs
        from decoy_engine.vault import VaultError, VaultWriter

        writer = VaultWriter(b"\x07" * 32)
        writer._mask_key = _JOB_SEED
        with pytest.raises(VaultError) as exc:
            assert_vault_inputs(writer, {"tables": []}, _JOB_SEED)
        assert exc.value.code == "weak_mask_secret"

    @pytest.mark.parametrize(
        "route",
        [
            pytest.param("full_frame", id="full_frame-CHARACTERIZATION"),
            pytest.param("out_of_core", id="out_of_core-CHARACTERIZATION"),
            pytest.param(
                "run_mask_pipeline_chunked", id="run_mask_pipeline_chunked-CHARACTERIZATION"
            ),
        ],
    )
    def test_strong_key_each_route_still_completes(self, tmp_path, monkeypatch, route):
        from decoy_engine.vault import VaultWriter, load_vault

        cfg, tables = _unkeyed_fk_job(tmp_path)
        writer = VaultWriter(_SECRET.mask_key())
        calls = _route_work_spy(monkeypatch)
        _run_route(route, cfg, tables, writer, key_provider=_SECRET)
        assert calls
        path = tmp_path / "v.vault"
        writer.write(path)
        assert load_vault(path, _SECRET.mask_key()) == ({}, 0)
