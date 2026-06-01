"""Unit tests for decoy_engine.expressions.

Verifies the safe_eval wrapper, the MASK_GLOBALS allowlist, and
BASE_GLOBALS builtins suppression. The goal is that all Python eval()
calls in the engine are auditable through this module.
"""

from __future__ import annotations

import pytest

from decoy_engine.expressions import BASE_GLOBALS, MASK_GLOBALS, safe_eval


class TestSafeEval:
    def test_evaluates_simple_expression(self):
        result = safe_eval("1 + 1", BASE_GLOBALS, {})
        assert result == 2

    def test_evaluates_with_locals(self):
        result = safe_eval("x * 2", BASE_GLOBALS, {"x": 5})
        assert result == 10

    def test_string_result(self):
        result = safe_eval("'hello' + ' world'", BASE_GLOBALS, {})
        assert result == "hello world"

    def test_none_result(self):
        result = safe_eval("None", BASE_GLOBALS, {})
        assert result is None


class TestBuiltinsSuppression:
    def test_base_globals_blocks_builtins(self):
        """BASE_GLOBALS must suppress __builtins__ so callers can't reach
        open(), exec(), __import__, etc. from within a formula."""
        with pytest.raises((NameError, TypeError)):
            safe_eval("open('/etc/passwd')", BASE_GLOBALS, {})

    def test_mask_globals_blocks_builtins(self):
        with pytest.raises((NameError, TypeError)):
            safe_eval("open('/etc/passwd')", MASK_GLOBALS, {})

    def test_base_globals_blocks_import(self):
        with pytest.raises((NameError, ImportError, TypeError)):
            safe_eval("__import__('os')", BASE_GLOBALS, {})


class TestMaskGlobals:
    def test_str_available(self):
        assert safe_eval("str(42)", MASK_GLOBALS, {}) == "42"

    def test_int_available(self):
        assert safe_eval("int('7')", MASK_GLOBALS, {}) == 7

    def test_re_available(self):
        result = safe_eval("re.sub(r'[0-9]', 'X', value)", MASK_GLOBALS, {"value": "abc123"})
        assert result == "abcXXX"

    def test_abs_available(self):
        assert safe_eval("abs(-5)", MASK_GLOBALS, {}) == 5

    def test_value_local_passes_through(self):
        result = safe_eval("value.upper()", MASK_GLOBALS, {"value": "hello"})
        assert result == "HELLO"


class TestQA1MakeMaskGlobals:
    """QA-1 M21 (2026-06-01): the make_mask_globals factory returns a
    MASK_GLOBALS scope with RNG bindings targeting an isolated Random
    instance, so two formula strategies in the same job no longer
    share module-global RNG state."""

    def test_make_mask_globals_returns_isolated_rng(self):
        import random

        from decoy_engine.expressions import make_mask_globals, safe_eval

        rng_a = random.Random(42)
        rng_b = random.Random(42)
        scope_a = make_mask_globals(rng_a)
        scope_b = make_mask_globals(rng_b)
        # Same seed -> same sequence even though each scope has its
        # own Random instance.
        a1 = safe_eval("randint(1, 1000)", scope_a, {})
        b1 = safe_eval("randint(1, 1000)", scope_b, {})
        assert a1 == b1

    def test_make_mask_globals_isolation_from_module_global(self):
        import random

        from decoy_engine.expressions import make_mask_globals, safe_eval

        # Pollute module-global random state.
        random.seed(999)
        for _ in range(100):
            random.random()
        # The factory's rng must NOT inherit from module-global state.
        # Running the same seed twice should produce byte-identical output
        # regardless of what module-global random looks like.
        rng_a = random.Random(42)
        scope_a = make_mask_globals(rng_a)
        val_a = safe_eval("randint(1, 100)", scope_a, {})
        random.seed(12345)  # more pollution
        rng_b = random.Random(42)
        scope_b = make_mask_globals(rng_b)
        val_b = safe_eval("randint(1, 100)", scope_b, {})
        assert val_a == val_b

    def test_make_mask_globals_preserves_non_rng_bindings(self):
        import random

        from decoy_engine.expressions import make_mask_globals, safe_eval

        rng = random.Random(42)
        scope = make_mask_globals(rng)
        # Non-RNG bindings still present.
        assert safe_eval("len('abc')", scope, {}) == 3
        assert safe_eval("abs(-5)", scope, {}) == 5
        assert safe_eval("re.search(r'\\d+', 'a42').group()", scope, {}) == "42"
