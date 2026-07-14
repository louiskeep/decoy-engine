"""DE-02 KeyProvider: fail-closed keyed-mask secret + full-surface rekey.

Path-completeness proof (the design's acceptance bar): for every keyed
re-identification strategy, masked output CHANGES when a real >=32-byte secret is
supplied and is BYTE-IDENTICAL to the seed-only baseline when no secret is
present. A site left on `job_seed` would show up here as "unchanged under a
secret". Plus: the fail-closed gate (pre-GA fallback / GA hard-error), the
`env:`/`file:` ref resolver (hex + base64, <32-byte rejection), and the
no-secret-in-serialized-plan guarantee.

Anonymisation carve-out (finding, tested as an invariant): `code_set` and
`joint_mask` are ANONYMISATION strategies (many-to-one, lossy). Their keyed
selection uses a fixed INTERNAL salt (`decoy.code_set.keyed_access.v1`,
`decoy.reference_tables.keyed_access.v1`), NOT `job_seed`, so their mask output
is independent of both the seed and the secret BY DESIGN -- the anonymisation
comes from lossiness, not key secrecy. The DE-02 site map listed them among the
23 job_seed sites, but their mask path never consumed `job_seed`; rekeying them
would break no-secret byte-identity (the golden `a_healthcare_claims` uses
code_set) for zero anonymisation benefit. This module pins that as an explicit
invariant rather than a silent gap.
"""

from __future__ import annotations

import base64
import json
import os

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import (
    KeyedStrategyRequiresSecret,
    SecretKeyProvider,
    SeedKeyProvider,
    WeakMaskSecret,
    run_pipeline,
)
from decoy_engine.config import PipelineConfig
from decoy_engine.determinism import DeterminismError, derive
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution.out_of_core._mask import mask_batch
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.keyprovider import (
    MaskSecretError,
    MissingMaskSecret,
    key_provider_from_ref,
    plan_has_keyed_strategy,
    resolve_key_provider,
    resolve_mask_secret_ref,
)
from decoy_engine.plan import compile_plan
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.profile import profile_source
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_EV = "de02-accept"
_JOB_SEED = (42).to_bytes(8, "big")
# A real >=32-byte secret (distinct value; 40 bytes to exercise the >32 path).
_SECRET = SecretKeyProvider(b"a-strong-32B+-managed-secret-value!!", key_version="v1")


def _validated(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


# --------------------------------------------------------------------------
# The end-to-end keyed-surface: one mask table exercising 8 pandas strategies
# through the real run_pipeline path (gate + threading + StrategyContext).
# --------------------------------------------------------------------------

_KEYED_COLUMNS = [
    {
        "name": "acct",
        "strategy": "fpe",
        "namespace": "acct_ns",
        "provider_config": {"charset": "digits"},
    },
    {"name": "email", "strategy": "hash", "namespace": "email_ns"},
    {
        "name": "dob",
        "strategy": "date_shift",
        "namespace": "dob_ns",
        "provider_config": {"date_format": "%Y-%m-%d", "min_days": -100, "max_days": 100},
    },
    {
        "name": "dept",
        "strategy": "categorical",
        "namespace": "dept_ns",
        "deterministic": True,
        "provider_config": {"categories": ["A", "B", "C", "D", "E", "F"]},
    },
    {
        "name": "hire_date",
        "strategy": "bucket_perturb",
        "namespace": "hire_ns",
        "provider_config": {"bucket": "month"},
    },
    {
        "name": "contact",
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "contact_ns",
        "provider_config": {"pool_size": 500},
    },
    {"name": "notes", "strategy": "text_mask", "namespace": "notes_ns"},
    {"name": "dept2", "strategy": "shuffle", "namespace": "dept2_ns", "deterministic": True},
]

_KEYED_COLUMN_NAMES = [c["name"] for c in _KEYED_COLUMNS]


def _frame(n: int = 60) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "acct": [f"{100000000 + i}" for i in range(n)],
            "email": [f"user{i}@example.com" for i in range(n)],
            "dob": [f"19{50 + (i % 40):02d}-0{1 + (i % 9)}-15" for i in range(n)],
            "dept": [["eng", "sales", "ops", "hr"][i % 4] for i in range(n)],
            "hire_date": [f"20{10 + (i % 12):02d}-{1 + (i % 12):02d}-15" for i in range(n)],
            "contact": [f"person{i}@src.example" for i in range(n)],
            "notes": [
                f"ssn 12{i % 9}-45-678{i % 9} phone (415) 555-01{i % 90:02d}" for i in range(n)
            ],
            "dept2": [["eng", "sales", "ops", "hr"][i % 4] for i in range(n)],
        }
    )


