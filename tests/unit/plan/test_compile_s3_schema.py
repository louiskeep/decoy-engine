"""S1 -> S3 plan-schema delta tests (B2 + H1 resolution).

S3's plan-schema delta deletes the four int seed fields
(`column_seed`, `group_seed`, `table_seed`, `NamespaceBinding.seed`) and
re-types `SeedEnvelope.job_seed` from `int` to `bytes`. These tests pin:

- The dataclass shapes after the delta
- The seed_overflow PlanCompileError at the pipeline-config boundary
- The SEED_PROTOCOL_VERSION=4 stamp on every emitted plan (was 1
  at S3 ship; F-series bumped to 2; QA walks/gen F3 PO Q-F3=b 2026-06-01
  bumped to 3 for the vectorised null-injection RNG-family swap;
  formula-hash migration to keyed HMAC-SHA256 2026-06-01 bumped to 4)
- End-to-end derive_value wiring with the plan's bytes-typed job_seed
"""

from __future__ import annotations

from datetime import datetime

import pytest

from decoy_engine.determinism import IdentityDomain, derive, derive_value
from decoy_engine.plan import (
    ColumnSeed,
    GroupSeed,
    NamespaceBinding,
    PlanCompileError,
    SeedEnvelope,
    TableSeed,
    compile_plan,
)
from decoy_engine.profile import (
    ColumnProfile,
    Profile,
    Relationship,
    TableProfile,
)


def _minimal_profile() -> Profile:
    return Profile(
        schema_version=1,
        tables=(
            TableProfile(
                name="customers",
                row_count=10,
                columns=(
                    ColumnProfile(
                        name="customer_id",
                        dtype="object",
                        row_count=10,
                        null_count=0,
                        distinct_count=10,
                        sampled=False,
                        is_candidate_key_sampled=True,
                        declared_pk=True,
                        is_fk=False,
                        fk_target=None,
                        pii_class=None,
                    ),
                ),
            ),
            TableProfile(
                name="orders",
                row_count=20,
                columns=(
                    ColumnProfile(
                        name="customer_id",
                        dtype="object",
                        row_count=20,
                        null_count=0,
                        distinct_count=10,
                        sampled=False,
                        is_candidate_key_sampled=False,
                        declared_pk=False,
                        is_fk=True,
                        fk_target=("customers", "customer_id"),
                        pii_class=None,
                    ),
                ),
            ),
        ),
        relationships=(
            Relationship(
                parent_table="customers",
                parent_columns=("customer_id",),
                child_table="orders",
                child_columns=("customer_id",),
                namespace="customer_identity",
            ),
        ),
        profiled_at=datetime(2026, 5, 27, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _minimal_config(seed: int = 42) -> dict:
    return {
        "global_settings": {"seed": seed},
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["customer_id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "fail",
                "namespace": "customer_identity",
            }
        ],
    }


class TestSeedProtocolVersionStamp:
    """S3 stamped v1; F-series corrections bumped to v2; QA walks/gen
    F3 PO Q-F3=b (2026-06-01) bumped to v3 for the vectorised null-
    injection RNG-family swap; formula-hash migration to keyed
    HMAC-SHA256 (2026-06-01) bumps to v4. Every shipped plan now
    stamps `seed_protocol_version == 4`."""

    def test_plan_stamps_v4(self) -> None:
        plan = compile_plan(_minimal_config(), _minimal_profile(), decoy_engine_version="0.1.0")
        assert plan.seed_protocol_version == 4


class TestJobSeedBytesShape:
    """B2 + H1: SeedEnvelope.job_seed is bytes (length 8), not int."""

    def test_job_seed_is_bytes(self) -> None:
        plan = compile_plan(_minimal_config(), _minimal_profile(), decoy_engine_version="0.1.0")
        assert isinstance(plan.seed_envelope.job_seed, bytes)
        assert len(plan.seed_envelope.job_seed) == 8

    def test_job_seed_value_matches_config_int(self) -> None:
        """Config seed=42 normalizes to (42).to_bytes(8, 'big')."""
        plan = compile_plan(
            _minimal_config(seed=42), _minimal_profile(), decoy_engine_version="0.1.0"
        )
        assert plan.seed_envelope.job_seed == (42).to_bytes(8, "big")

    def test_job_seed_zero_default(self) -> None:
        """Missing global_settings.seed defaults to 0 -> b'\\x00' * 8."""
        config = {
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "fail",
                    "namespace": "customer_identity",
                }
            ]
        }
        plan = compile_plan(config, _minimal_profile(), decoy_engine_version="0.1.0")
        assert plan.seed_envelope.job_seed == b"\x00" * 8


