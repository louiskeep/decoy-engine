"""SP-46: opt-in FPE join groups -- T1-T8 test suite.

Decision recap (Cam-ratified 2026-06-29):
  A: active join group -> compile-time manifest warning + run-time QualityWarning.
  B: config key = fpe_join_group (in provider_config).
  C: tweak change mirrored in V1 transforms/fpe.py FPEStrategy.
  D: a join group with < 2 members is a HARD compile error.

Mechanism: tweak = (fpe_join_group or column).encode("utf-8", errors="replace").
Key derivation UNCHANGED. No SEED_PROTOCOL_VERSION bump.

T1: join works (same value -> identical ciphertext, intra + cross-table).
T2: F3 NOT regressed -- two fpe cols same namespace, NO shared group -> DIFFERENT ciphertext.
T3: determinism (re-run byte-identical).
T4: default unchanged + existing FPE golden snapshots pass UNMODIFIED.
T5: unmask round-trip on a joined column.
T6: plan-check rejections (singleton/mixed-charset/non-fpe/cross-namespace each raises its code).
T7: cross-group isolation (two different groups, same value -> different ciphertext).
T8: audit signal emitted (QualityWarning + compile-time manifest record).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.profile._types import Profile
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_EMPTY_PROFILE = Profile(
    schema_version=1,
    tables=(),
    relationships=(),
    profiled_at=datetime(2026, 6, 29, 0, 0, 0),
    decoy_engine_version="sp46-test",
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0xDECAFC0FFEE).to_bytes(8, "big")


def _ctx() -> StrategyContext:
    return StrategyContext(
        registry=_REG,
        pool_cache=PoolCache(),
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
        job_seed=_SEED,
    )


def _fpe_col(
    *,
    namespace: str = "phone_ns",
    join_group: str | None = None,
    charset: str = "digits",
) -> ColumnSeed:
    pc: list[tuple[str, object]] = [("charset", charset)]
    if join_group is not None:
        pc.append(("fpe_join_group", join_group))
    return ColumnSeed(
        namespace=namespace,
        strategy="fpe",
        provider="fpe",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=tuple(pc),
        coherent_with=(),
    )


# ---------------------------------------------------------------------------
# T1: join works -- same value, different column names, same join_group
# ---------------------------------------------------------------------------


class TestT1JoinWorks:
    def test_intra_table_same_ciphertext(self) -> None:
        """Two columns in the same table with the same fpe_join_group and value
        must produce identical ciphertext (the join property)."""
        value = "5551234567"
        df_a = pd.DataFrame({"msisdn": [value]})
        df_b = pd.DataFrame({"called_msisdn": [value]})

        plan_a = _fpe_col(namespace="phone_ns", join_group="phone_e164")
        plan_b = _fpe_col(namespace="phone_ns", join_group="phone_e164")

        out_a, _ = FpeStrategyHandler(chunk_count=1).run(df_a, "msisdn", plan_a, _ctx())
        out_b, _ = FpeStrategyHandler(chunk_count=1).run(df_b, "called_msisdn", plan_b, _ctx())

        assert out_a["msisdn"].tolist()[0] == out_b["called_msisdn"].tolist()[0], (
            "Joined columns must produce identical ciphertext for the same value"
        )

    def test_cross_table_same_ciphertext(self) -> None:
        """Telco scenario: subscribers.msisdn + cdr.called_msisdn share a join
        group and must encrypt identically so cross-table joins survive masking."""
        value = "4155552671"
        # Simulate two independent table mask runs.
        df_subscribers = pd.DataFrame({"msisdn": [value, "9998887776"]})
        df_cdr = pd.DataFrame({"called_msisdn": [value, "1112223334"]})

        plan = _fpe_col(namespace="phone_ns", join_group="phone_e164")

        out_sub, _ = FpeStrategyHandler(chunk_count=1).run(df_subscribers, "msisdn", plan, _ctx())
        out_cdr, _ = FpeStrategyHandler(chunk_count=1).run(df_cdr, "called_msisdn", plan, _ctx())

        enc_sub = out_sub["msisdn"].tolist()[0]
        enc_cdr = out_cdr["called_msisdn"].tolist()[0]
        assert enc_sub == enc_cdr, (
            "Cross-table join must survive masking: same value + same group + "
            f"same namespace -> identical ciphertext, got sub={enc_sub!r} cdr={enc_cdr!r}"
        )

    def test_different_values_encrypt_differently(self) -> None:
        """Sanity: two distinct values in the same join group still encrypt distinctly."""
        plan = _fpe_col(namespace="phone_ns", join_group="phone_e164")
        df = pd.DataFrame({"msisdn": ["1111111111", "2222222222"]})
        out, _ = FpeStrategyHandler(chunk_count=1).run(df, "msisdn", plan, _ctx())
        vals = out["msisdn"].tolist()
        assert vals[0] != vals[1]


# ---------------------------------------------------------------------------
# T2: F3 NOT regressed -- no shared group -> DIFFERENT ciphertext
# ---------------------------------------------------------------------------


class TestT2F3NotRegressed:
    """CRITICAL regression guard: the F3 domain-separation invariant must be intact.

    Two fpe columns sharing the same namespace but NO fpe_join_group must produce
    DIFFERENT ciphertexts for the same value. This is the existing guarantee that
    column A in namespace X and column B in namespace X are independent.

    If this test fails, the default-tweak behaviour has regressed (the `join_group
    or column` guard must evaluate to the column name when no join_group is set).
    """

    def test_no_join_group_columns_differ(self) -> None:
        value = "5551234567"
        plan_no_group = _fpe_col(namespace="phone_ns", join_group=None)

        df_a = pd.DataFrame({"col_a": [value]})
        df_b = pd.DataFrame({"col_b": [value]})

        out_a, _ = FpeStrategyHandler(chunk_count=1).run(df_a, "col_a", plan_no_group, _ctx())
        out_b, _ = FpeStrategyHandler(chunk_count=1).run(df_b, "col_b", plan_no_group, _ctx())

        enc_a = out_a["col_a"].tolist()[0]
        enc_b = out_b["col_b"].tolist()[0]
        assert enc_a != enc_b, (
            "F3 regression: two fpe columns with the same namespace but NO join_group "
            "must produce different ciphertext (tweak = column name, not group name). "
            f"Got enc_a={enc_a!r} enc_b={enc_b!r}"
        )

    def test_join_group_differs_from_no_group(self) -> None:
        """A joined column and a non-joined column (same namespace, same value)
        must produce different ciphertexts -- the join group changes the tweak."""
        value = "5551234567"
        plan_joined = _fpe_col(namespace="phone_ns", join_group="phone_e164")
        plan_plain = _fpe_col(namespace="phone_ns", join_group=None)

        df = pd.DataFrame({"col": [value]})
        out_joined, _ = FpeStrategyHandler(chunk_count=1).run(df.copy(), "col", plan_joined, _ctx())
        out_plain, _ = FpeStrategyHandler(chunk_count=1).run(df.copy(), "col", plan_plain, _ctx())
        # When join_group="phone_e164" the tweak is b"phone_e164"; when unset the
        # tweak is b"col". So the outputs will differ (unless a hash collision occurs,
        # which is negligible for a 10-digit test value).
        enc_joined = out_joined["col"].tolist()[0]
        enc_plain = out_plain["col"].tolist()[0]
        assert enc_joined != enc_plain, (
            "A joined column (tweak=group name) must differ from a plain column "
            "(tweak=column name) even for the same value + namespace."
        )


# ---------------------------------------------------------------------------
# T3: determinism -- re-run byte-identical
# ---------------------------------------------------------------------------


class TestT3Determinism:
    def test_rerun_identical(self) -> None:
        plan = _fpe_col(namespace="phone_ns", join_group="phone_e164")
        df = pd.DataFrame({"msisdn": ["5551234567", "9998887776", "1112223334"]})

        out1, _ = FpeStrategyHandler(chunk_count=1).run(df.copy(), "msisdn", plan, _ctx())
        out2, _ = FpeStrategyHandler(chunk_count=1).run(df.copy(), "msisdn", plan, _ctx())

        assert out1["msisdn"].tolist() == out2["msisdn"].tolist()

    def test_chunked_serial_parity(self) -> None:
        """chunk_count=1 vs chunk_count=4 must be byte-identical for joined columns."""
        plan = _fpe_col(namespace="phone_ns", join_group="phone_e164")
        rows = [f"{i:010d}" for i in range(50)]

        serial, _ = FpeStrategyHandler(chunk_count=1).run(
            pd.DataFrame({"msisdn": list(rows)}), "msisdn", plan, _ctx()
        )
        parallel, _ = FpeStrategyHandler(chunk_count=4).run(
            pd.DataFrame({"msisdn": list(rows)}), "msisdn", plan, _ctx()
        )
        assert serial["msisdn"].tolist() == parallel["msisdn"].tolist()


# ---------------------------------------------------------------------------
# T4: default unchanged -- no fpe_join_group -> byte-identical to pre-SP46
# ---------------------------------------------------------------------------


class TestT4DefaultUnchanged:
    """The golden snapshot guarantee: existing FPE output with no fpe_join_group
    must be byte-identical. This test pins the tweak-equals-column behaviour and
    would fail immediately if `join_group or column` ever evaluated to group_name
    when join_group is None/missing."""

    def test_no_group_matches_column_tweak(self) -> None:
        from decoy_engine.determinism import derive
        from decoy_engine.execution._strategies._fpe import FPE_KEY_LABEL
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        value = "1234567890"
        plan = _fpe_col(namespace="phone_ns", join_group=None)
        df = pd.DataFrame({"acct": [value]})
        out, _ = FpeStrategyHandler(chunk_count=1).run(df, "acct", plan, _ctx())
        actual = out["acct"].tolist()[0]

        # Reproduce manually with tweak = column name (the pre-SP46 default)
        key = derive(_SEED, "phone_ns", FPE_KEY_LABEL)
        expected = fpe_encrypt_value(value, key, "0123456789", b"acct")
        assert actual == expected, (
            "Default (no fpe_join_group) must be byte-identical to tweak=column_name"
        )

    def test_null_passthrough_unchanged(self) -> None:
        plan = _fpe_col(namespace="phone_ns", join_group=None)
        df = pd.DataFrame({"acct": ["12345", None, "99999"]})
        out, _ = FpeStrategyHandler(chunk_count=1).run(df, "acct", plan, _ctx())
        vals = out["acct"].tolist()
        assert pd.isna(vals[1])
        assert len(vals[0]) == 5 and vals[0].isdigit()


# ---------------------------------------------------------------------------
# T5: unmask round-trip on a joined column
# ---------------------------------------------------------------------------


class TestT5UnmaskRoundTrip:
    def test_fpe_joined_column_round_trips(self, tmp_path) -> None:
        """A joined column must decrypt back to the source value (unmask_pipeline).

        The config needs two members in the same group to pass the compile check
        (singleton raises fpe_join_group_singleton). Both members are verified to
        round-trip independently.
        """
        from decoy_engine import unmask_pipeline
        from decoy_engine.config import PipelineConfig
        from decoy_engine.execution import run_pipeline

        source_msisdn = ["5551234567", "4155552671", "9998887776"]
        source_called = ["4155552671", "9998887776", "5551234567"]

        cfg_raw = {
            "version": 1,
            "global_settings": {"seed": 999},
            "sources": {
                "subs": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "subs.csv"),
                },
            },
            "tables": [
                {
                    "name": "subs",
                    "columns": [
                        {
                            "name": "msisdn",
                            "strategy": "fpe",
                            "namespace": "phone_ns",
                            "provider_config": {
                                "charset": "digits",
                                "fpe_join_group": "phone_e164",
                            },
                        },
                        {
                            "name": "called_msisdn",
                            "strategy": "fpe",
                            "namespace": "phone_ns",
                            "provider_config": {
                                "charset": "digits",
                                "fpe_join_group": "phone_e164",
                            },
                        },
                    ],
                }
            ],
            "targets": {
                "subs": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "out.csv"),
                },
            },
        }
        cfg = PipelineConfig.model_validate(cfg_raw).model_dump()

        df = pd.DataFrame({"msisdn": source_msisdn, "called_msisdn": source_called})
        df.to_csv(tmp_path / "subs.csv", index=False)
        sources = {"subs": pa.Table.from_pandas(df, preserve_index=False)}

        result = run_pipeline(cfg, sources=sources, engine_version="sp46-test")
        masked = dict(result.outputs)

        # Masked values should differ from source
        masked_msisdn = masked["subs"].column("msisdn").to_pylist()
        assert masked_msisdn != source_msisdn

        # Unmask must recover both columns exactly
        unmask_result = unmask_pipeline(cfg, masked)
        recovered_msisdn = unmask_result.outputs["subs"].column("msisdn").to_pylist()
        recovered_called = unmask_result.outputs["subs"].column("called_msisdn").to_pylist()
        assert recovered_msisdn == source_msisdn, (
            f"Unmask of joined msisdn failed: {recovered_msisdn!r} != {source_msisdn!r}"
        )
        assert recovered_called == source_called, (
            f"Unmask of joined called_msisdn failed: {recovered_called!r} != {source_called!r}"
        )

        # The shared value must encrypt identically across both columns
        # (source_msisdn[0] == "5551234567", source_called[2] == "5551234567")
        assert masked_msisdn[0] == masked["subs"].column("called_msisdn").to_pylist()[2], (
            "The same value must encrypt to the same ciphertext in both columns of the group"
        )

    def test_cross_table_both_decrypt(self, tmp_path) -> None:
        """Two columns across two tables in the same join group must each decrypt
        to their source value, and their shared plaintext encrypts identically.

        In real usage both tables live in one pipeline config. The join group
        spans tables; the compile check sees both members so no singleton error.
        """
        from decoy_engine import unmask_pipeline
        from decoy_engine.config import PipelineConfig
        from decoy_engine.execution import run_pipeline

        source_msisdn = ["5551234567", "4155552671"]
        source_called = ["4155552671", "9998887776"]

        # Both tables in one config -- the compile check sees both members.
        raw = {
            "version": 1,
            "global_settings": {"seed": 777},
            "sources": {
                "subscribers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "subs.csv"),
                },
                "cdr": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "cdr.csv"),
                },
            },
            "tables": [
                {
                    "name": "subscribers",
                    "columns": [
                        {
                            "name": "msisdn",
                            "strategy": "fpe",
                            "namespace": "phone_ns",
                            "provider_config": {
                                "charset": "digits",
                                "fpe_join_group": "phone_e164",
                            },
                        }
                    ],
                },
                {
                    "name": "cdr",
                    "columns": [
                        {
                            "name": "called_msisdn",
                            "strategy": "fpe",
                            "namespace": "phone_ns",
                            "provider_config": {
                                "charset": "digits",
                                "fpe_join_group": "phone_e164",
                            },
                        }
                    ],
                },
            ],
            "targets": {
                "subscribers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "subs_out.csv"),
                },
                "cdr": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "cdr_out.csv"),
                },
            },
        }
        cfg = PipelineConfig.model_validate(raw).model_dump()

        df_a = pd.DataFrame({"msisdn": source_msisdn})
        df_b = pd.DataFrame({"called_msisdn": source_called})
        df_a.to_csv(tmp_path / "subs.csv", index=False)
        df_b.to_csv(tmp_path / "cdr.csv", index=False)

        sources = {
            "subscribers": pa.Table.from_pandas(df_a, preserve_index=False),
            "cdr": pa.Table.from_pandas(df_b, preserve_index=False),
        }
        result = run_pipeline(cfg, sources=sources, engine_version="sp46-test")
        masked = dict(result.outputs)

        # Both columns must unmask correctly
        unmask_result = unmask_pipeline(cfg, masked)
        assert unmask_result.outputs["subscribers"].column("msisdn").to_pylist() == source_msisdn
        assert unmask_result.outputs["cdr"].column("called_msisdn").to_pylist() == source_called

        # The shared value "4155552671" must encrypt identically across tables
        enc_sub = masked["subscribers"].column("msisdn").to_pylist()[1]
        enc_cdr = masked["cdr"].column("called_msisdn").to_pylist()[0]
        assert enc_sub == enc_cdr, (
            f"Cross-table join broken: subscribers.msisdn[1]={enc_sub!r} "
            f"!= cdr.called_msisdn[0]={enc_cdr!r} (both are '4155552671')"
        )


# ---------------------------------------------------------------------------
# T6: plan-check rejections
# ---------------------------------------------------------------------------


class TestT6PlanCheckRejections:
    """check_fpe_join_groups must reject misconfigured join groups at compile time."""

    def _run_check(self, tables: list[dict]) -> None:
        from decoy_engine.plan._checks_fpe_join import check_fpe_join_groups

        check_fpe_join_groups({"tables": tables})

    def test_singleton_raises(self) -> None:
        """D: a group with < 2 members is a HARD compile error."""
        with pytest.raises(PlanCompileError) as exc:
            self._run_check(
                [
                    {
                        "name": "subs",
                        "columns": [
                            {
                                "name": "msisdn",
                                "strategy": "fpe",
                                "namespace": "phone_ns",
                                "provider_config": {"fpe_join_group": "phone_e164"},
                            }
                        ],
                    }
                ]
            )
        assert exc.value.code == "fpe_join_group_singleton"

    def test_non_fpe_member_raises(self) -> None:
        with pytest.raises(PlanCompileError) as exc:
            self._run_check(
                [
                    {
                        "name": "subs",
                        "columns": [
                            {
                                "name": "msisdn",
                                "strategy": "fpe",
                                "namespace": "phone_ns",
                                "provider_config": {"fpe_join_group": "phone_e164"},
                            },
                            {
                                "name": "called_msisdn",
                                "strategy": "hash",  # NOT fpe
                                "namespace": "phone_ns",
                                "provider_config": {"fpe_join_group": "phone_e164"},
                            },
                        ],
                    }
                ]
            )
        assert exc.value.code == "fpe_join_group_non_fpe_member"

    def test_cross_namespace_raises(self) -> None:
        with pytest.raises(PlanCompileError) as exc:
            self._run_check(
                [
                    {
                        "name": "subs",
                        "columns": [
                            {
                                "name": "msisdn",
                                "strategy": "fpe",
                                "namespace": "phone_ns_a",
                                "provider_config": {"fpe_join_group": "phone_e164"},
                            },
                        ],
                    },
                    {
                        "name": "cdr",
                        "columns": [
                            {
                                "name": "called_msisdn",
                                "strategy": "fpe",
                                "namespace": "phone_ns_b",  # different namespace
                                "provider_config": {"fpe_join_group": "phone_e164"},
                            },
                        ],
                    },
                ]
            )
        assert exc.value.code == "fpe_join_group_namespace_mismatch"

    def test_config_mismatch_charset_raises(self) -> None:
        with pytest.raises(PlanCompileError) as exc:
            self._run_check(
                [
                    {
                        "name": "subs",
                        "columns": [
                            {
                                "name": "msisdn",
                                "strategy": "fpe",
                                "namespace": "phone_ns",
                                "provider_config": {
                                    "charset": "digits",
                                    "fpe_join_group": "phone_e164",
                                },
                            },
                            {
                                "name": "called_msisdn",
                                "strategy": "fpe",
                                "namespace": "phone_ns",
                                "provider_config": {
                                    "charset": "alpha",  # DIFFERENT charset
                                    "fpe_join_group": "phone_e164",
                                },
                            },
                        ],
                    }
                ]
            )
        assert exc.value.code == "fpe_join_group_config_mismatch"

    def test_valid_two_member_group_passes(self) -> None:
        """A correctly configured group must not raise."""
        from decoy_engine.plan._checks_fpe_join import check_fpe_join_groups

        check_fpe_join_groups(
            {
                "tables": [
                    {
                        "name": "subs",
                        "columns": [
                            {
                                "name": "msisdn",
                                "strategy": "fpe",
                                "namespace": "phone_ns",
                                "provider_config": {
                                    "charset": "digits",
                                    "fpe_join_group": "phone_e164",
                                },
                            },
                            {
                                "name": "called_msisdn",
                                "strategy": "fpe",
                                "namespace": "phone_ns",
                                "provider_config": {
                                    "charset": "digits",
                                    "fpe_join_group": "phone_e164",
                                },
                            },
                        ],
                    }
                ]
            }
        )  # must not raise


# ---------------------------------------------------------------------------
# T7: cross-group isolation -- two groups, same value -> different ciphertext
# ---------------------------------------------------------------------------


class TestT7CrossGroupIsolation:
    def test_different_groups_produce_different_ciphertext(self) -> None:
        """Two distinct groups must not share ciphertext for the same value.
        The group name IS the tweak; different tweaks -> different outputs."""
        value = "5551234567"
        plan_a = _fpe_col(namespace="phone_ns", join_group="group_alpha")
        plan_b = _fpe_col(namespace="phone_ns", join_group="group_beta")

        df = pd.DataFrame({"col": [value]})
        out_a, _ = FpeStrategyHandler(chunk_count=1).run(df.copy(), "col", plan_a, _ctx())
        out_b, _ = FpeStrategyHandler(chunk_count=1).run(df.copy(), "col", plan_b, _ctx())

        enc_a = out_a["col"].tolist()[0]
        enc_b = out_b["col"].tolist()[0]
        assert enc_a != enc_b, (
            "Cross-group isolation violated: two distinct groups must produce "
            f"different ciphertexts for the same value (got {enc_a!r} == {enc_b!r})"
        )

    def test_group_member_differs_from_non_group_column_of_same_name(self) -> None:
        """A column in a join group differs from the same column without a group
        (tweak is group name vs column name -- they are different unless a hash
        collision, which is negligible)."""
        value = "5551234567"
        plan_joined = _fpe_col(namespace="phone_ns", join_group="phone_e164")
        plan_plain = _fpe_col(namespace="phone_ns", join_group=None)

        df = pd.DataFrame({"msisdn": [value]})
        out_joined, _ = FpeStrategyHandler(chunk_count=1).run(
            df.copy(), "msisdn", plan_joined, _ctx()
        )
        out_plain, _ = FpeStrategyHandler(chunk_count=1).run(
            df.copy(), "msisdn", plan_plain, _ctx()
        )

        enc_joined = out_joined["msisdn"].tolist()[0]
        enc_plain = out_plain["msisdn"].tolist()[0]
        assert enc_joined != enc_plain


# ---------------------------------------------------------------------------
# T8: audit signal -- QualityWarning emitted + manifest record
# ---------------------------------------------------------------------------


class TestT8AuditSignal:
    def test_quality_warning_emitted_when_join_group_active(self) -> None:
        """When fpe_join_group is set, the strategy handler must return a
        QualityWarning with code 'fpe_join_group_active'."""
        plan = _fpe_col(namespace="phone_ns", join_group="phone_e164")
        df = pd.DataFrame({"msisdn": ["5551234567"]})

        _df, warnings = FpeStrategyHandler(chunk_count=1).run(df, "msisdn", plan, _ctx())

        assert len(warnings) == 1
        w = warnings[0]
        assert isinstance(w, QualityWarning)
        assert w.code == "fpe_join_group_active"
        assert w.column == "msisdn"
        assert "phone_e164" in str(w.detail)

    def test_no_warning_when_no_join_group(self) -> None:
        """Without fpe_join_group, run() must return an empty warning list."""
        plan = _fpe_col(namespace="phone_ns", join_group=None)
        df = pd.DataFrame({"acct": ["5551234567"]})

        _df, warnings = FpeStrategyHandler(chunk_count=1).run(df, "acct", plan, _ctx())
        assert warnings == []

    def test_compile_time_manifest_record_in_warnings(self, tmp_path) -> None:
        """When a join group is configured, compile_plan must add a warning
        string to plan_compile.warnings naming the group + members."""
        from decoy_engine.config import PipelineConfig
        from decoy_engine.plan._compile import compile_plan

        cfg_raw = {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "subs": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "subs.csv"),
                }
            },
            "tables": [
                {
                    "name": "subs",
                    "columns": [
                        {
                            "name": "msisdn",
                            "strategy": "fpe",
                            "namespace": "phone_ns",
                            "provider_config": {
                                "charset": "digits",
                                "fpe_join_group": "phone_e164",
                            },
                        },
                        {
                            "name": "called_msisdn",
                            "strategy": "fpe",
                            "namespace": "phone_ns",
                            "provider_config": {
                                "charset": "digits",
                                "fpe_join_group": "phone_e164",
                            },
                        },
                    ],
                }
            ],
            "targets": {
                "subs": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "out.csv"),
                }
            },
        }
        cfg = PipelineConfig.model_validate(cfg_raw).model_dump()
        plan = compile_plan(cfg, _EMPTY_PROFILE, decoy_engine_version="sp46-test", no_profile=True)

        warning_text = " ".join(plan.plan_compile.warnings)
        assert "phone_e164" in warning_text, (
            "Manifest must name the join group in plan_compile.warnings"
        )
        assert "msisdn" in warning_text or "called_msisdn" in warning_text, (
            "Manifest must name the member columns"
        )

    def test_fpe_join_groups_check_in_checks_passed(self, tmp_path) -> None:
        """'fpe_join_groups' must appear in plan_compile.checks_passed."""
        from decoy_engine.config import PipelineConfig
        from decoy_engine.plan._compile import compile_plan

        cfg_raw = {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "t": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "t.csv"),
                }
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "a",
                            "strategy": "fpe",
                            "namespace": "ns",
                            "provider_config": {
                                "charset": "digits",
                                "fpe_join_group": "grp",
                            },
                        },
                        {
                            "name": "b",
                            "strategy": "fpe",
                            "namespace": "ns",
                            "provider_config": {
                                "charset": "digits",
                                "fpe_join_group": "grp",
                            },
                        },
                    ],
                }
            ],
            "targets": {
                "t": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "out.csv"),
                }
            },
        }
        cfg = PipelineConfig.model_validate(cfg_raw).model_dump()
        plan = compile_plan(cfg, _EMPTY_PROFILE, decoy_engine_version="sp46-test", no_profile=True)

        assert "fpe_join_groups" in plan.plan_compile.checks_passed