def _config(tmp_path, columns=None, seed: int = 42, mask_secret_ref=None) -> dict:
    gs: dict = {"seed": seed}
    if mask_secret_ref is not None:
        gs["mask_secret_ref"] = mask_secret_ref
    return _validated(
        {
            "version": 1,
            "global_settings": gs,
            "sources": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "t.csv")}},
            "tables": [{"name": "t", "columns": columns or _KEYED_COLUMNS}],
            "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
        }
    )


def _run(tmp_path, cfg, *, key_provider=None, **kwargs):
    df = _frame()
    df.to_csv(tmp_path / "t.csv", index=False)
    sources = {"t": pa.Table.from_pandas(df, preserve_index=False)}
    result = run_pipeline(
        cfg, sources=sources, engine_version=_EV, key_provider=key_provider, **kwargs
    )
    return result.outputs["t"].to_pandas()


class TestEndToEndRekey:
    """The core path-completeness proof through run_pipeline."""

    def test_no_secret_is_deterministic_and_masks(self, tmp_path):
        cfg = _config(tmp_path)
        base = _run(tmp_path, cfg)
        again = _run(tmp_path, cfg)
        assert base.equals(again), "no-secret output must be deterministic"
        src = _frame()
        for col in _KEYED_COLUMN_NAMES:
            assert base[col].tolist() != src[col].tolist(), f"{col} should be masked"

    @pytest.mark.parametrize("column", _KEYED_COLUMN_NAMES)
    def test_column_changes_under_secret(self, tmp_path, column):
        cfg = _config(tmp_path)
        base = _run(tmp_path, cfg)
        secret = _run(tmp_path, cfg, key_provider=_SECRET)
        assert base[column].tolist() != secret[column].tolist(), (
            f"{column} is a keyed site but did not change under a secret -- "
            "the rekey missed this call site (still on job_seed)."
        )

    def test_secret_run_is_deterministic(self, tmp_path):
        cfg = _config(tmp_path)
        one = _run(tmp_path, cfg, key_provider=_SECRET)
        two = _run(tmp_path, cfg, key_provider=_SECRET)
        assert one.equals(two), "output under a fixed secret must be deterministic"

    def test_distinct_key_versions_rekey_whole_surface(self, tmp_path):
        cfg = _config(tmp_path)
        v1 = _run(tmp_path, cfg, key_provider=SecretKeyProvider(b"z" * 33, key_version="v1"))
        v2 = _run(tmp_path, cfg, key_provider=SecretKeyProvider(b"z" * 33, key_version="v2"))
        for col in _KEYED_COLUMN_NAMES:
            assert v1[col].tolist() != v2[col].tolist(), f"{col} did not rotate with key_version"

    def test_secret_via_env_ref_matches_programmatic(self, tmp_path, monkeypatch):
        raw = b"a-strong-32B+-managed-secret-value!!"
        monkeypatch.setenv("DECOY_MASK_SECRET_ACCEPT", raw.hex())
        cfg_ref = _config(tmp_path, mask_secret_ref="env:DECOY_MASK_SECRET_ACCEPT")
        via_ref = _run(tmp_path, cfg_ref)
        cfg_plain = _config(tmp_path)
        via_prog = _run(tmp_path, cfg_plain, key_provider=SecretKeyProvider(raw))
        assert via_ref.equals(via_prog)


