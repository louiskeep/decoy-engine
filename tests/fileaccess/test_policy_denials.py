"""Engine-level regressions for the host-file read and write vectors (V1 to V9, V15, V18, V22)
under a strict policy: each is refused with the one denial, nothing outside the root is read or
written, and no locator, path, column name or file content reaches any message.
"""

from __future__ import annotations

import json
import logging
import traceback
import uuid
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.data_discovery import DiscoverySqlError, run_discovery_sql
from decoy_engine.fileaccess import FileAccessDenied, scope
from decoy_engine.generators.derivation import strategy_config_fingerprint
from decoy_engine.keyprovider import resolve_mask_secret_ref
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._generation import read_and_pin_snapshots
from decoy_engine.profile import profile_source
from decoy_engine.quarantine import write_jsonl_staged
from decoy_engine.reference_tables import load_table
from decoy_engine.transforms._codeset_loader import load_corpus
from decoy_engine.transforms.joint_mask import validate_joint_mask_config
from tests.fileaccess.support import RootRelative

TOKEN = f"tok{uuid.uuid4().hex[:12]}"
MARKER = "SENTINEL-CONTENT-" + uuid.uuid4().hex[:8]


@dataclass
class World:
    root: Path
    outside: Path
    policy: RootRelative

    def sentinel(self, name: str, data: bytes | None = None) -> Path:
        path = self.outside / name
        path.write_bytes(data if data is not None else MARKER.encode())
        return path

    def inside(self, name: str, data: bytes | None = None) -> Path:
        path = self.root / name
        path.write_bytes(data if data is not None else MARKER.encode())
        return path


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / f"outside_{TOKEN}"
    outside.mkdir()
    policy = RootRelative(root)
    try:
        yield World(root, outside, policy)
    finally:
        policy.close()


def _assert_denial(exc: BaseException, key: str) -> None:
    assert isinstance(exc, FileAccessDenied)
    assert str(exc) == f"file access denied: {key}"
    # What can cross a boundary: the message, the repr and the exception chain. The traceback's
    # source lines belong to the test itself and are not part of the message.
    text = "".join(traceback.format_exception_only(type(exc), exc)) + repr(exc) + repr(exc.args)
    for forbidden in (TOKEN, MARKER, "/tmp/"):
        assert forbidden not in text, forbidden
    assert exc.__cause__ is None and exc.__suppress_context__ and exc.__context__ is None


def _table(path: Path, **columns: list[object]) -> Path:
    pq.write_table(pa.table(columns), path)
    return path


def test_v6_corpus_outside_the_root_is_denied_and_inside_loads(world: World) -> None:
    secret = _table(world.outside / f"{TOKEN}.parquet", code=[MARKER, "B2"])
    with pytest.raises(FileAccessDenied) as info:
        load_corpus("c", secret, file_access=world.policy)
    _assert_denial(info.value, "code_set.corpus_source")
    ok = _table(world.root / "c.parquet", code=["A1", "B2"])
    assert {r["code"] for r in load_corpus("c", ok, file_access=world.policy)} == {"A1", "B2"}


def test_v7_reference_outside_the_root_is_denied_with_no_column_names(world: World) -> None:
    secret = _table(
        world.outside / f"{TOKEN}.parquet", id=pa.array([1], pa.int64()), **{f"col_{TOKEN}": ["x"]}
    )
    with pytest.raises(FileAccessDenied) as info:
        load_table(f"customer:{secret}", file_access=world.policy)
    _assert_denial(info.value, "joint_mask.reference")
    inside = _table(world.root / "r.parquet", id=pa.array([1, 2], pa.int64()), v=["a", "b"])
    assert load_table(f"customer:{inside}", file_access=world.policy).row_count == 2


def test_v8_and_v18_snapshot_outside_the_root_is_denied(world: World) -> None:
    snapshot = world.sentinel(f"{TOKEN}.json", b"{}")
    config = {
        "tables": [
            {
                "name": "t",
                "generate_columns": [
                    {"name": "c", "type": "statistical", "snapshot_file": str(snapshot)}
                ],
            }
        ]
    }
    with scope(world.policy), pytest.raises(FileAccessDenied) as info:
        read_and_pin_snapshots(config)
    _assert_denial(info.value, "tables.t.generate_columns.c.snapshot_file")
    for target in (str(snapshot), "/dev/zero", "../outside"):
        with pytest.raises(FileAccessDenied) as fp:
            strategy_config_fingerprint({"snapshot_file": target}, file_access=world.policy)
        assert str(fp.value) == "file access denied: snapshot_file"


