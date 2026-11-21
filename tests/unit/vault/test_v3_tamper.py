"""AT-V3 to AT-V8b, V17: every change to a v3 file is refused, and a wrong key is not "tampered"."""

from __future__ import annotations

from pathlib import Path

import pytest
from nacl import bindings

from decoy_engine.vault import VaultError
from tests.unit.vault._support import KEY, OTHER_KEY, ROWS, forge, join, read, split, written


def _refuse(blob: bytes, tmp_path: Path, key: bytes = KEY) -> VaultError:
    with pytest.raises(VaultError) as info:
        read(blob, tmp_path, key)
    return info.value


def _flip_digit(text: str) -> str:
    return ("0" if text[0] != "0" else "1") + text[1:]


def test_header_edit_is_tampered(tmp_path: Path) -> None:
    blob = written()
    assert read(blob, tmp_path)[0]  # non-vacuity: the unedited file reads
    header, segments = split(blob)
    for field, value in (("ambiguous_dropped", 5), ("entry_count", len(ROWS) + 1)):
        assert _refuse(join({**header, field: value}, segments), tmp_path).code == "vault_tampered"
    flipped = _flip_digit(header["stream_header"])
    assert _refuse(join({**header, "stream_header": flipped}, segments), tmp_path).code == (
        "vault_tampered"
    )
    assert _refuse(join({**header, "segment_rows": 7}, segments), tmp_path).code == "vault_tampered"


@pytest.fixture
def three_segments(monkeypatch: pytest.MonkeyPatch) -> bytes:
    from decoy_engine.vault import _format

    monkeypatch.setattr(_format, "_SEGMENT_ROWS", 2)
    blob = written()
    assert len(split(blob)[1]) == 3
    return blob


def test_segment_order(three_segments: bytes, tmp_path: Path) -> None:
    assert read(three_segments, tmp_path)[0] == {(n, m): v for n, m, v in ROWS}
    header, s = split(three_segments)
    cells = {
        "swap": [s[1], s[0], s[2]],
        "drop": [s[0], s[2]],
        "repeat": [s[0], s[0], s[1], s[2]],
    }
    for name, segments in cells.items():
        assert _refuse(join(header, segments), tmp_path).code == "vault_tampered", name


def test_truncation(three_segments: bytes, tmp_path: Path) -> None:
    assert read(three_segments, tmp_path)[0] == {(n, m): v for n, m, v in ROWS}
    header, s = split(three_segments)
    assert _refuse(join(header, s[:-1]), tmp_path).code == "vault_tampered"
    assert _refuse(three_segments[:-5], tmp_path).code == "vault_tampered"
    assert _refuse(three_segments + b"\x00", tmp_path).code == "vault_tampered"
    assert _refuse(join(header, [*s, s[-1]]), tmp_path).code == "vault_tampered"


def test_final_tag(tmp_path: Path) -> None:
    def seg(n: int) -> list[tuple[str, str, str]]:
        return [("ns", f"m{n}", f"s{n}")]

    cells = {
        "no final": forge([seg(0), seg(1), seg(2)], ["message"] * 3),
        "final in the middle": forge([seg(0), seg(1), seg(2)], ["message", "final", "final"]),
        "push tag": forge([seg(0)], ["push"]),
        "rekey tag": forge([seg(0)], ["rekey"]),
    }
    for name, blob in cells.items():
        assert _refuse(blob, tmp_path).code == "vault_tampered", name
    ok = forge([seg(0), seg(1)], ["message", "final"])
    assert read(ok, tmp_path)[0] == {("ns", "m0"): "s0", ("ns", "m1"): "s1"}


def test_a_non_message_tag_before_the_final_segment_is_tampered(tmp_path: Path) -> None:
    """The tag check is per segment: a REKEY or PUSH segment ahead of a valid final one is refused
    even when the count and the final tag are right (the one-segment cells above fail on the
    missing final tag alone)."""
    first, second = [("ns", "a", "1")], [("ns", "b", "2")]
    for tag in ("rekey", "push"):
        blob = forge([first, second], [tag, "final"])
        assert _refuse(blob, tmp_path).code == "vault_tampered", tag