# --------------------------------------------------------------------------
# Handler-level proof for the transform-backed keyed strategies whose full
# pipeline config is awkward (cross-column refs). Two StrategyContexts with the
# SAME job_seed but different mask_key: if the handler still read ctx.job_seed
# the outputs would be identical, so a difference proves it reads ctx.mask_key.
# --------------------------------------------------------------------------

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_GRAPH = RelationshipGraph(edges=(), ordering=())


def _ctx(mask_key: bytes) -> StrategyContext:
    return StrategyContext(
        registry=_REG,
        pool_cache=PoolCache(),
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
        job_seed=_JOB_SEED,
        mask_key=mask_key,
    )


def _cseed(strategy, provider=None, pc=(), namespace="ns", deterministic=False) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=provider,
        backend_type="transform",
        backend_version="v1",
        cardinality_mode="preserve",
        deterministic=deterministic,
        provider_config=tuple(pc.items()) if isinstance(pc, dict) else pc,
    )


def _handler_output(handler, df_factory, column, seed, mask_key):
    return handler.run(df_factory(), column, seed, _ctx(mask_key))[0][column].tolist()


_COMPOSITE_COLUMNS = [
    {
        "name": "first_name",
        "strategy": "faker",
        "provider": "composite_name_email",
        "deterministic": True,
        "namespace": "nm_ns",
        "coherent_with": ["last_name", "email"],
    },
    {
        "name": "last_name",
        "strategy": "faker",
        "provider": "composite_name_email",
        "deterministic": True,
        "namespace": "nm_ns",
        "coherent_with": ["first_name", "email"],
    },
    {
        "name": "email",
        "strategy": "faker",
        "provider": "composite_name_email",
        "deterministic": True,
        "namespace": "nm_ns",
        "coherent_with": ["first_name", "last_name"],
    },
]


def _composite_frame(n: int = 24) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "first_name": [f"Fn{i}" for i in range(n)],
            "last_name": [f"Ln{i}" for i in range(n)],
            "email": [f"real{i}@corp.example" for i in range(n)],
        }
    )


class TestCompositeDeterministicRekey:
    """Deterministic composite (site 14): a real source value maps to a coherent
    synthetic bundle -- keyed, so it draws from mask_key (and needed the
    ProviderSpec 8-or-32 seed relax to accept the 32-byte mask root)."""

    def _run_composite(self, tmp_path, key_provider):
        df = _composite_frame()
        df.to_csv(tmp_path / "t.csv", index=False)
        cfg = _config(tmp_path, columns=_COMPOSITE_COLUMNS, seed=9)
        sources = {"t": pa.Table.from_pandas(df, preserve_index=False)}
        return (
            run_pipeline(cfg, sources=sources, engine_version=_EV, key_provider=key_provider)
            .outputs["t"]
            .to_pandas()
        )

    def test_changes_under_secret_and_is_deterministic(self, tmp_path):
        base = self._run_composite(tmp_path, None)
        again = self._run_composite(tmp_path, None)
        secret = self._run_composite(tmp_path, _SECRET)
        assert base["email"].tolist() == again["email"].tolist()
        assert base["email"].tolist() != secret["email"].tolist()


