"""AT-V1, V2, V9, V10, V12, V20, V21, R1: the `decoy-vault/v3` format, writer and header rules."""

from __future__ import annotations

import ast
import hashlib
import re
import sys
from pathlib import Path

import pytest

from decoy_engine import reasons
from decoy_engine._canonical import canonical_json_bytes
from decoy_engine.determinism import derive
from decoy_engine.secret_providers._errors import SecretClosedError
from decoy_engine.vault import (
    VAULT_REFUSAL_CODES,
    VaultError,
    VaultWriter,
    _format,
    load_vault,
    open_vault,
)
from tests.unit.vault._support import KEY, ROWS, join, prepared, read, split, vault_writer, written

FIXTURES = Path(__file__).parent / "fixtures"


def _noise(seed: int, length: int) -> str:
    """Text that Parquet cannot shrink, so a segment's size follows its row count."""
    out, block = "", hashlib.sha256(str(seed).encode()).hexdigest()
    while len(out) < length:
        block = hashlib.sha256(block.encode()).hexdigest()
        out += block
    return out[:length]


def _code(blob: bytes, tmp_path: Path) -> str:
    with pytest.raises(VaultError) as info:
        read(blob, tmp_path)
    return info.value.code


def test_round_trip_and_header(tmp_path: Path) -> None:
    rows = [*ROWS, ("email_ns", "masked-1", "SRC-OTHER")]  # one ambiguous pair, dropped
    writer = vault_writer(KEY)
    writer.add(rows)
    path = tmp_path / "v.vault"
    writer.write(path)
    opened = open_vault(path, prepared(KEY))
    assert opened.ambiguous_dropped == 1
    assert opened.mapping == {(n, m): s for n, m, s in ROWS if m != "masked-1"}
    header = opened.header
    assert header.format == "decoy-vault/v3"
    assert header.key_version == "v1"
    assert re.fullmatch(r"[0-9a-f]{64}", header.vault_id)
    assert header.entry_count == len(opened.mapping)
    assert path.read_bytes().startswith(b"DCYVAULT3\n")


_BAD_HEADERS = {
    "extra_key": lambda h: {**h, "extra": 1},
    "missing_key": lambda h: {k: v for k, v in h.items() if k != "entry_count"},
    "bool_ambiguous": lambda h: {**h, "ambiguous_dropped": True},
    "str_ambiguous": lambda h: {**h, "ambiguous_dropped": "5"},
    "bool_entry_count": lambda h: {**h, "entry_count": True},
    "str_entry_count": lambda h: {**h, "entry_count": "5"},
    "bool_segment_rows": lambda h: {**h, "segment_rows": True},
    "str_segment_rows": lambda h: {**h, "segment_rows": "5"},
    "zero_segment_rows": lambda h: {**h, "segment_rows": 0},
    "huge_segment_rows": lambda h: {**h, "segment_rows": 1_048_577},
    "upper_vault_id": lambda h: {**h, "vault_id": h["vault_id"].upper()},
    "short_key_check": lambda h: {**h, "key_check": h["key_check"][:15]},
    "short_stream_header": lambda h: {**h, "stream_header": h["stream_header"][:47]},
    "newline_vault_id": lambda h: {**h, "vault_id": h["vault_id"] + "\n"},
    "bad_key_version": lambda h: {**h, "key_version": "bad label"},
}


@pytest.mark.parametrize("name", sorted(_BAD_HEADERS))
def test_header_closed_set(name: str, tmp_path: Path) -> None:
    header, segments = split(written())
    assert _code(join(_BAD_HEADERS[name](header), segments), tmp_path) == "vault_unreadable"


def test_bool_protocol_version_is_a_version_mismatch(tmp_path: Path) -> None:
    header, segments = split(written())
    blob = join({**header, "seed_protocol_version": True}, segments)
    assert _code(blob, tmp_path) == "vault_protocol_version_mismatch"


def test_other_format_value_is_unsupported(tmp_path: Path) -> None:
    header, segments = split(written())
    assert _code(join({**header, "format": "decoy-vault/v4"}, segments), tmp_path) == (
        "vault_format_unsupported"
    )


