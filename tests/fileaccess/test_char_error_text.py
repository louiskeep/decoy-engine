"""E1-0 characterization: the error text of every file-reading row, with no policy.

These pin what a CLI caller sees today (relative paths, library error text included).
They must keep passing unchanged after the engine routes every open through a
`FileAccess` policy: `LocalFileAccess` is the no-policy default and may not change a
character of this text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import ConfigError
from decoy_engine.generation.statistical._spec import StatisticalSpecError, _load_snapshot
from decoy_engine.generators.derivation import _snapshot_content_digest
from decoy_engine.keyprovider import MaskSecretError, MissingMaskSecret, resolve_mask_secret_ref
from decoy_engine.profile._readers import LazySource, build_profile_source
from decoy_engine.reference_tables._loader import load_table
from decoy_engine.validation import validate_config
from decoy_engine.vault import VaultError, load_vault


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def _text(exc: BaseException) -> str:
    return str(exc)


def _raises(fn: Any) -> BaseException:
    with pytest.raises(BaseException) as info:
        fn()
    return info.value


PARQUET_MISSING = "[Errno 2] Failed to open local file 'nope.parquet'. Detail: [errno 2] No such file or directory"


@pytest.mark.parametrize("method", ["row_count", "schema"])
def test_r1_parquet_source_missing(method: str) -> None:
    source = build_profile_source({"type": "file", "format": "parquet", "path": "nope.parquet"})
    exc = _raises(getattr(source, method))
    assert isinstance(exc, FileNotFoundError)
    assert _text(exc) == PARQUET_MISSING


def test_r1_parquet_sample_missing() -> None:
    source = build_profile_source({"type": "file", "format": "parquet", "path": "nope.parquet"})
    exc = _raises(lambda: source.sample_frame(5))
    assert isinstance(exc, FileNotFoundError)
    assert _text(exc) == PARQUET_MISSING


@pytest.mark.parametrize("method", ["row_count", "schema"])
def test_r1_csv_source_missing(method: str) -> None:
    source = build_profile_source({"type": "file", "format": "csv", "path": "nope.csv"})
    exc = _raises(getattr(source, method))
    assert isinstance(exc, FileNotFoundError)
    assert _text(exc) == "[Errno 2] No such file or directory: 'nope.csv'"


def test_r1_csv_sample_and_frame_missing() -> None:
    source = build_profile_source({"type": "file", "format": "csv", "path": "nope.csv"})
    for call in (lambda: source.sample_frame(5), source.to_frame):
        exc = _raises(call)
        assert isinstance(exc, FileNotFoundError)
        assert _text(exc) == "[Errno 2] No such file or directory: 'nope.csv'"


def test_r1_fixed_width_source_missing() -> None:
    source = build_profile_source(
        {"type": "file", "format": "fixed_width", "path": "nope.fw", "layout": {"columns": []}}
    )
    exc = _raises(source.row_count)
    assert isinstance(exc, FileNotFoundError)
    assert _text(exc) == "[Errno 2] No such file or directory: 'nope.fw'"


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.num_rows,
        lambda s: s.footer_facts(),
        lambda s: s.schema,
        lambda s: s.column_null_counts(),
        lambda s: list(s.iter_batches(5)),
        lambda s: s.open_batches(5),
    ],
    ids=["num_rows", "footer_facts", "schema", "null_counts", "iter_batches", "open_batches"],
)
def test_r1_lazy_source_missing(call: Any) -> None:
    exc = _raises(lambda: call(LazySource(Path("nope.parquet"))))
    assert isinstance(exc, FileNotFoundError)
    assert _text(exc) == PARQUET_MISSING


def test_r1_lazy_source_to_table_missing_and_malformed() -> None:
    exc = _raises(LazySource(Path("nope.parquet")).to_table)
    assert isinstance(exc, FileNotFoundError)
    assert _text(exc) == "nope.parquet"
    Path("bad.parquet").write_text("x")
    exc = _raises(LazySource(Path("bad.parquet")).to_table)
    assert isinstance(exc, pa.ArrowInvalid)
    assert _text(exc) == (
        "Could not open Parquet input source 'bad.parquet': Parquet file size is 1 bytes, "
        "smaller than the minimum file footer (8 bytes)"
    )


def test_r4_reference_table_errors() -> None:
    shipped = _raises(lambda: load_table("nope"))
    assert isinstance(shipped, FileNotFoundError)
    assert _text(shipped) == (
        "no shipped reference table 'nope'. Provide a path= override, a "
        "'customer:/path/to/file.parquet' reference, or check the table name."
    )
    missing = _raises(lambda: load_table("customer:nope.parquet"))
    assert isinstance(missing, ValueError)
    assert _text(missing) == "failed to read Parquet at nope.parquet: nope.parquet"
    Path("bad.parquet").write_text("x")
    bad = _raises(lambda: load_table("customer:bad.parquet"))
    assert _text(bad).startswith("failed to read Parquet at bad.parquet: Could not open Parquet")
    pq.write_table(pa.table({"x": [1]}), "noid.parquet")
    noid = _raises(lambda: load_table("customer:noid.parquet"))
    assert _text(noid) == (
        "reference table at noid.parquet is missing required 'id' column. "
        "All reference tables must have a stable 'id' column."
    )
    pq.write_table(pa.table({"id": pa.array([1], pa.int32())}), "id32.parquet")
    id32 = _raises(lambda: load_table("customer:id32.parquet"))
    assert _text(id32) == (
        "reference table at id32.parquet has 'id' column of type DataType(int32); expected "
        "int64. All reference tables must have a stable int64 'id' column (schema convention "
        "-- see reference_tables/__init__.py)."
    )


def test_r4_shipped_names_load() -> None:
    shipped = load_table("us_zip5_city_state")
    assert shipped.row_count > 0 and "id" in shipped.column_names


def test_r6_mask_secret_file_errors() -> None:
    missing = _raises(lambda: resolve_mask_secret_ref("file:nope.key"))
    assert isinstance(missing, MissingMaskSecret)
    assert _text(missing) == (
        "missing_mask_secret: mask secret file 'nope.key' (from mask_secret_ref) could not be "
        "read: [Errno 2] No such file or directory: 'nope.key'."
    )
    Path("bin.key").write_bytes(b"\xff\xfe\x00")
    bad = _raises(lambda: resolve_mask_secret_ref("file:bin.key"))
    assert isinstance(bad, MaskSecretError)
    assert _text(bad) == (
        "bad_secret_ref: mask secret file 'bin.key' (from mask_secret_ref) is not valid UTF-8: "
        "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte."
    )
    Path("ok.key").write_text("ab" * 32)
    assert len(resolve_mask_secret_ref("file:ok.key")) == 32


def test_r10_vault_read_errors() -> None:
    missing = _raises(lambda: load_vault("nope.dcyvault", b"12345678"))
    assert isinstance(missing, VaultError)
    assert _text(missing) == (
        "[vault_unreadable] vault file 'nope.dcyvault' could not be read: "
        "[Errno 2] No such file or directory: 'nope.dcyvault'"
    )
    Path("bad.dcyvault").write_bytes(b"junk")
    bad = _raises(lambda: load_vault("bad.dcyvault", b"12345678"))
    assert (
        _text(bad)
        == "[vault_unreadable] 'bad.dcyvault' is not a decoy vault file (bad magic header)."
    )


def test_r12_config_file_errors() -> None:
    missing = _raises(lambda: validate_config("nope.yaml"))
    assert isinstance(missing, ConfigError)
    assert _text(missing) == "Config file not found: nope.yaml"
    Path("l.yaml").write_text("- a\n")
    assert _text(_raises(lambda: validate_config("l.yaml"))) == (
        "Config root must be a YAML mapping, got list"
    )
    Path("b.yaml").write_text("a: [\n")
    assert _text(_raises(lambda: validate_config("b.yaml"))).startswith("Failed to parse YAML:")


def test_r3_snapshot_read_errors() -> None:
    missing = _raises(lambda: _load_snapshot("nope.json"))
    assert isinstance(missing, StatisticalSpecError)
    assert _text(missing) == (
        "[statistical_snapshot_unreadable] snapshot_file 'nope.json' could not be read: "
        "[Errno 2] No such file or directory: 'nope.json'"
    )
    Path("s.json").write_text("{")
    bad = _raises(lambda: _load_snapshot("s.json"))
    assert _text(bad) == (
        "[statistical_snapshot_unreadable] snapshot_file 's.json' could not be read: "
        "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
    )
    digest = _raises(lambda: _snapshot_content_digest("nope.json"))
    assert isinstance(digest, RuntimeError)
    assert _text(digest).startswith(
        "strategy_config_fingerprint: snapshot_file 'nope.json' is missing or unreadable "
        "(FileNotFoundError: [Errno 2] No such file or directory: 'nope.json'); "
    )


def test_r15c_custom_providers_directory_is_forgiving(tmp_path: Path) -> None:
    from decoy_engine.internal.faker_setup import load_custom_providers

    assert load_custom_providers(Path("nodir")) == {}
    Path("afile").write_text("x")
    assert load_custom_providers(Path("afile")) == {}
    Path("cp").mkdir()
    Path("cp/colors.txt").write_text("# c\nred\n\nblue\n")
    Path("cp/sizes.json").write_text('["s", "m"]')
    Path("cp/skip.dat").write_text("zzz")
    assert load_custom_providers(Path("cp")) == {
        "custom.colors": ["red", "blue"],
        "custom.sizes": ["s", "m"],
    }
