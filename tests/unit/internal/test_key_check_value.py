"""AT-K1: the one key check value function."""

from __future__ import annotations

import re

import pytest

from decoy_engine.internal.crypto import (
    RESUME_KEY_CHECK_LABEL,
    VAULT_KEY_CHECK_LABEL,
    hmac_hex,
    key_check_value,
)

_KEY = bytes(range(32))


def test_matches_the_truncated_hmac_of_the_label() -> None:
    expected = hmac_hex(_KEY, RESUME_KEY_CHECK_LABEL)
    assert expected is not None
    assert key_check_value(lambda: _KEY, RESUME_KEY_CHECK_LABEL) == expected[:16]


def test_context_is_joined_with_the_unit_separator() -> None:
    expected = hmac_hex(_KEY, VAULT_KEY_CHECK_LABEL + "\x1f" + "abc")
    assert expected is not None
    assert key_check_value(lambda: _KEY, VAULT_KEY_CHECK_LABEL, context="abc") == expected[:16]


def test_is_sixteen_lowercase_hex() -> None:
    assert re.fullmatch(r"[0-9a-f]{16}", key_check_value(lambda: _KEY, VAULT_KEY_CHECK_LABEL))


def test_context_changes_the_value() -> None:
    first = key_check_value(lambda: _KEY, VAULT_KEY_CHECK_LABEL, "a")
    second = key_check_value(lambda: _KEY, VAULT_KEY_CHECK_LABEL, "b")
    assert first != second


def test_labels_are_frozen() -> None:
    assert RESUME_KEY_CHECK_LABEL == "decoy-resume-key-check/v1"
    assert VAULT_KEY_CHECK_LABEL == "decoy-vault-key-check/v1"


def test_a_missing_label_is_refused() -> None:
    with pytest.raises(ValueError, match="label is empty"):
        key_check_value(lambda: _KEY, None)  # type: ignore[arg-type]