def test_duplicate_key_header_is_unreadable(tmp_path: Path) -> None:
    header, segments = split(written())
    body = canonical_json_bytes(header)[:-1] + b',"entry_count":5}'
    blob = _format._MAGIC + _format._frame(body) + b"".join(_format._frame(s) for s in segments)
    assert _code(blob, tmp_path) == "vault_unreadable"


def test_oversized_and_non_json_headers_are_unreadable(tmp_path: Path) -> None:
    big = _format._MAGIC + _format._frame(b"{" + b" " * 4097 + b"}")
    junk = _format._MAGIC + _format._frame(b"not json")
    assert _code(big, tmp_path) == "vault_unreadable"
    assert _code(junk, tmp_path) == "vault_unreadable"


@pytest.mark.parametrize(
    ("blob", "code"),
    [
        (b"DCYVAULT4\n" + b"\x00" * 20, "vault_format_unsupported"),
        (b"DCYVAULT\n" + b"\x00" * 20, "vault_unreadable"),
    ],
    ids=["synthetic-v4", "no-number"],
)
def test_other_magic_refused_with_remedy(blob: bytes, code: str, tmp_path: Path) -> None:
    with pytest.raises(VaultError) as info:
        read(blob, tmp_path)
    assert info.value.code == code
    if code == "vault_format_unsupported":
        assert "re-run" in info.value.message.lower()
        assert "--vault" in info.value.message


def test_per_vault_key(tmp_path: Path) -> None:
    first, second = written(), written()
    one, segs_one = split(first)
    two, segs_two = split(second)
    assert one["vault_id"] != two["vault_id"]
    assert one["key_check"] != two["key_check"]
    id_one, id_two = bytes.fromhex(one["vault_id"]), bytes.fromhex(two["vault_id"])
    assert derive(KEY, "vault", _format._stream_key_context(id_one)) != derive(
        KEY, "vault", _format._stream_key_context(id_two)
    )
    spliced = join(one, [segs_two[0], *segs_one[1:]] if len(segs_one) > 1 else [segs_two[0]])
    assert _code(spliced, tmp_path) == "vault_tampered"


def test_writer_takes_prepared_secret() -> None:
    with pytest.raises(TypeError):
        VaultWriter("not a key")  # type: ignore[arg-type]
    closed = prepared(KEY)
    closed.close()
    with pytest.raises(SecretClosedError):
        VaultWriter(closed)
    for label in ("bad label", "v1\n"):
        with pytest.raises(ValueError, match="key version"):
            VaultWriter(prepared(KEY, key_version=label))
    caller = prepared(KEY)
    writer = VaultWriter(caller)
    writer.add(ROWS)
    caller.close()
    blob, _ = writer.serialize()  # the writer owns its clone
    assert split(blob)[0]["entry_count"] == len(ROWS)
    writer.close()
    with pytest.raises(SecretClosedError):
        writer.serialize()


def test_writer_refuses_a_weak_prepared_key() -> None:
    with pytest.raises(VaultError) as info:
        VaultWriter(prepared(b"\x01" * 8))
    assert info.value.code == "weak_mask_secret"


def test_vault_id_per_serialize() -> None:
    writer = vault_writer(KEY)
    writer.add(ROWS)
    one, _ = writer.serialize()
    two, _ = writer.serialize()
    header_one, segs_one = split(one)
    header_two, segs_two = split(two)
    assert header_one["vault_id"] != header_two["vault_id"]
    assert segs_one[0] != segs_two[0]


def test_segment_byte_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_format, "_SEGMENT_MAX_BYTES", 4096)
    rows = [("ns", f"masked-{i:04d}", _noise(i, 300)) for i in range(30)]
    blob = written(rows)
    _, segments = split(blob)
    assert len(segments) > 1  # the window was halved until it fit
    mapping, _ = read(blob, tmp_path)
    assert mapping == {(n, m): s for n, m, s in rows}
    with pytest.raises(VaultError) as info:
        written([("ns", "huge", _noise(0, 20_000))])
    assert info.value.code == "vault_entry_too_large"