class TestTransformBackedHandlersRekey:
    def test_group_key_changes_under_secret(self):
        from decoy_engine.execution._strategies._group_key import GroupKeyStrategyHandler

        h = GroupKeyStrategyHandler()
        seed = _cseed("group_key", pc={"group_by": "grp", "prefix": "EP-"})
        df = lambda: pd.DataFrame({"grp": ["a", "b", "a", "c", "b"], "episode": [""] * 5})
        base = _handler_output(h, df, "episode", seed, _JOB_SEED)
        secret = _handler_output(h, df, "episode", seed, _SECRET.mask_key())
        assert base != secret
        assert _handler_output(h, df, "episode", seed, _JOB_SEED) == base  # deterministic

    def test_grouped_series_changes_under_secret(self):
        from decoy_engine.execution._strategies._grouped_series import (
            GroupedSeriesStrategyHandler,
        )

        h = GroupedSeriesStrategyHandler()
        seed = _cseed(
            "grouped_series",
            pc={"group_by": "grp", "order_by": "ord", "generator": "monotone_walk"},
        )
        df = lambda: pd.DataFrame(
            {"grp": ["a", "b", "a", "c", "b"], "ord": [1, 2, 3, 4, 5], "visit": [0] * 5}
        )
        base = _handler_output(h, df, "visit", seed, _JOB_SEED)
        secret = _handler_output(h, df, "visit", seed, _SECRET.mask_key())
        assert base != secret

    def test_windowed_date_changes_under_secret(self):
        from decoy_engine.execution._strategies._windowed_date import (
            WindowedDateStrategyHandler,
        )

        h = WindowedDateStrategyHandler()
        seed = _cseed("windowed_date", pc={"anchor": "anchor", "min_days": 1, "max_days": 30})
        df = lambda: pd.DataFrame({"anchor": ["2020-01-01"] * 5, "fu": [""] * 5})
        base = _handler_output(h, df, "fu", seed, _JOB_SEED)
        secret = _handler_output(h, df, "fu", seed, _SECRET.mask_key())
        assert base != secret


class TestAnonymisationStrategiesAreKeyIndependent:
    """FINDING pinned as invariant: code_set / joint_mask are anonymisation whose
    keyed selection uses a fixed internal salt, so mask output is independent of
    both job_seed and the secret. They still mask (output != input)."""

    def test_code_set_mask_is_secret_independent_but_masks(self):
        from decoy_engine.execution._strategies._code_set import CodeSetHandler

        h = CodeSetHandler()
        seed = _cseed("code_set", provider="code_set", pc={"code_set": "icd10"})
        df = lambda: pd.DataFrame({"dx": ["E11.9", "I10", "J45.909", "E11.65", "I10"]})
        base = _handler_output(h, df, "dx", seed, _JOB_SEED)
        secret = _handler_output(h, df, "dx", seed, _SECRET.mask_key())
        assert base == secret, "code_set mask uses a fixed salt (anonymisation), not the key"
        assert base != ["E11.9", "I10", "J45.909", "E11.65", "I10"], "still masks"

    def test_joint_mask_mask_is_secret_independent(self):
        from decoy_engine.execution._strategies._joint_mask import JointMaskHandler

        h = JointMaskHandler()
        seed = _cseed(
            "joint_mask",
            pc={
                "reference": "us_zip5_city_state",
                "columns": ["city", "state"],
                "key_by": "anchor",
            },
        )
        df = lambda: pd.DataFrame(
            {
                "city": ["Boston", "Miami", "Boston", "Reno", "Miami"],
                "state": ["MA", "FL", "MA", "NV", "FL"],
                "anchor": ["c1", "c2", "c3", "c4", "c5"],
            }
        )
        base = _handler_output(h, df, "city", seed, _JOB_SEED)
        secret = _handler_output(h, df, "city", seed, _SECRET.mask_key())
        assert base == secret, "joint_mask keyed_row uses a fixed reference-table salt"


# --------------------------------------------------------------------------
# Substrate coverage: polars-native + out-of-core streaming rekey.
# --------------------------------------------------------------------------

_POLARS_COLUMNS = [
    {"name": "email", "strategy": "hash", "namespace": "email_ns"},
    {
        "name": "dept",
        "strategy": "categorical",
        "namespace": "dept_ns",
        "deterministic": True,
        "provider_config": {"categories": ["A", "B", "C", "D"]},
    },
    {"name": "dept2", "strategy": "shuffle", "namespace": "dept2_ns", "deterministic": True},
]


