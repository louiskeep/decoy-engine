"""DE-03: fail-closed output projection.

A column with no strategy is dropped from the seed envelope
(`plan/_seed_envelope.py`), and a table absent from `tables:` (or with a
`columns:` block that covers only a subset of the source) gets no work node for
the undeclared columns -- so their raw values reach output unmasked, silently.
These tests pin the fail-closed gate at every emission route:

- `policy="error"` raises `ExecutionError(code="undeclared_output_columns")` on
  (c) a known-but-unstrategied column, (d) true runtime schema drift (a column
  in the masked frame absent from the profiled source), and the whole-table case
  (a table with no declared surface).
- `policy="warn"` (the pre-GA default) emits a structured `QualityWarning`
  through `ExecutionResult.warnings` AND still passes the column through -- the
  raw value is emitted, which is exactly the silent leak pre-fix `main` produced
  with NO warning and no error (see `test_warn_passthrough_is_the_documented_leak`).
- The release-phase coupling: the default resolves to `warn` while `is_pre_ga()`.
- The sibling compile check rejects `strategy: faker` with no provider.

Real-input `PipelineConfig.model_validate(...)` + `run_pipeline` / `compile_plan`
for the full-frame + policy-resolution + compile paths; hand-built seed plans fed
straight to `run_fk_out_of_core` / `run_sequential` for the out-of-core and
sequential routes, mirroring the DE-10 route-parametrized fixture bar
(`test_de10_fk_lossless_typing.py`, `tests/perf_fixtures/fk_relational.py`).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import release, run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionError, PandasExecutionAdapter
from decoy_engine.execution._output_projection import (
    known_output_columns,
    resolve_unconfigured_column_policy,
)
from decoy_engine.execution._sequential import run_sequential
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.plan._types import ColumnSeed, GroupSeed, SeedEnvelope, TableSeed
from decoy_engine.profile import profile_source
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_ENGINE_VERSION = "de03-test"
_UNDECLARED_CODE = "undeclared_output_columns"


# ---------------------------------------------------------------------------
# Real-config helpers (full-frame route + policy resolution)
# ---------------------------------------------------------------------------


def _config(
    tmp_path,
    columns: list[dict],
    *,
    policy: str | None = None,
    table: str = "people",
) -> dict:
    """A real, pydantic-validated PipelineConfig dump (not a toy dict)."""
    global_settings: dict[str, Any] = {"seed": 42}
    if policy is not None:
        global_settings["unconfigured_column_policy"] = policy
    cfg = {
        "version": 1,
        "global_settings": global_settings,
        "sources": {table: {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}},
        "tables": [{"name": table, "columns": columns}],
        "targets": {table: {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}},
    }
    return PipelineConfig.model_validate(cfg).model_dump()


def _write(tmp_path, df: pd.DataFrame) -> None:
    df.to_csv(tmp_path / "in.csv", index=False)


def _run(cfg: dict, df: pd.DataFrame, table: str = "people"):
    return run_pipeline(
        cfg,
        sources={table: pa.Table.from_pandas(df, preserve_index=False)},
        engine_version=_ENGINE_VERSION,
    )


# A source whose `secret` column is never declared in `columns:` -- known at
# profile time (it is in the CSV), unstrategied in the config.
def _known_unstrategied(tmp_path, policy: str | None):
    df = pd.DataFrame({"id": ["a", "b", "c"], "secret": ["s1", "s2", "s3"]})
    _write(tmp_path, df)
    cfg = _config(tmp_path, [{"name": "id", "strategy": "passthrough"}], policy=policy)
    return cfg, df


# ---------------------------------------------------------------------------
# policy="error": (c) known-but-unstrategied, (d) drift  -- full-frame route
# ---------------------------------------------------------------------------


class TestErrorPolicyFullFrame:
    def test_known_but_unstrategied_column_raises(self, tmp_path) -> None:
        cfg, df = _known_unstrategied(tmp_path, policy="error")
        with pytest.raises(ExecutionError) as exc:
            _run(cfg, df)
        assert exc.value.code == _UNDECLARED_CODE
        assert "secret" in exc.value.message
        assert "people" in exc.value.message

    def test_runtime_schema_drift_raises(self, tmp_path) -> None:
        # (d): the profiled CSV has only [id]; the masked Arrow frame carries an
        # extra `drift_col` the plan was never built against.
        _write(tmp_path, pd.DataFrame({"id": ["a", "b"]}))
        cfg = _config(tmp_path, [{"name": "id", "strategy": "passthrough"}], policy="error")
        drifted = pd.DataFrame({"id": ["a", "b"], "drift_col": ["x", "y"]})
        with pytest.raises(ExecutionError) as exc:
            _run(cfg, drifted)
        assert exc.value.code == _UNDECLARED_CODE
        assert "drift_col" in exc.value.message


# ---------------------------------------------------------------------------
# policy="warn": structured warning + passthrough (no raise)  -- full-frame
# ---------------------------------------------------------------------------


class TestWarnPolicyFullFrame:
    def test_warn_emits_structured_warning_and_passes_through(self, tmp_path) -> None:
        cfg, df = _known_unstrategied(tmp_path, policy="warn")
        result = _run(cfg, df)
        # Column still emitted (the passthrough the warn window preserves)...
        out = result.outputs["people"]
        assert "secret" in out.column_names
        assert out.column("secret").to_pylist() == ["s1", "s2", "s3"]
        # ...and a structured warning names it via the QualityWarning channel.
        codes = [w.code for w in result.warnings]
        assert _UNDECLARED_CODE in codes
        w = next(w for w in result.warnings if w.code == _UNDECLARED_CODE)
        assert w.detail["table"] == "people"
        assert "secret" in w.detail["undeclared_columns"]

    def test_warn_is_the_pre_ga_default(self, tmp_path) -> None:
        # No explicit policy set -> pre-GA default is warn -> passthrough + warn.
        cfg, df = _known_unstrategied(tmp_path, policy=None)
        result = _run(cfg, df)
        assert "secret" in result.outputs["people"].column_names
        assert _UNDECLARED_CODE in [w.code for w in result.warnings]

    def test_warn_passthrough_is_the_documented_leak(self, tmp_path) -> None:
        # Fail-pre / pass-post evidence, in one place: under warn the RAW
        # undeclared value reaches output (this IS the DE-03 leak -- pre-fix
        # `main` produced exactly this output with NO warning and no error), and
        # post-fix the SAME run now also carries a structured warning. Flipping
        # the policy to error turns the identical shape into a hard failure.
        cfg_warn, df = _known_unstrategied(tmp_path, policy="warn")
        warned = _run(cfg_warn, df)
        assert warned.outputs["people"].column("secret").to_pylist() == ["s1", "s2", "s3"]
        assert _UNDECLARED_CODE in [w.code for w in warned.warnings]  # NEW post-fix

        cfg_err, df2 = _known_unstrategied(tmp_path, policy="error")
        with pytest.raises(ExecutionError):
            _run(cfg_err, df2)


# ---------------------------------------------------------------------------
# Policy resolution + release-phase coupling
# ---------------------------------------------------------------------------


class TestPolicyResolution:
    def test_pre_ga_default_is_warn(self) -> None:
        assert release.is_pre_ga() is True
        assert resolve_unconfigured_column_policy(None) == "warn"
        assert resolve_unconfigured_column_policy({"global_settings": {"seed": 1}}) == "warn"

    def test_explicit_setting_overrides_phase(self) -> None:
        assert (
            resolve_unconfigured_column_policy(
                {"global_settings": {"unconfigured_column_policy": "error"}}
            )
            == "error"
        )
        assert (
            resolve_unconfigured_column_policy(
                {"global_settings": {"unconfigured_column_policy": "warn"}}
            )
            == "warn"
        )

    def test_ga_default_is_error(self, monkeypatch) -> None:
        # At GA the fail-closed policy binds automatically with no per-config flip.
        monkeypatch.setattr(release, "RELEASE_PHASE", "ga")
        assert release.is_pre_ga() is False
        assert resolve_unconfigured_column_policy(None) == "error"
        # An explicit warn still overrides even at GA.
        assert (
            resolve_unconfigured_column_policy(
                {"global_settings": {"unconfigured_column_policy": "warn"}}
            )
            == "warn"
        )


class TestKnownOutputColumns:
    def test_union_of_per_column_and_group_coherent(self) -> None:
        seed = _hash_seed("ns")
        gseed = GroupSeed(namespace="g", coherent_columns=("a", "b"))
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=b"\x00" * 8,
                per_table=(
                    ("t", TableSeed(per_column=(("id", seed),), per_group=(("a__b", gseed),))),
                ),
            )
        )
        assert known_output_columns(plan, "t") == frozenset({"id", "a", "b"})

    def test_absent_table_is_empty_set(self) -> None:
        plan = SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=b"\x00" * 8, per_table=()))
        assert known_output_columns(plan, "missing") == frozenset()


# ---------------------------------------------------------------------------
# Hand-built seed plans for the out-of-core + sequential routes
# ---------------------------------------------------------------------------

_REG = get_default_registry()
_NS_REG = NamespaceRegistry(bindings=())
_JOB_SEED = b"\x33" * 8


def _hash_seed(namespace: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _fk_sources() -> dict[str, pa.Table]:
    # parent[pk] <- child[fk]; child ALSO carries an undeclared `payload_leak`.
    parent = pa.table({"pk": ["p0", "p1", "p2"]})
    child = pa.table({"fk": ["p0", "p1", "p2"], "payload_leak": ["raw0", "raw1", "raw2"]})
    return {"parent": parent, "child": child}


def _fk_plan() -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", _hash_seed("ns_p")),), per_group=())),
                # `payload_leak` is deliberately NOT declared -> undeclared output.
                ("child", TableSeed(per_column=(("fk", _hash_seed("ns_p")),), per_group=())),
            ),
        )
    )


def _fk_graph() -> RelationshipGraph:
    return RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="ns_p",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )


class TestOutOfCoreRoute:
    def test_error_policy_raises_on_undeclared_child_column(self) -> None:
        with pytest.raises(ExecutionError) as exc:
            run_fk_out_of_core(
                _fk_plan(),
                _fk_sources(),
                registry=_REG,
                relationship_graph=_fk_graph(),
                unconfigured_column_policy="error",
            )
        assert exc.value.code == _UNDECLARED_CODE
        assert "payload_leak" in exc.value.message

    def test_warn_policy_passes_through_with_warning(self) -> None:
        result = run_fk_out_of_core(
            _fk_plan(),
            _fk_sources(),
            registry=_REG,
            relationship_graph=_fk_graph(),
            unconfigured_column_policy="warn",
        )
        assert "payload_leak" in result.outputs["child"].column_names
        assert _UNDECLARED_CODE in [w.code for w in result.warnings]


class TestSequentialRoute:
    def _loader(self):
        src = _fk_sources()

        def load(table: str) -> pa.Table:
            return src[table]

        return load

    def test_error_policy_raises_on_undeclared_child_column(self) -> None:
        with pytest.raises(ExecutionError) as exc:
            run_sequential(
                PandasExecutionAdapter(),
                _fk_plan(),
                self._loader(),
                registry=_REG,
                relationship_graph=_fk_graph(),
                namespace_registry=_NS_REG,
                unconfigured_column_policy="error",
            )
        assert exc.value.code == _UNDECLARED_CODE
        assert "payload_leak" in exc.value.message

    def test_warn_policy_passes_through_with_warning(self) -> None:
        result = run_sequential(
            PandasExecutionAdapter(),
            _fk_plan(),
            self._loader(),
            registry=_REG,
            relationship_graph=_fk_graph(),
            namespace_registry=_NS_REG,
            unconfigured_column_policy="warn",
        )
        assert "payload_leak" in result.outputs["child"].column_names
        assert _UNDECLARED_CODE in [w.code for w in result.warnings]


class TestWholeTableFailsClosed:
    """A table with no declared output surface (empty TableSeed) fails closed:
    every one of its columns is undeclared, so the whole table is rejected."""

    def test_empty_table_seed_rejects_every_column(self) -> None:
        source = pa.table({"a": ["x"], "b": ["y"]})
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=_JOB_SEED,
                per_table=(("t", TableSeed(per_column=(), per_group=())),),
            )
        )
        with pytest.raises(ExecutionError) as exc:
            PandasExecutionAdapter().run(
                plan,
                {"t": source},
                registry=_REG,
                relationship_graph=RelationshipGraph(edges=(), ordering=()),
                namespace_registry=_NS_REG,
                unconfigured_column_policy="error",
            )
        assert exc.value.code == _UNDECLARED_CODE
        assert "a" in exc.value.message and "b" in exc.value.message


# ---------------------------------------------------------------------------
# Generate-echo tables are exempt (not falsely flagged as undeclared)
# ---------------------------------------------------------------------------


class TestGenerateEchoExempt:
    def test_generate_table_columns_are_not_flagged(self, tmp_path) -> None:
        # A pure-generate table's columns are declared by its generate config,
        # not the mask plan; even under error they must pass the gate cleanly.
        cfg = {
            "version": 1,
            "global_settings": {"seed": 7, "unconfigured_column_policy": "error"},
            "sources": {},
            "tables": [
                {
                    "name": "synth",
                    "row_count": 5,
                    "generate_columns": [
                        {"name": "amount", "type": "sequence", "start": 1, "step": 1}
                    ],
                }
            ],
            "targets": {
                "synth": {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}
            },
        }
        cfg = PipelineConfig.model_validate(cfg).model_dump()
        result = run_pipeline(cfg, sources={}, engine_version=_ENGINE_VERSION)
        assert "amount" in result.outputs["synth"].column_names
        assert _UNDECLARED_CODE not in [w.code for w in result.warnings]


# ---------------------------------------------------------------------------
# Sibling compile check: faker without provider
# ---------------------------------------------------------------------------


class TestFakerRequiresProvider:
    def test_faker_without_provider_raises_at_compile(self, tmp_path) -> None:
        _write(tmp_path, pd.DataFrame({"email": ["a@x.com", "b@x.com"]}))
        cfg = _config(tmp_path, [{"name": "email", "strategy": "faker"}])
        profile = profile_source(cfg, seed=0)
        with pytest.raises(PlanCompileError) as exc:
            compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
        assert exc.value.code == "faker_requires_provider"

    def test_faker_with_provider_compiles(self, tmp_path) -> None:
        _write(tmp_path, pd.DataFrame({"email": ["a@x.com", "b@x.com"]}))
        cfg = _config(
            tmp_path,
            [{"name": "email", "strategy": "faker", "provider": "person_email"}],
        )
        profile = profile_source(cfg, seed=0)
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
        assert "faker_requires_provider" in plan.plan_compile.checks_passed
