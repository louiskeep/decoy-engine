"""MG-3 / M3 (2026-05-31): security cells for `when:` numexpr scope clamp.

Mirrors the Dennis C1 patch tests on `_transforms.py`: the same
scope-clamp pattern (engine='numexpr', local_dict={}, global_dict={})
protects `when:` from `@var`-style scope walks reaching module-top
imports.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.execution._when_gate import run_with_when_gate
from decoy_engine.plan._types import ColumnSeed


def _seed(when: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="redact",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=(),
        when=when,
    )


class _Ctx:
    pass


class TestNumexprScopeClamp:
    def test_when_at_var_scope_walk_blocked(self):
        # The numexpr engine plus empty local_dict/global_dict blocks
        # @var-style scope walks. The eval raises rather than walking
        # out of the local scope.
        df = pd.DataFrame({"v": ["a"], "n": [1]})
        with pytest.raises(StrategyError) as exc:
            run_with_when_gate(
                RedactHandler(),
                df,
                "v",
                _seed("@pd.compat.os.system('echo x') > 0"),
                _Ctx(),
            )
        assert exc.value.code == "when_expression_error"

    def test_when_unknown_name_raises_not_evaluated_via_python(self):
        # An unknown name in a numexpr expression must raise rather
        # than fall back to Python eval. The scope-clamp ensures the
        # name has no chance to resolve.
        df = pd.DataFrame({"v": ["a"], "n": [1]})
        with pytest.raises(StrategyError) as exc:
            run_with_when_gate(
                RedactHandler(),
                df,
                "v",
                _seed("unknown_module.attr == 1"),
                _Ctx(),
            )
        assert exc.value.code == "when_expression_error"

    def test_when_dunder_attribute_access_blocked(self):
        # Dunder access in pandas eval is filtered by parser; under
        # numexpr it never even reaches a name resolver. Either way,
        # the call must raise.
        df = pd.DataFrame({"v": ["a"], "n": [1]})
        with pytest.raises(StrategyError) as exc:
            run_with_when_gate(
                RedactHandler(),
                df,
                "v",
                _seed("n.__class__ == 1"),
                _Ctx(),
            )
        assert exc.value.code == "when_expression_error"

    def test_when_import_statement_blocked(self):
        # Statement-level constructs are syntax errors in numexpr.
        df = pd.DataFrame({"v": ["a"], "n": [1]})
        with pytest.raises(StrategyError) as exc:
            run_with_when_gate(
                RedactHandler(),
                df,
                "v",
                _seed("import os"),
                _Ctx(),
            )
        assert exc.value.code == "when_expression_error"