def test_v18_fingerprint_of_an_own_snapshot_matches_the_ambient_value(world: World) -> None:
    own = world.inside("own.json", b'{"schema_version": "x"}')
    config = {"snapshot_file": str(own), "type": "faker"}
    # Ambient first: once a strict policy has been used, a no-policy call fails closed.
    ambient = strategy_config_fingerprint(config)
    strict = strategy_config_fingerprint(config, file_access=world.policy)
    assert strict == ambient


def test_v9_secret_file_outside_the_root_is_denied(world: World) -> None:
    key = world.sentinel(f"{TOKEN}.key", ("ab" * 32).encode())
    with pytest.raises(FileAccessDenied) as info:
        resolve_mask_secret_ref(f"file:{key}", file_access=world.policy)
    _assert_denial(info.value, "global_settings.mask_secret_ref")
    inside = world.inside("k.key", ("ab" * 32).encode())
    assert len(resolve_mask_secret_ref(f"file:{inside}", file_access=world.policy)) == 32


@pytest.mark.parametrize("ref", ["../../x", "/abs/x", "us_zip5_city_state/../../x", "a\0b"])
def test_v15_bare_reference_names_never_become_paths(ref: str) -> None:
    with pytest.raises(FileNotFoundError, match="no shipped reference table"):
        load_table(ref)
    with pytest.raises(PlanCompileError) as info:
        validate_joint_mask_config({"columns": ["a"], "key_by": "k", "reference": ref})
    assert info.value.code == "joint_mask_reference_not_found"


def test_v15_shipped_names_still_load() -> None:
    assert load_table("us_zip5_city_state").row_count > 0
    validate_joint_mask_config(
        {"columns": ["city"], "key_by": "k", "reference": "us_zip5_city_state"}
    )


@pytest.mark.parametrize(
    "locator",
    ["{outside}/s.parquet", "../outside_x/s.parquet", "{root}/../s.parquet", "link/s.parquet"],
)
def test_v1_to_v4_source_paths_outside_the_root_are_denied(world: World, locator: str) -> None:
    secret = _table(world.outside / "s.parquet", **{f"col_{TOKEN}": [1]})
    (world.root / "link").symlink_to(world.outside)
    path = locator.format(outside=world.outside, root=world.root)
    config = {"sources": {"t": {"type": "file", "format": "parquet", "path": path}}}
    with pytest.raises(FileAccessDenied) as info:
        profile_source(config, file_access=world.policy)
    _assert_denial(info.value, "sources.t.path")
    assert pq.read_table(secret).num_rows == 1


def test_a_missing_file_and_a_foreign_file_are_the_same_denial(world: World) -> None:
    secret = _table(world.outside / "s.parquet", a=[1])
    messages = set()
    for path in (str(secret), str(world.root / "missing.parquet")):
        config = {"sources": {"t": {"type": "file", "format": "parquet", "path": path}}}
        with pytest.raises(FileAccessDenied) as info:
            profile_source(config, file_access=world.policy)
        messages.add(str(info.value))
    assert len(messages) == 1


def test_own_source_profiles_under_the_policy(world: World) -> None:
    _table(world.root / "s.parquet", a=[1, 2, 3])
    config = {
        "sources": {
            "t": {"type": "file", "format": "parquet", "path": str(world.root / "s.parquet")}
        }
    }
    profile = profile_source(config, file_access=world.policy)
    assert profile.tables[0].row_count == 3


def test_v5_quarantine_output_is_confined_to_the_root(world: World) -> None:
    target = world.outside / f"{TOKEN}.jsonl"
    with scope(world.policy):
        with pytest.raises(FileAccessDenied) as info:
            write_jsonl_staged(str(target), [{"a": 1}])
        _assert_denial(info.value, "quarantine.output_path")
        staged = write_jsonl_staged(str(world.root / "sub" / "q.jsonl"), [{"a": 1}])
        staged.publish()
    assert not target.exists()
    assert json.loads((world.root / "sub" / "q.jsonl").read_text()) == {"a": 1}
    assert list(world.outside.iterdir()) == []


DUCKDB_CASES = [
    "SELECT * FROM read_text('{p}/sentinel.txt')",
    "SELECT * FROM read_blob('{p}/sentinel.txt')",
    "SELECT * FROM glob('{p}/*')",
    "SELECT * FROM read_csv('{p}/sentinel.txt')",
    "SELECT * FROM read_json_auto('{p}/sentinel.txt')",
    "SELECT * FROM sniff_csv('{p}/sentinel.txt')",
    "SELECT * FROM parquet_metadata('{p}/sentinel.parquet')",
    "SELECT * FROM '{p}/sentinel.parquet'",
    "SELECT * FROM t JOIN '{p}/sentinel.parquet' USING (a)",
    "SELECT * FROM t, '{p}/sentinel.parquet'",
    "COPY (SELECT 1) TO '{p}/out.csv'",
    "EXPORT DATABASE '{p}/exported'",
    "ATTACH '{p}/new.db'",
    "ATTACH '{p}/new.db' (READ_ONLY)",
    "INSTALL httpfs",
    "LOAD 'httpfs'",
    "LOAD '{p}/x.duckdb_extension'",
    "SET enable_external_access = true",
    "RESET enable_external_access",
    "SET lock_configuration = false",
    "PRAGMA database_list",
    "CALL pragma_version()",
    "CREATE SECRET s (TYPE s3)",
    "SELECT 1; COPY (SELECT 1) TO '{p}/out2.csv'",
]