def test_a_segment_repeating_a_row_is_tampered(tmp_path: Path) -> None:
    """The writer folds duplicates, so a repeated row is corruption, not a short count."""
    row = ("ns", "a", "1")
    across = forge([[row], [row]], ["message", "final"], entry_count=1)
    within = forge([[row, row]], ["final"], entry_count=1)
    for name, blob in {"across segments": across, "within a segment": within}.items():
        assert _refuse(blob, tmp_path).code == "vault_tampered", name


def test_a_segment_over_segment_rows_is_tampered(tmp_path: Path) -> None:
    rows = [("ns", "a", "1"), ("ns", "b", "2")]
    blob = forge([rows], ["final"], segment_rows=1)
    assert _refuse(blob, tmp_path).code == "vault_tampered"
    assert read(forge([rows], ["final"], segment_rows=2), tmp_path)[0] == {
        ("ns", "a"): "1",
        ("ns", "b"): "2",
    }


def test_entry_count_mismatch(tmp_path: Path) -> None:
    rows = [[("ns", "a", "1"), ("ns", "b", "2")]]
    assert _refuse(forge(rows, ["final"], entry_count=3), tmp_path).code == "vault_tampered"
    assert _refuse(forge(rows, ["final"], entry_count=1), tmp_path).code == "vault_tampered"


def test_empty_vault(tmp_path: Path) -> None:
    blob = written([])
    header, segments = split(blob)
    assert len(segments) == 1
    assert read(blob, tmp_path) == ({}, 0)
    assert _refuse(join(header, []), tmp_path).code == "vault_tampered"


def _spy_on_pull(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = bindings.crypto_secretstream_xchacha20poly1305_init_pull

    def spy(*args: object) -> object:
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(bindings, "crypto_secretstream_xchacha20poly1305_init_pull", spy)
    return calls


def test_wrong_key_is_key_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _spy_on_pull(monkeypatch)
    error = _refuse(written(), tmp_path, OTHER_KEY)
    assert error.code == "vault_key_mismatch"
    assert "'v1'" in error.message
    assert calls == []


def test_kcv_edit_is_key_mismatch(tmp_path: Path) -> None:
    header, segments = split(written())
    blob = join({**header, "key_check": _flip_digit(header["key_check"])}, segments)
    error = _refuse(blob, tmp_path)
    assert error.code == "vault_key_mismatch"
    assert "key_check" in error.message
    assert "vault_id" in error.message


def test_vault_id_edit_is_key_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _spy_on_pull(monkeypatch)
    header, segments = split(written())
    blob = join({**header, "vault_id": _flip_digit(header["vault_id"])}, segments)
    assert _refuse(blob, tmp_path).code == "vault_key_mismatch"
    assert calls == []


def test_no_values_in_errors(three_segments: bytes, tmp_path: Path) -> None:
    header, s = split(three_segments)
    refusals = [
        _refuse(join({**header, "ambiguous_dropped": 9}, s), tmp_path),
        _refuse(join(header, [s[1], s[0], s[2]]), tmp_path),
        _refuse(three_segments[:-3], tmp_path),
        _refuse(three_segments, tmp_path, OTHER_KEY),
        _refuse(join({**header, "key_check": _flip_digit(header["key_check"])}, s), tmp_path),
        _refuse(b"DCYVAULT3\n\x00\x00\x00\x02{}", tmp_path),
    ]
    forbidden = [row[2] for row in ROWS] + [row[1] for row in ROWS] + [header["key_check"]]
    forbidden += [KEY.hex(), repr(KEY)]
    for error in refusals:
        text = str(error)
        assert not any(needle in text for needle in forbidden), error.code
        assert error.__cause__ is None
        assert error.__context__ is None
