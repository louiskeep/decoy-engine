"""GEN-UNIQUE E2: routes, pinned, retry bound, memory, pooling and value-free refusals.

AT-7, AT-9, AT-10, AT-11, AT-12 of the plan.
"""

from __future__ import annotations

import pytest

from decoy_engine import reasons
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.generation import _faker_pool, _unique
from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation._bounds import generation_floor_bytes, unique_set_bytes
from decoy_engine.generation._plan_entry import generate_tables, generate_tables_streamed
from decoy_engine.generation.pinned import PinnedModeError, compile_pinned_plan, execute_pinned
from decoy_engine.generation.pool import GenerationError
from decoy_engine.generation.window import classify_window_table
from decoy_engine.generators.derivation import GenDeriveContext, with_table
from decoy_engine.internal.faker_setup import register_faker_provider, unregister_faker_provider
from tests.parity._fr0_support import fingerprints, plan_of
from tests.unit.generation.unique._gu_e2_support import (
    MARKER,
    cfg,
    col_values,
    ref,
    run,
    seq_parent,
    table,
    validate,
)

IMPOSSIBLE = str(reasons.runtime.UNIQUENESS_IMPOSSIBLE)


def _cat(name: str = "c", **extra):
    return {
        "name": name,
        "type": "categorical",
        "categories": [f"v{i}" for i in range(30)],
        **extra,
    }


def _pair_tables(rows: int = 20):
    return [
        seq_parent("pa", 10),
        seq_parent("pb", 10),
        table("j", rows, [ref("a", "pa"), ref("b", "pb")], unique_together=[["a", "b"]]),
    ]


class TestAT7Routes:
    def _reasons(self, tables):
        adm = classify_plan(cfg(tables))
        return {(c.table, c.column): (c.eligible, c.reason) for c in adm.columns}

    def test_unique_columns_decline_with_unique(self):
        tables = [
            table(
                "t",
                10,
                [
                    _cat("cat", unique=True),
                    {"name": "f", "type": "faker", "faker_type": "email", "unique": True},
                ],
            ),
            seq_parent("p", 5),
            table("k", 5, [ref("pid", "p", unique=True)]),
        ]
        got = self._reasons(tables)
        assert got[("t", "cat")] == (False, "unique")
        assert got[("t", "f")] == (False, "unique")
        assert got[("k", "pid")] == (False, "unique")

    def test_pair_members_decline_with_unique_pair(self):
        got = self._reasons(_pair_tables())
        assert got[("j", "a")] == (False, "unique_pair")
        assert got[("j", "b")] == (False, "unique_pair")
        assert got[("pa", "id")][0] is True

    def test_sequence_with_unique_stays_admitted_with_equal_bytes(self):
        base = {"name": "s", "type": "sequence", "start": 1, "step": 2}
        assert self._reasons([table("t", 10, [{**base, "unique": True}])])[("t", "s")][0] is True
        a = run([table("t", 10, [{**base, "unique": True}])])
        b = run([table("t", 10, [base])])
        assert a["t"].equals(b["t"])

    def test_window_admission_refuses_class_b_unique(self):
        c = cfg([table("t", 10, [_cat("c", unique=True)])])
        verdict = classify_window_table(c, "t", statistical_specs={})
        assert verdict.klass == "B"
        assert ("c", "unique") in verdict.class_b_reasons

    def test_window_admission_pair_members_are_class_b(self):
        verdict = classify_window_table(cfg(_pair_tables()), "j", statistical_specs={})
        assert {r for _, r in verdict.class_b_reasons} == {"unique_pair"}

    def test_window_unique_reason_wins_over_null_injection(self):
        c = cfg([table("t", 10, [_cat("c", unique=True, null_probability=0.2)])])
        verdict = classify_window_table(c, "t", statistical_specs={})
        assert ("c", "unique") in verdict.class_b_reasons

    def test_whole_table_and_bounded_stream_give_equal_bytes(self, tmp_path):
        tables = [
            seq_parent("p", 40),
            table("k", 30, [ref("pid", "p", unique=True), _cat("c", unique=True)]),
        ]
        plan = plan_of(cfg(tables))
        whole = generate_tables(plan)
        result = generate_tables_streamed(plan, sink=ParquetTransactionalSink(tmp_path / "out"))
        # The streamed core declines the plan, so it runs resident and the caller owns the write.
        assert result.mode == "resident"
        assert result.reason.startswith("legacy:")
        assert "k.pid:unique" in result.declines
        assert fingerprints(result.tables) == fingerprints(whole)

    def test_adding_unique_to_one_column_leaves_the_others_identical(self):
        # U4.
        base = [
            seq_parent("p", 40),
            table(
                "k",
                30,
                [ref("pid", "p"), _cat("c"), {"name": "f", "type": "faker", "faker_type": "city"}],
            ),
        ]
        changed = [
            base[0],
            table(
                "k",
                30,
                [
                    ref("pid", "p"),
                    _cat("c", unique=True),
                    {"name": "f", "type": "faker", "faker_type": "city"},
                ],
            ),
        ]
        a, b = run(base), run(changed)
        for column in ("pid", "f"):
            assert col_values(a, "k", column) == col_values(b, "k", column)
        assert col_values(a, "k", "c") != col_values(b, "k", "c")

    def test_a_refusal_mid_table_publishes_nothing(self, tmp_path):
        tables = [
            seq_parent("p", 10, null_probability=0.5),
            table("k", 8, [ref("pid", "p", unique=True)]),
        ]
        out = tmp_path / "out"
        with pytest.raises(GenerationError):
            generate_tables_streamed(plan_of(cfg(tables)), sink=ParquetTransactionalSink(out))
        assert not out.exists()