@pytest.mark.parametrize("sql", DUCKDB_CASES)
def test_v22_discovery_sql_cannot_touch_a_file(world: World, sql: str) -> None:
    (world.outside / "sentinel.txt").write_text(MARKER)
    _table(world.outside / "sentinel.parquet", a=[1])
    table = _table(world.root / "t.parquet", a=[1, 2])
    before = {p.name: p.stat().st_mtime_ns for p in world.outside.iterdir()}
    with pytest.raises(Exception) as info:
        run_discovery_sql(sql.format(p=world.outside), {"t": str(table)}, file_access=world.policy)
    assert isinstance(info.value, (DiscoverySqlError, FileAccessDenied)) or "ermission" in str(
        info.value
    )
    assert MARKER not in str(info.value)
    assert {p.name: p.stat().st_mtime_ns for p in world.outside.iterdir()} == before


def test_v22_a_plain_select_over_a_registered_table_works_and_pushes_down(world: World) -> None:
    table = _table(world.root / "t.parquet", a=[1, 2, 3], b=["x", "y", "z"])
    result = run_discovery_sql(
        "SELECT sum(a) AS s FROM t WHERE a > 1", {"t": str(table)}, file_access=world.policy
    )
    assert result.rows == [{"s": 5}]


def test_v22_a_table_outside_the_root_is_not_registered(world: World) -> None:
    secret = _table(world.outside / f"{TOKEN}.parquet", a=[1])
    with pytest.raises(FileAccessDenied) as info:
        run_discovery_sql("SELECT * FROM t", {"t": str(secret)}, file_access=world.policy)
    _assert_denial(info.value, "discovery.tables.t")


def test_denials_leave_no_warning_or_log_record_with_a_locator(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    secret = _table(world.outside / f"{TOKEN}.parquet", id=pa.array([1], pa.int64()))
    with caplog.at_level(logging.DEBUG), warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(FileAccessDenied):
            load_table(f"customer:{secret}", file_access=world.policy)
        with pytest.raises(FileAccessDenied):
            load_corpus("c", secret, file_access=world.policy)
    assert TOKEN not in caplog.text
    assert not any(TOKEN in str(w.message) for w in caught)


def test_a_denial_names_the_config_position_the_read_belongs_to(world: World) -> None:
    from decoy_engine import fileaccess

    outside = Path(world.sentinel(f"{TOKEN}.parquet", b"x"))
    with fileaccess.at_position("tables.orders.columns.zip"), scope(world.policy):
        with pytest.raises(FileAccessDenied) as info:
            load_table("customer_table", outside)
    _assert_denial(info.value, "tables.orders.columns.zip.joint_mask.reference")
    with scope(world.policy), pytest.raises(FileAccessDenied) as bare:
        load_table("customer_table", outside)
    _assert_denial(bare.value, "joint_mask.reference")


def test_a_disguise_directory_needs_its_own_role(rooted, root_dir) -> None:  # type: ignore[no-untyped-def]
    from decoy_engine.disguises.loader import load_disguises

    (root_dir / "d").mkdir()
    # The fixture grants MODEL_PACK_ROOT but not DISGUISE_ROOT.
    with pytest.raises(FileAccessDenied) as info:
        load_disguises(root_dir / "d", file_access=rooted)
    assert str(info.value) == "file access denied: disguises.directory"


def test_the_joint_mask_handler_names_its_table_and_column(world: World) -> None:
    from types import SimpleNamespace

    from decoy_engine.execution._strategies._joint_mask import JointMaskHandler

    outside = world.sentinel(f"{TOKEN}.ref.parquet", b"x")
    plan = SimpleNamespace(
        provider_config=(
            ("columns", ["zip"]),
            ("reference", f"customer:{outside}"),
            ("key_by", "zip"),
        ),
        namespace="n",
    )
    ctx = SimpleNamespace(current_table="orders", nested_outer_column="", mask_key=b"k")
    with scope(world.policy), pytest.raises(FileAccessDenied) as info:
        JointMaskHandler().run(None, "zip", plan, ctx)  # type: ignore[arg-type]
    _assert_denial(info.value, "tables.orders.columns.zip.joint_mask.reference")