class TestSubstrateRekey:
    @pytest.mark.parametrize("column", ["email", "dept", "dept2"])
    def test_polars_native_changes_under_secret(self, tmp_path, column):
        cfg = _config(tmp_path, columns=_POLARS_COLUMNS)
        base = _run(tmp_path, cfg, substrate="polars")
        secret = _run(tmp_path, cfg, key_provider=_SECRET, substrate="polars")
        assert base[column].tolist() != secret[column].tolist()

    def test_out_of_core_mask_batch_changes_under_secret(self, tmp_path):
        cols = [{"name": "email", "strategy": "hash", "namespace": "email_ns"}]
        cfg = _config(tmp_path, columns=cols)
        df = _frame(10)[["email"]]
        df.to_csv(tmp_path / "t.csv", index=False)
        prof = profile_source(cfg, seed=42)
        plan = compile_plan(cfg, prof, decoy_engine_version=_EV)
        batch = pa.RecordBatch.from_pandas(df, preserve_index=False)
        base = mask_batch(plan, "t", batch, mask_key=_JOB_SEED).column("email").to_pylist()
        secret = (
            mask_batch(plan, "t", batch, mask_key=_SECRET.mask_key()).column("email").to_pylist()
        )
        no_key = mask_batch(plan, "t", batch).column("email").to_pylist()
        assert base == no_key, "out-of-core mask_key defaults to job_seed (byte-identical)"
        assert base != secret, "out-of-core streaming mask did not rekey off the secret"

    def test_chunked_route_changes_under_secret(self, tmp_path):
        cols = [{"name": "email", "strategy": "hash", "namespace": "email_ns"}]
        cfg = _config(tmp_path, columns=cols)
        # chunk_size below row count forces the chunked route.
        base = _run(tmp_path, cfg, auto_chunk=True, chunk_size_rows=10, auto_chunk_threshold_rows=1)
        secret = _run(
            tmp_path,
            cfg,
            key_provider=_SECRET,
            auto_chunk=True,
            chunk_size_rows=10,
            auto_chunk_threshold_rows=1,
        )
        assert base["email"].tolist() != secret["email"].tolist()


# --------------------------------------------------------------------------
# Reversal surface: vault (site 22) + unmask FPE (site 23) rekey off the secret.
# --------------------------------------------------------------------------

_HAS_CRYPTO = True
try:
    import cryptography  # noqa: F401
except ImportError:  # pragma: no cover
    _HAS_CRYPTO = False

needs_crypto = pytest.mark.skipif(not _HAS_CRYPTO, reason="needs the vault extra (cryptography)")


class TestReversalSurfaceRekey:
    _COLS = [
        {
            "name": "acct",
            "strategy": "fpe",
            "namespace": "acct_ns",
            "provider_config": {"charset": "digits"},
        },
        {"name": "email", "strategy": "hash", "namespace": "email_ns", "vault": True},
    ]

    def _src(self, tmp_path, n=15):
        df = pd.DataFrame(
            {
                "acct": [f"{100000000 + i}" for i in range(n)],
                "email": [f"u{i}@e.com" for i in range(n)],
            }
        )
        df.to_csv(tmp_path / "t.csv", index=False)
        return df, {"t": pa.Table.from_pandas(df, preserve_index=False)}

    @needs_crypto
    def test_fpe_and_vault_round_trip_under_secret(self, tmp_path):
        from decoy_engine import unmask_pipeline
        from decoy_engine.vault import vault_writer_for_config

        cfg = _config(tmp_path, columns=self._COLS, seed=3)
        df, sources = self._src(tmp_path)
        writer = vault_writer_for_config(cfg, key_provider=_SECRET)
        result = run_pipeline(
            cfg, sources=sources, engine_version=_EV, vault_writer=writer, key_provider=_SECRET
        )
        writer.write(tmp_path / "vault.bin")
        masked = result.outputs["t"]
        assert masked.column("acct").to_pylist() != df["acct"].tolist()

        recovered = (
            unmask_pipeline(
                cfg, {"t": masked}, vault_path=str(tmp_path / "vault.bin"), key_provider=_SECRET
            )
            .outputs["t"]
            .to_pandas()
        )
        assert recovered["acct"].tolist() == df["acct"].tolist(), (
            "fpe must reverse under the secret"
        )
        assert recovered["email"].tolist() == df["email"].tolist(), (
            "vault must reverse under secret"
        )

    @needs_crypto
    def test_secret_vault_does_not_open_without_the_secret(self, tmp_path):
        from decoy_engine import unmask_pipeline
        from decoy_engine.execution import ExecutionError
        from decoy_engine.vault import vault_writer_for_config

        cfg = _config(tmp_path, columns=self._COLS, seed=3)
        _df, sources = self._src(tmp_path)
        writer = vault_writer_for_config(cfg, key_provider=_SECRET)
        result = run_pipeline(
            cfg, sources=sources, engine_version=_EV, vault_writer=writer, key_provider=_SECRET
        )
        writer.write(tmp_path / "vault.bin")
        with pytest.raises(ExecutionError) as exc:  # no secret -> wrong Fernet key
            unmask_pipeline(cfg, {"t": result.outputs["t"]}, vault_path=str(tmp_path / "vault.bin"))
        assert exc.value.code == "vault_key_mismatch"