class TestAT9Pooling:
    def test_pool_opt_out_includes_unique(self):
        assert _faker_pool.pool_opted_out({"unique": True}) is True
        assert _faker_pool.pool_opted_out({"unique": False}) is False

    def test_a_unique_column_builds_no_pool(self, monkeypatch):
        def boom(**_):
            raise AssertionError("pool built for a unique column")

        monkeypatch.setattr(_faker_pool, "build_and_sample", boom)
        col = {
            "name": "c",
            "type": "faker",
            "faker_type": "first_name",
            "unique": True,
            "_table": "t",
        }
        assert _faker_pool.try_pool(col, 60_000, 1, None, None, {}) is None

    def test_pooled_true_with_unique_is_refused(self):
        from pydantic import ValidationError

        col = {
            "name": "c",
            "type": "faker",
            "faker_type": "first_name",
            "pooled": True,
            "unique": True,
        }
        with pytest.raises(ValidationError) as exc:
            validate([table("t", 5, [col])])
        assert str(reasons.config.GENERATE_UNIQUE_CONFLICT) in [
            e["type"] for e in exc.value.errors()
        ]


class _FakeInstance:
    def __init__(self):
        self.seeds: list[int] = []

    def seed_instance(self, seed: int) -> None:
        self.seeds.append(seed)


def _ctx():
    col = with_table({"name": "c", "type": "faker", "faker_type": "x", "unique": True}, "t")
    return GenDeriveContext.for_column(derive_key=None, column_config=col, fallback_seed=7)