def test_oversized_frame_is_tampered_without_a_pull(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nacl import bindings

    header, segments = split(written())
    monkeypatch.setattr(_format, "_SEGMENT_MAX_BYTES", 4096)
    calls: list[int] = []
    real = bindings.crypto_secretstream_xchacha20poly1305_pull

    def spy(*args: object) -> object:
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(bindings, "crypto_secretstream_xchacha20poly1305_pull", spy)
    blob = join(header, [segments[0] + b"\x00" * (4096 + 17)])
    assert _code(blob, tmp_path) == "vault_tampered"
    assert calls == []


def test_load_vault_still_returns_the_tuple(tmp_path: Path) -> None:
    mapping, ambiguous = read(written(), tmp_path)
    assert (mapping, ambiguous) == ({(n, m): s for n, m, s in ROWS}, 0)
    assert load_vault is not None


def test_open_vault_refuses_a_non_prepared_key(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        open_vault(tmp_path / "x", KEY)


# ---- reason codes (AT-R1) ----------------------------------------------------------------------


def test_new_reasons_are_registered_unreleased() -> None:
    for name in ("VAULT_TAMPERED", "VAULT_ENTRY_TOO_LARGE"):
        code = getattr(reasons.runtime, name)
        assert reasons.get("runtime", code).since == reasons.UNRELEASED


def test_refusal_codes_match_what_the_package_raises() -> None:
    package = Path(sys.modules[VaultWriter.__module__].__file__).parent
    raised: set[str] = set()
    for source in package.glob("_*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and ast.unparse(node.value) == "reasons.runtime":
                name = node.attr
                if name.startswith("VAULT_") or name == "WEAK_MASK_SECRET":
                    raised.add(getattr(reasons.runtime, name))
    assert raised == set(VAULT_REFUSAL_CODES)


def test_ad_is_the_hash_of_magic_and_header_frame() -> None:
    blob = written()
    header_end = len(_format._MAGIC) + 4 + int.from_bytes(blob[10:14], "big")
    expected = hashlib.sha256(blob[:header_end]).digest()
    header, segments = split(blob)
    # The first segment authenticates under exactly this digest and under no other.
    for ad, ok in ((expected, True), (hashlib.sha256(blob[: header_end - 1]).digest(), False)):
        state = _format._open_pull(
            lambda: KEY, bytes.fromhex(header["vault_id"]), bytes.fromhex(header["stream_header"])
        )
        pulled = _format._pull_segment(state, segments[0], ad)
        assert (pulled is not None) is ok


_NO_NACL_SCRIPT = """
import json
import sys

class _BlockNacl:
    def find_spec(self, name, path=None, target=None):
        if name == "nacl" or name.startswith("nacl."):
            raise ModuleNotFoundError("No module named 'nacl' (blocked for test)")
        return None

sys.meta_path.insert(0, _BlockNacl())
for mod in list(sys.modules):
    if mod == "nacl" or mod.startswith("nacl."):
        del sys.modules[mod]

from decoy_engine.vault import VaultError, load_vault
from tests.unit.vault._support import KEY, vault_writer

codes = {}
try:
    load_vault(sys.argv[1], KEY)
except VaultError as exc:
    codes["load"] = [exc.code, "PyNaCl" in str(exc)]
writer = vault_writer(KEY)
writer.add([("ns", "m", "s")])
try:
    writer.serialize()
except VaultError as exc:
    codes["serialize"] = [exc.code, "PyNaCl" in str(exc)]
print(json.dumps(codes))
"""


def test_missing_pynacl_refuses_load_and_serialize(tmp_path: Path) -> None:
    """With `nacl` hidden, both directions raise `vault_crypto_not_installed` naming PyNaCl, and
    neither gets as far as reading the file or writing a vault."""
    import json
    import subprocess

    path = tmp_path / "v.vault"
    path.write_bytes(written())
    repo_root = Path(__file__).resolve().parents[3]
    proc = subprocess.run(  # noqa: S603 -- args are test literals
        [sys.executable, "-c", _NO_NACL_SCRIPT, str(path)],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo_root,
    )
    expected = [reasons.runtime.VAULT_CRYPTO_NOT_INSTALLED, True]
    assert json.loads(proc.stdout.strip()) == {"load": expected, "serialize": expected}
