"""TQ mutation-kill oracles for `PandasExecutionAdapter`'s SMALL surfaces:
`__init__` (handler table), `run_single` (the single-table convenience guard),
the `_fk_key_value` compat wrapper, and the `get_default_executor` singleton.
The big `run` / `_dispatch_mask_node` / `_resolve_fk_node` / `_parent_map`
methods are graded by their own kill files. These drive the leaf surfaces
directly and assert the machine fields each owns.
"""

from __future__ import annotations

import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution._fk_keys import fk_key_value
from decoy_engine.execution._pandas_adapter import (
    PandasExecutionAdapter,
    _fk_key_value,
    _reset_default_executor_for_tests,
    get_default_executor,
)
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.relationships._graph import OrphanPolicy
from tests.perf_fixtures.fk_relational import build_fk_relational

_SECRET = SecretKeyProvider(b"a-strong-32B+-managed-secret-value!!", key_version="v1")


class TestAdapterInit:
    def test_configured_fpe_chunk_count_reaches_the_fpe_key(self) -> None:
        # __init__ REPLACES the shared SCALAR "fpe" handler with one built at the
        # adapter's fpe_chunk_count, so the knob is live. Registering it under a
        # renamed key ("XXfpeXX" / "FPE") leaves the default-chunk-count SCALAR
        # handler under "fpe", so a non-default configured count never lands. Use
        # a non-default (9) since the default is 4 on both sides.
        adapter = PandasExecutionAdapter(fpe_chunk_count=9)
        assert adapter.supports_strategy("fpe") is True
        assert adapter._handlers["fpe"]._chunk_count == 9


class TestFkKeyValueWrapper:
    def test_delegates_the_real_value_not_none(self) -> None:
        # `_fk_key_value(value)` must forward `value` to `fk_key_value`; the
        # `fk_key_value(None)` mutant returns the null-sentinel normalization for
        # every input.
        assert _fk_key_value(5) == fk_key_value(5)
        assert _fk_key_value(5) != fk_key_value(None)
        assert _fk_key_value("abc") == fk_key_value("abc")


class TestGetDefaultExecutor:
    def test_returns_a_real_adapter_not_none(self) -> None:
        # `cached = select_execution_adapter()` mutated to `cached = None` makes
        # the singleton hand back None (and poison the cache). Reset first so the
        # assertion sees this call's result, not a prior cached instance.
        _reset_default_executor_for_tests()
        try:
            executor = get_default_executor()
            assert executor is not None
            assert hasattr(executor, "run")
        finally:
            _reset_default_executor_for_tests()


class TestRunSingleTableGuard:
    def test_multi_table_plan_without_explicit_table_fails_closed(self) -> None:
        # run_single infers the table only for a single-table plan; a 2-table
        # plan with no `table=` must raise the coded error. Kills the code
        # nulled/renamed/re-cased mutants (the code is the machine field).
        fx = build_fk_relational(rows=20, width=2, orphan_frac=0.0)
        source = next(iter(fx.sources.values()))
        with pytest.raises(ExecutionError) as exc:
            PandasExecutionAdapter().run_single(
                fx.plan,
                source,
                registry=fx.registry,
                relationship_graph=fx.graph(OrphanPolicy.PRESERVE),
                namespace_registry=fx.namespace_registry,
                table=None,
            )
        assert exc.value.code == "run_single_requires_table"
        # identifying data in the message (kills a nulled message); the prose
        # itself is left equivalent (house style, code pinned above).
        assert "table=" in exc.value.message

    def test_key_provider_forwarded_to_run(self) -> None:
        # run_single forwards `key_provider` to `run`; a keyed column masks off
        # the managed secret WITH it and off job_seed WITHOUT it, so the outputs
        # differ. Kills the key_provider=None (mut_21) and dropped-kwarg (mut_28)
        # mutants -- the mask-key derivation path on this public method.
        adapter = PandasExecutionAdapter()
        fx = build_fk_relational(rows=120, width=1, orphan_frac=0.0)
        graph = fx.graph(OrphanPolicy.PRESERVE)
        source = fx.sources["parent"]
        common = {
            "registry": fx.registry,
            "relationship_graph": graph,
            "namespace_registry": fx.namespace_registry,
            "table": "parent",
        }
        unkeyed = adapter.run_single(fx.plan, source, **common)
        keyed = adapter.run_single(fx.plan, source, key_provider=_SECRET, **common)
        assert not keyed.outputs["parent"].equals(unkeyed.outputs["parent"])
