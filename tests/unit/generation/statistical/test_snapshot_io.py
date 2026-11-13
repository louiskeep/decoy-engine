"""DUP-DEFECTS E1 (A5): one snapshot parse helper with a closed refusal for undecodable bytes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from decoy_engine import reasons
from decoy_engine.generation.statistical import StatisticalSpecError, parse_snapshot_json

UNREADABLE = reasons.plan.STATISTICAL_SNAPSHOT_UNREADABLE
BAD_BYTES = b'{"a": "\xff"}'
TRUNCATED = b'{"a": '
LEAKS = ("\xff", "0xff", "ÿ", "position", "codec")


def _refusal(raw: bytes) -> StatisticalSpecError:
    with pytest.raises(StatisticalSpecError) as caught:
        parse_snapshot_json(raw, shown="s.json")
    return caught.value


def test_at_e1_1_non_utf8_bytes_get_the_unreadable_code() -> None:
    assert _refusal(BAD_BYTES).code == UNREADABLE


@pytest.mark.parametrize("raw", [BAD_BYTES, TRUNCATED])
def test_at_e1_3_message_names_the_path_and_leaks_nothing(raw: bytes) -> None:
    text = str(_refusal(raw))
    assert "s.json" in text
    if raw is BAD_BYTES:
        assert not [leak for leak in LEAKS if leak in text]


@pytest.mark.parametrize("raw", [BAD_BYTES, TRUNCATED])
def test_at_e1_4_no_exception_chain_carries_file_content(raw: bytes) -> None:
    err = _refusal(raw)
    assert err.__cause__ is None
    assert err.__context__ is None


def test_at_e1_5_truncated_json_keeps_its_code_and_message_text() -> None:
    err = _refusal(TRUNCATED)
    assert err.code == UNREADABLE
    assert "snapshot_file 's.json' could not be read: Expecting value" in str(err)


def test_valid_json_parses() -> None:
    assert parse_snapshot_json(b'{"a": 1}', shown="s.json") == {"a": 1}


def test_at_e1_7_cold_import_cycle_and_public_export() -> None:
    code = (
        "from decoy_engine.generation.statistical import "
        "StatisticalSpecError, parse_snapshot_json\n"
        "try:\n"
        "    parse_snapshot_json(b'\\xff', shown='s.json')\n"
        "except StatisticalSpecError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert done.returncode == 0, done.stderr