# --------------------------------------------------------------------------
# Fail-closed gate.
# --------------------------------------------------------------------------


def _keyed_plan(tmp_path):
    cfg = _config(tmp_path, columns=[{"name": "email", "strategy": "hash", "namespace": "e_ns"}])
    df = _frame(5)[["email"]]
    df.to_csv(tmp_path / "t.csv", index=False)
    prof = profile_source(cfg, seed=42)
    return compile_plan(cfg, prof, decoy_engine_version=_EV)


def _unkeyed_plan(tmp_path):
    cfg = _config(tmp_path, columns=[{"name": "email", "strategy": "redact"}])
    df = _frame(5)[["email"]]
    df.to_csv(tmp_path / "t.csv", index=False)
    prof = profile_source(cfg, seed=42)
    return compile_plan(cfg, prof, decoy_engine_version=_EV)


class TestFailClosedGate:
    def test_plan_has_keyed_strategy_detects_keyed_and_unkeyed(self, tmp_path):
        assert plan_has_keyed_strategy(_keyed_plan(tmp_path)) is True
        assert plan_has_keyed_strategy(_unkeyed_plan(tmp_path)) is False

    def test_pre_ga_keyed_no_secret_falls_back_to_seed(self, tmp_path):
        provider = resolve_key_provider(plan=_keyed_plan(tmp_path), key_provider=None)
        assert provider is None  # -> job_seed fallback, byte-identical

    def test_ga_keyed_no_secret_hard_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr("decoy_engine.keyprovider.is_pre_ga", lambda: False)
        with pytest.raises(KeyedStrategyRequiresSecret):
            resolve_key_provider(plan=_keyed_plan(tmp_path), key_provider=None)

    def test_ga_unkeyed_no_secret_is_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("decoy_engine.keyprovider.is_pre_ga", lambda: False)
        assert resolve_key_provider(plan=_unkeyed_plan(tmp_path), key_provider=None) is None

    def test_ga_keyed_with_secret_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("decoy_engine.keyprovider.is_pre_ga", lambda: False)
        provider = resolve_key_provider(plan=_keyed_plan(tmp_path), key_provider=_SECRET)
        assert provider is _SECRET

    def test_weak_secret_rejected_at_construction(self):
        with pytest.raises(WeakMaskSecret):
            SecretKeyProvider(os.urandom(31))

    def test_end_to_end_ga_no_secret_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr("decoy_engine.keyprovider.is_pre_ga", lambda: False)
        cfg = _config(
            tmp_path, columns=[{"name": "email", "strategy": "hash", "namespace": "e_ns"}]
        )
        with pytest.raises(KeyedStrategyRequiresSecret):
            _run(tmp_path, cfg)


# --------------------------------------------------------------------------
# Secret-source ref resolver (env: / file:, hex + base64, <32 rejection).
# --------------------------------------------------------------------------


