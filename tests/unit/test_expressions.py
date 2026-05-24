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