class TestSeedOverflow:
    """B2/H1: seed_overflow PlanCompileError at the pipeline-config boundary."""

    def test_negative_seed_raises_overflow(self) -> None:
        with pytest.raises(PlanCompileError) as excinfo:
            compile_plan(_minimal_config(seed=-1), _minimal_profile(), decoy_engine_version="0.1.0")
        assert excinfo.value.code == "seed_overflow"
        assert excinfo.value.path == "global_settings.seed"

    def test_too_large_seed_raises_overflow(self) -> None:
        with pytest.raises(PlanCompileError) as excinfo:
            compile_plan(
                _minimal_config(seed=1 << 64), _minimal_profile(), decoy_engine_version="0.1.0"
            )
        assert excinfo.value.code == "seed_overflow"


class TestPlanSchemaDeltaDataclasses:
    """B2: column_seed / group_seed / table_seed / NamespaceBinding.seed
    are removed from the dataclasses. Constructing them with a legacy
    keyword argument raises TypeError."""

    def test_column_seed_rejects_legacy_kwarg(self) -> None:
        with pytest.raises(TypeError):
            ColumnSeed(
                column_seed=42,  # type: ignore[call-arg]
                namespace=None,
                strategy="x",
                provider="x",
                backend_type="faker",
                backend_version="stub",
                cardinality_mode="reuse",
            )

    def test_group_seed_rejects_legacy_kwarg(self) -> None:
        with pytest.raises(TypeError):
            GroupSeed(
                group_seed=42,  # type: ignore[call-arg]
                namespace="ns",
                coherent_columns=("a", "b"),
            )

    def test_table_seed_rejects_legacy_kwarg(self) -> None:
        with pytest.raises(TypeError):
            TableSeed(
                table_seed=42,  # type: ignore[call-arg]
                per_column=(),
                per_group=(),
            )

    def test_namespace_binding_rejects_legacy_seed_kwarg(self) -> None:
        with pytest.raises(TypeError):
            NamespaceBinding(
                namespace="ns",
                declared_by=(),
                seed=42,  # type: ignore[call-arg]
            )

    def test_seed_envelope_accepts_bytes_job_seed(self) -> None:
        env = SeedEnvelope(job_seed=b"\x00" * 8, per_table=())
        assert env.job_seed == b"\x00" * 8


class TestEndToEndDeriveWithPlanJobSeed:
    """H1 end-to-end: pass plan.seed_envelope.job_seed directly into derive_value
    without any conversion. Pins that the rest of the engine consumes bytes."""

    def test_derive_value_consumes_plan_job_seed_directly(self) -> None:
        plan = compile_plan(
            _minimal_config(seed=42), _minimal_profile(), decoy_engine_version="0.1.0"
        )
        # No int->bytes conversion here; the plan ships bytes.
        result = derive_value(
            plan.seed_envelope.job_seed,
            "customer_identity",
            b"some-source",
            domain=IdentityDomain(),
        )
        # Equals what derive(...) produces directly.
        expected = derive(plan.seed_envelope.job_seed, "customer_identity", b"some-source")
        assert result == expected