class TestSecretRefResolver:
    def test_env_hex(self, monkeypatch):
        raw = os.urandom(32)
        monkeypatch.setenv("DECOY_S_HEX", raw.hex())
        assert resolve_mask_secret_ref("env:DECOY_S_HEX") == raw

    def test_env_base64(self, monkeypatch):
        raw = os.urandom(40)
        monkeypatch.setenv("DECOY_S_B64", base64.b64encode(raw).decode())
        assert resolve_mask_secret_ref("env:DECOY_S_B64") == raw

    def test_file_hex(self, tmp_path):
        raw = os.urandom(32)
        p = tmp_path / "secret.hex"
        p.write_text(raw.hex())
        assert resolve_mask_secret_ref(f"file:{p}") == raw

    def test_missing_env_raises(self):
        with pytest.raises(MissingMaskSecret):
            resolve_mask_secret_ref("env:DECOY_DOES_NOT_EXIST_XYZ")

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(MissingMaskSecret):
            resolve_mask_secret_ref(f"file:{tmp_path / 'nope.bin'}")

    def test_unknown_ref_kind_raises(self):
        with pytest.raises(MaskSecretError):
            resolve_mask_secret_ref("kms://tenant/key")

    def test_short_secret_via_ref_rejected(self, monkeypatch):
        monkeypatch.setenv("DECOY_S_SHORT", os.urandom(16).hex())
        with pytest.raises(WeakMaskSecret):
            key_provider_from_ref("env:DECOY_S_SHORT")


# --------------------------------------------------------------------------
# The secret must never be serialized into the plan / manifest / metrics.
# --------------------------------------------------------------------------


class TestSecretNeverSerialized:
    def test_secret_bytes_absent_from_plan_and_result(self, tmp_path):
        raw = b"top-secret-managed-key-material-xyz!!"
        cfg = _config(tmp_path)
        df = _frame()
        df.to_csv(tmp_path / "t.csv", index=False)
        sources = {"t": pa.Table.from_pandas(df, preserve_index=False)}
        result = run_pipeline(
            cfg, sources=sources, engine_version=_EV, key_provider=SecretKeyProvider(raw)
        )
        # The config the plan compiles from carries no raw secret.
        assert raw.hex() not in json.dumps(cfg)
        assert "top-secret" not in json.dumps(cfg)
        # Neither do the emitted quality metrics / table kinds.
        blob = json.dumps(result.quality_metrics, default=str) + json.dumps(result.table_kinds)
        assert "top-secret" not in blob
        assert raw.hex() not in blob

    def test_mask_secret_ref_is_a_reference_not_the_secret(self, tmp_path, monkeypatch):
        raw = os.urandom(32)
        monkeypatch.setenv("DECOY_REF_ONLY", raw.hex())
        cfg = _config(tmp_path, mask_secret_ref="env:DECOY_REF_ONLY")
        # The serialized config carries only the ref string, never the bytes.
        assert "env:DECOY_REF_ONLY" in json.dumps(cfg)
        assert raw.hex() not in json.dumps(cfg)


# --------------------------------------------------------------------------
# derive() accepts 8 (job_seed) OR 32 (mask_key) bytes; 8-byte path unchanged.
# --------------------------------------------------------------------------


class TestDeriveGuard:
    def test_accepts_8_and_32_bytes(self):
        assert len(derive(b"x" * 8, "ns", b"src")) == 32
        assert len(derive(b"y" * 32, "ns", b"src")) == 32

    def test_8_byte_output_is_key_length_sensitive(self):
        # An 8-byte and a 32-byte IKM produce different keyed output (the rekey).
        assert derive(b"z" * 8, "ns", b"src") != derive(b"z" * 32, "ns", b"src")

    def test_rejects_other_lengths(self):
        for bad in (b"", b"x" * 7, b"x" * 16, b"x" * 31, b"x" * 33):
            with pytest.raises(DeterminismError):
                derive(bad, "ns", b"src")

    def test_seed_key_provider_is_job_seed(self):
        assert SeedKeyProvider(_JOB_SEED).mask_key() == _JOB_SEED