class TestAT12RetryAndMemory:
    def test_a_retry_reseeds_from_the_faker_unique_family(self):
        # M11 (retry from row_int("faker", i) again) turns this red.
        inst, ctx = _FakeInstance(), _ctx()
        row1_first, row1_retry = ctx.row_int("faker", 1), ctx.row_int("faker_unique", 1 * 1000 + 0)

        def provider(**_):
            return "dup" if inst.seeds[-1] in (ctx.row_int("faker", 0), row1_first) else "fresh"

        out = _unique.unique_faker(inst, provider, {}, ctx, 2, table="t", column="c")
        assert out == ["dup", "fresh"]
        assert inst.seeds == [ctx.row_int("faker", 0), row1_first, row1_retry]

    def test_always_colliding_provider_refuses_after_exactly_1000_attempts(self):
        inst, ctx = _FakeInstance(), _ctx()
        with pytest.raises(GenerationError) as exc:
            _unique.unique_faker(inst, lambda **_: "same", {}, ctx, 3, table="t", column="c")
        assert exc.value.code == IMPOSSIBLE
        assert len(inst.seeds) == 1 + 1000  # row 0 once, then 1000 attempts for row 1
        assert "row 2" in exc.value.message

    @pytest.mark.serial
    def test_unique_set_bytes_join_the_floor_as_a_peak_not_a_sum(self):
        base = [{"name": "s", "type": "sequence", "start": 1}]
        one = [*base, {"name": "f1", "type": "faker", "faker_type": "email", "unique": True}]
        two = [*one, {"name": "f2", "type": "faker", "faker_type": "email", "unique": True}]
        rows = 1000

        def floor(cols):
            c = cfg([table("t", rows, cols)])
            return generation_floor_bytes(c, classify_plan(c))

        peak = unique_set_bytes(rows, 320)
        assert floor(one) - floor(base) >= peak
        # The second column adds its list slots but not a second working set.
        assert floor(two) - floor(one) < peak

    def test_a_budget_that_fits_the_columns_but_not_the_seen_set_refuses(self):
        from decoy_engine.generation._bounds import legacy_table_bytes
        from decoy_engine.generation._budget_ledger import GenerationBudgetLedger
        from decoy_engine.generation._streamed import _fit_band, run_generation

        rows = 20_000
        col = {"name": "f", "type": "faker", "faker_type": "email"}
        legacy = legacy_table_bytes(rows, [320])
        limit = int(legacy * (1 + _fit_band())) + 1024
        plain = cfg([table("t", rows, [col])])
        assert run_generation(plain, ledger=GenerationBudgetLedger(limit)).tables is not None
        uniq = cfg([table("t", rows, [{**col, "unique": True}])])
        assert unique_set_bytes(rows, 320) * (1 + _fit_band()) > 1024 + 1
        with pytest.raises(Exception) as exc:
            run_generation(uniq, ledger=GenerationBudgetLedger(limit))
        assert getattr(exc.value, "code", None) == str(reasons.runtime.GENERATION_BUDGET_EXCEEDED)


class TestAT11Pinned:
    def _pinned_cfg(self, **kw):
        return cfg(
            [
                seq_parent("parent", 50),
                table("child", 25, [_cat("c", unique=True), ref("pid", "parent", unique=True)]),
            ],
            **kw,
        )

    def test_pinned_and_whole_table_routes_give_equal_bytes(self):
        c = self._pinned_cfg()
        c["global_settings"]["reference_date"] = "2026-10-22T09:30:00Z"
        pinned = execute_pinned(compile_pinned_plan(c), 4242)
        whole = run(c["tables"], seed=4242)
        for name in ("c", "pid"):
            assert col_values(pinned, "child", name) == col_values(whole, "child", name)

    def test_unique_together_is_refused_at_pinned_compile(self):
        c = cfg(_pair_tables())
        c["global_settings"]["reference_date"] = "2026-10-22T09:30:00Z"
        with pytest.raises(PinnedModeError) as exc:
            compile_pinned_plan(c)
        assert exc.value.code == str(reasons.plan.PINNED_UNIQUE_PAIR_UNSUPPORTED)


class TestAT10ValueFree:
    def _assert_free(self, exc):
        assert MARKER not in str(exc.value)
        assert MARKER not in getattr(exc.value, "message", "")

    def test_validate_refusals_do_not_echo_categories(self):
        from pydantic import ValidationError

        col = {"name": "c", "type": "categorical", "categories": [MARKER, "b"], "unique": True}
        with pytest.raises(ValidationError) as exc:
            validate([table("t", 5, [col])])
        self._assert_free(exc)

    def test_parent_key_values_do_not_reach_the_run_refusal(self):
        parent = seq_parent("parent", 20, prefix=MARKER, null_probability=0.5)
        with pytest.raises(GenerationError) as exc:
            run([parent, table("k", 18, [ref("pid", "parent", unique=True)])])
        self._assert_free(exc)

    @pytest.mark.parametrize("value", [MARKER, [MARKER]])
    def test_provider_output_does_not_reach_the_run_refusal(self, value):
        register_faker_provider("gu_marker", lambda fake: value)
        try:
            with pytest.raises(GenerationError) as exc:
                run(
                    [
                        table(
                            "t",
                            3,
                            [
                                {
                                    "name": "c",
                                    "type": "faker",
                                    "faker_type": "gu_marker",
                                    "unique": True,
                                }
                            ],
                        )
                    ]
                )
        finally:
            unregister_faker_provider("gu_marker")
        assert exc.value.code == IMPOSSIBLE
        self._assert_free(exc)
        assert exc.value.__cause__ is None
