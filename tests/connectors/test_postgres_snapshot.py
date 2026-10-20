"""Postgres snapshot primitive against a throwaway Postgres (docker).

Suites: GATE 1 snapshot consistency, GUARANTEE 2 determinism, GATE 5 type
fidelity, failure/cleanup, leak. Needs docker (or DECOY_TEST_PG_DSN); skips
cleanly without it.
"""

from __future__ import annotations

import hashlib
import time
import traceback
from decimal import Decimal

import pytest

pytest.importorskip("duckdb")
pq = pytest.importorskip("pyarrow.parquet")
pa = pytest.importorskip("pyarrow")


from decoy_engine.connectors import postgres_snapshot as ps  # noqa: E402
from decoy_engine.connectors.postgres_snapshot import (  # noqa: E402
    PostgresSnapshotError,
    RelationSelection,
    snapshot_tables_to_parquet,
)

pytestmark = pytest.mark.pg_integration


def _sel(pg, table, columns, order_by=()):
    return RelationSelection(
        schema=pg.schema, table=table, columns=list(columns), order_by=list(order_by)
    )


def _read(path, col):
    return pq.read_table(path).column(col).to_pylist()


def _fk_tables(pg, n):
    s = pg.schema
    pg.run(
        f"CREATE TABLE {s}.parent(id int PRIMARY KEY, v text);"
        f"CREATE TABLE {s}.child(id int PRIMARY KEY, pid int REFERENCES {s}.parent(id));"
        f"INSERT INTO {s}.parent SELECT g, 'p' || g FROM generate_series(1, {n}) g;"
        f"INSERT INTO {s}.child SELECT g, g FROM generate_series(1, {n}) g;"
    )


def _mutating_export(pg, after_first, monkeypatch):
    """Wrap the per-relation export so the source changes between exports."""
    real = ps._export_relation
    calls = {"n": 0}

    def wrapper(conn, sel, target, rgs):
        real(conn, sel, target, rgs)
        calls["n"] += 1
        if calls["n"] == 1:
            after_first()

    monkeypatch.setattr(ps, "_export_relation", wrapper)
    return calls


# ----- GATE 1: snapshot consistency ----------------------------------------


@pytest.mark.parametrize("n", [1_000, 400_000])
def test_gate1_mutation_between_exports_yields_complete_before_state(
    pg, make_config, tmp_path, monkeypatch, n
):
    """Barrier: a writer commits parent+child (plus an update and a delete)
    between the two exports. The staged set must equal the full before-state
    in BOTH relations. 400k rows makes the scanner use its parallel ctid
    workers under the shipped threads=4 config."""
    _fk_tables(pg, n)
    s = pg.schema

    def mutate():
        pg.run(
            "BEGIN;"
            f"INSERT INTO {s}.parent VALUES ({n + 1}, 'new');"
            f"INSERT INTO {s}.child VALUES ({n + 1}, {n + 1});"
            f"UPDATE {s}.parent SET v = 'changed' WHERE id = 1;"
            f"DELETE FROM {s}.child WHERE id = {n};"
            "COMMIT;"
        )

    calls = _mutating_export(pg, mutate, monkeypatch)
    out = snapshot_tables_to_parquet(
        make_config(),
        [_sel(pg, "parent", ["id", "v"], ["id"]), _sel(pg, "child", ["id", "pid"], ["id"])],
        tmp_path / "dest",
    )
    assert calls["n"] == 2
    parent = pq.read_table(out[f"{s}.parent"]).to_pydict()
    child = pq.read_table(out[f"{s}.child"]).to_pydict()
    assert parent["id"] == list(range(1, n + 1))
    assert parent["v"] == [f"p{i}" for i in range(1, n + 1)]
    assert child["id"] == list(range(1, n + 1))
    assert child["pid"] == list(range(1, n + 1))


def test_gate1_control_per_table_calls_do_tear(pg, make_config, tmp_path, monkeypatch):
    """Negative control: the same mutation across two SEPARATE snapshots is
    torn. This proves the barrier test above can detect a torn set."""
    _fk_tables(pg, 1_000)
    s = pg.schema
    cfg = make_config()
    first = snapshot_tables_to_parquet(cfg, [_sel(pg, "parent", ["id"], ["id"])], tmp_path / "a")
    pg.run(
        f"BEGIN; INSERT INTO {s}.parent VALUES (1001, 'x'); INSERT INTO {s}.child VALUES (1001, 1001); COMMIT;"
    )
    second = snapshot_tables_to_parquet(
        cfg, [_sel(pg, "child", ["id", "pid"], ["id"])], tmp_path / "b"
    )
    parents = set(_read(first[f"{s}.parent"], "id"))
    orphans = [p for p in _read(second[f"{s}.child"], "pid") if p not in parents]
    assert orphans == [1001]


def test_gate1_mutation_before_first_read_is_whole_after_state(pg, make_config, tmp_path):
    _fk_tables(pg, 100)
    s = pg.schema
    pg.run(
        f"BEGIN; INSERT INTO {s}.parent VALUES (101, 'n'); INSERT INTO {s}.child VALUES (101, 101); COMMIT;"
    )
    out = snapshot_tables_to_parquet(
        make_config(),
        [_sel(pg, "parent", ["id"], ["id"]), _sel(pg, "child", ["id"], ["id"])],
        tmp_path / "d",
    )
    assert _read(out[f"{s}.parent"], "id") == list(range(1, 102))
    assert _read(out[f"{s}.child"], "id") == list(range(1, 102))


def test_gate1_single_relation_is_the_same_function(pg, make_config, tmp_path):
    _fk_tables(pg, 10)
    out = snapshot_tables_to_parquet(
        make_config(), [_sel(pg, "parent", ["id"], ["id"])], tmp_path / "d"
    )
    assert list(out) == [f"{pg.schema}.parent"]
    assert out[f"{pg.schema}.parent"].parent.parent == tmp_path / "d"


# ----- GUARANTEE 2: deterministic snapshot ----------------------------------


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_guarantee2_same_state_gives_byte_identical_parquet(pg, make_config, tmp_path):
    _fk_tables(pg, 50_000)
    sels = [_sel(pg, "parent", ["id", "v"], ["id"]), _sel(pg, "child", ["id", "pid"], ["id"])]
    a = snapshot_tables_to_parquet(make_config(), sels, tmp_path / "a")
    b = snapshot_tables_to_parquet(make_config(), sels, tmp_path / "b")
    for k in a:
        assert _sha(a[k]) == _sha(b[k])


def test_guarantee2_keyless_relation_is_totally_ordered(pg, make_config, tmp_path):
    """Keyless table with duplicate rows, then physically reshuffled by
    rewriting tuples. Keyless rule: tiebreak on every projected column."""
    s = pg.schema
    pg.run(
        f"CREATE TABLE {s}.k(a int, b text);"
        f"INSERT INTO {s}.k SELECT (g * 7919) % 101, 'v' || ((g * 31) % 13) FROM generate_series(1, 20000) g;"
    )
    sel = _sel(pg, "k", ["a", "b"])  # no order_by
    a = snapshot_tables_to_parquet(make_config(), [sel], tmp_path / "a")
    pg.run(f"UPDATE {s}.k SET a = a; VACUUM FULL {s}.k;".replace("; VACUUM FULL " + s + ".k;", ";"))
    b = snapshot_tables_to_parquet(make_config(), [sel], tmp_path / "b")
    ta = pq.read_table(a[f"{s}.k"]).to_pydict()
    tb = pq.read_table(b[f"{s}.k"]).to_pydict()
    assert ta == tb
    rows = list(zip(ta["a"], ta["b"], strict=True))
    assert rows == sorted(rows)
    assert _sha(a[f"{s}.k"]) == _sha(b[f"{s}.k"])


def test_guarantee2_nonunique_key_ties_are_broken_deterministically(pg, make_config, tmp_path):
    s = pg.schema
    pg.run(
        f"CREATE TABLE {s}.t(g int, x text);"
        f"INSERT INTO {s}.t SELECT g % 5, 'x' || ((g * 17) % 1000) FROM generate_series(1, 5000) g;"
    )
    sel = _sel(pg, "t", ["g", "x"], ["g"])
    a = snapshot_tables_to_parquet(make_config(), [sel], tmp_path / "a")
    pg.run(f"UPDATE {s}.t SET g = g;")
    b = snapshot_tables_to_parquet(make_config(), [sel], tmp_path / "b")
    assert _sha(a[f"{s}.t"]) == _sha(b[f"{s}.t"])


# ----- GATE 5: type fidelity matrix ----------------------------------------

# (case id, pg type, literal, expected python value after Arrow round trip)
_PRESERVED = [
    ("decimal_small", "numeric(10,2)", "12345.67", Decimal("12345.67")),
    (
        "decimal_38",
        "numeric(38,10)",
        "1234567890123456789012345678.1234567891",
        Decimal("1234567890123456789012345678.1234567891"),
    ),
    ("uuid", "uuid", "'123e4567-e89b-12d3-a456-426614174000'", None),
    ("int_array", "int[]", "'{1,2,NULL,3}'", [1, 2, None, 3]),
    ("text_array_2d", "text[][]", "'{{a,b},{c,d}}'", [["a", "b"], ["c", "d"]]),
    ("json", "json", "'{\"a\":1}'", '{"a":1}'),
    ("jsonb", "jsonb", "'{\"a\": [1, 2]}'", '{"a": [1, 2]}'),
    ("enum", "{s}.mood", "'ok'", "ok"),
    ("enum_array", "{s}.mood[]", "'{sad,happy}'", ["sad", "happy"]),
    ("bigint_min", "bigint", "-9223372036854775808", -(2**63)),
    ("bigint_max", "bigint", "9223372036854775807", 2**63 - 1),
    ("smallint_min", "smallint", "-32768", -32768),
    ("bytea", "bytea", "'\\x00ff'", b"\x00\xff"),
    ("bool", "boolean", "true", True),
    ("float_inf", "float8", "'Infinity'", float("inf")),
    ("unicode_text", "text", "'héllo ☃'", "héllo ☃"),
    ("real", "real", "1.5", 1.5),
]

_REJECTED = [
    (
        "numeric_unbounded",
        "numeric",
        "3.14159265358979323846264338327950288",
        "unsupported_column_type",
    ),
    ("numeric_60_digits", "numeric(60,5)", "1" + "0" * 50 + ".12345", "unsupported_column_type"),
    ("numeric_array", "numeric(10,2)[]", "'{1.5}'", "unsupported_column_type"),
    ("numeric_nan", "numeric(10,2)", "'NaN'", "value_not_representable"),
    ("interval", "interval", "'1 year 2 mons 3 days 04:05:06.789'", "unsupported_column_type"),
    ("money", "money", "'1.23'", "unsupported_column_type"),
    ("inet", "inet", "'10.0.0.1'", "unsupported_column_type"),
    ("char_n_padding", "char(5)", "'ab'", "unsupported_column_type"),
    ("bit", "bit(4)", "B'1010'", "unsupported_column_type"),
    ("timetz", "time with time zone", "'12:00:00+05'", "unsupported_column_type"),
    ("timestamp_far", "timestamp", "'294276-12-31 23:59:59'", "value_not_representable"),
    ("timestamp_infinity", "timestamp", "'infinity'", "value_not_representable"),
    ("timestamptz_neg_infinity", "timestamptz", "'-infinity'", "value_not_representable"),
    ("date_infinity", "date", "'infinity'", "value_not_representable"),
    ("timestamptz_array", "timestamptz[]", "'{2024-01-01 00:00:00+00}'", "unsupported_column_type"),
]

_TEMPORAL_PRESERVED = [
    ("date_far", "date", "'5874897-12-31'", (5874897, 12, 31)),
    ("date_bc", "date", "'4713-01-01 BC'", None),
    ("ts_bc", "timestamp", "'4713-01-01 BC'", None),
    ("ts_micro", "timestamp", "'2024-02-29 12:34:56.123456'", None),
    ("tstz_instant", "timestamptz", "'2024-01-01 00:00:00+05'", None),
    ("time", "time", "'12:34:56.789'", None),
]


def _one_col(pg, make_config, tmp_path, case, ty, literal):
    s = pg.schema
    pg.run(f"CREATE TYPE {s}.mood AS ENUM ('sad','ok','happy')")
    pg.run(
        f"CREATE TABLE {s}.x(id int, v {ty.format(s=s)}); INSERT INTO {s}.x VALUES (1, {literal})"
    )
    return snapshot_tables_to_parquet(
        make_config(), [_sel(pg, "x", ["id", "v"], ["id"])], tmp_path / case
    )[f"{s}.x"]


@pytest.mark.parametrize(
    ("case", "ty", "literal", "expected"), _PRESERVED, ids=[c[0] for c in _PRESERVED]
)
def test_gate5_preserved(pg, make_config, tmp_path, case, ty, literal, expected):
    path = _one_col(pg, make_config, tmp_path, case, ty, literal)
    got = pq.read_table(path).column("v")[0].as_py()
    if case == "uuid":
        assert str(got) == "123e4567-e89b-12d3-a456-426614174000"
    elif case == "real":
        assert got == pytest.approx(1.5)
    else:
        assert got == expected
    if case == "decimal_38":
        assert pq.read_table(path).schema.field("v").type == pa.decimal128(38, 10)


@pytest.mark.parametrize(
    ("case", "ty", "literal", "code"), _REJECTED, ids=[c[0] for c in _REJECTED]
)
def test_gate5_rejected_with_coded_error(pg, make_config, tmp_path, case, ty, literal, code):
    with pytest.raises(PostgresSnapshotError) as ei:
        _one_col(pg, make_config, tmp_path, case, ty, literal)
    assert ei.value.code == code
    assert ei.value.location == f"{pg.schema}.x.v"
    assert literal not in str(ei.value)
    assert not (tmp_path / case).exists() or not any((tmp_path / case).iterdir())


@pytest.mark.parametrize(
    ("case", "ty", "literal", "_"), _TEMPORAL_PRESERVED, ids=[c[0] for c in _TEMPORAL_PRESERVED]
)
def test_gate5_temporal_preserved_exactly(pg, make_config, tmp_path, case, ty, literal, _):
    """Compare Arrow's integer storage with Postgres' own epoch arithmetic."""
    s = pg.schema
    path = _one_col(pg, make_config, tmp_path, case, ty, literal)
    col = pq.read_table(path).column("v").combine_chunks()
    raw = (
        col.cast(pa.int32() if case.startswith("date") else pa.int64())
        if not case == "time"  # noqa: SIM201
        else None
    )
    c = pg._c
    if case.startswith("date"):
        oracle = c.execute(
            "SELECT * FROM postgres_query('admin', ?)",
            [f"SELECT (v - date '1970-01-01') FROM {s}.x"],
        ).fetchone()[0]
        assert raw[0].as_py() == oracle
    elif case in ("ts_bc", "ts_micro"):
        oracle = c.execute(
            "SELECT * FROM postgres_query('admin', ?)",
            [f"SELECT (extract(epoch FROM v) * 1000000)::numeric::bigint FROM {s}.x"],
        ).fetchone()[0]
        assert raw[0].as_py() == int(oracle)
    elif case == "tstz_instant":
        assert raw[0].as_py() == 1704049200 * 1_000_000 - 0 or raw[0].as_py() == 1704049200000000
    else:
        assert str(col[0].as_py()) == "12:34:56.789000"


def test_gate5_null_is_preserved_in_every_supported_class(pg, make_config, tmp_path):
    s = pg.schema
    pg.run(
        f"CREATE TABLE {s}.n(id int, a numeric(10,2), b date, c timestamp, d text, e jsonb);"
        f"INSERT INTO {s}.n VALUES (1, NULL, NULL, NULL, NULL, NULL);"
    )
    out = snapshot_tables_to_parquet(
        make_config(), [_sel(pg, "n", ["id", "a", "b", "c", "d", "e"], ["id"])], tmp_path / "n"
    )
    row = pq.read_table(out[f"{s}.n"]).to_pylist()[0]
    assert row == {"id": 1, "a": None, "b": None, "c": None, "d": None, "e": None}


def test_missing_relation_and_column_are_coded(pg, make_config, tmp_path):
    s = pg.schema
    pg.run(f"CREATE TABLE {s}.t(a int)")
    with pytest.raises(PostgresSnapshotError) as e1:
        snapshot_tables_to_parquet(make_config(), [_sel(pg, "nope", ["a"])], tmp_path / "d1")
    assert e1.value.code == "relation_not_found"
    with pytest.raises(PostgresSnapshotError) as e2:
        snapshot_tables_to_parquet(make_config(), [_sel(pg, "t", ["zz"])], tmp_path / "d2")
    assert e2.value.code == "column_not_found"
    assert e2.value.location == f"{s}.t.zz"


# ----- failure injection / atomic publish ----------------------------------


def _tree(p):
    return sorted(x for x in p.rglob("*")) if p.exists() else []


def test_second_relation_failure_publishes_nothing(pg, make_config, tmp_path):
    s = pg.schema
    pg.run(f"CREATE TABLE {s}.ok(a int); INSERT INTO {s}.ok VALUES (1)")
    cfg = make_config()
    with pytest.raises(PostgresSnapshotError) as ei:
        snapshot_tables_to_parquet(
            cfg, [_sel(pg, "ok", ["a"]), _sel(pg, "missing", ["a"])], tmp_path / "dest"
        )
    assert ei.value.code == "relation_not_found"
    assert _tree(tmp_path / "dest") == []
    assert _tree(cfg.temp_directory) == []


def test_mid_export_failure_removes_first_relations_staged_file(
    pg, make_config, tmp_path, monkeypatch
):
    s = pg.schema
    pg.run(
        f"CREATE TABLE {s}.a(x int); CREATE TABLE {s}.b(x int); INSERT INTO {s}.a VALUES (1);INSERT INTO {s}.b VALUES (1)"
    )
    real = ps._export_relation
    seen = []

    def boom(conn, sel, target, rgs):
        if seen:
            raise RuntimeError("disk full with secret-ish text")
        real(conn, sel, target, rgs)
        seen.append(target)
        assert target.exists()

    monkeypatch.setattr(ps, "_export_relation", boom)
    cfg = make_config()
    with pytest.raises(PostgresSnapshotError) as ei:
        snapshot_tables_to_parquet(
            cfg, [_sel(pg, "a", ["x"]), _sel(pg, "b", ["x"])], tmp_path / "dest"
        )
    assert ei.value.code == "export_failed"
    assert ei.value.cause_type == "RuntimeError"
    assert "secret-ish" not in str(ei.value)
    assert _tree(tmp_path / "dest") == []
    assert _tree(cfg.temp_directory) == []


def test_deadline_aborts_a_long_export_and_cleans_up(pg, make_config, tmp_path):
    s = pg.schema
    pg.run(
        f"CREATE TABLE {s}.big(a bigint, b text);"
        f"INSERT INTO {s}.big SELECT g, md5(g::text) FROM generate_series(1, 6000000) g;"
    )
    cfg = make_config(deadline_s=1, memory_limit="128MB", threads=1)
    t0 = time.monotonic()
    with pytest.raises(PostgresSnapshotError) as ei:
        snapshot_tables_to_parquet(cfg, [_sel(pg, "big", ["b", "a"], ["b"])], tmp_path / "dest")
    assert ei.value.code == "deadline_exceeded"
    assert time.monotonic() - t0 < 60
    assert _tree(tmp_path / "dest") == []
    assert _tree(cfg.temp_directory) == []


def test_success_leaves_only_the_published_directory_and_no_spill(pg, make_config, tmp_path):
    _fk_tables(pg, 100)
    cfg = make_config()
    out = snapshot_tables_to_parquet(cfg, [_sel(pg, "parent", ["id"], ["id"])], tmp_path / "dest")
    children = list((tmp_path / "dest").iterdir())
    assert len(children) == 1 and children[0].name.startswith("snapshot-")
    assert all(p.exists() for p in out.values())
    assert _tree(cfg.temp_directory) == []


# ----- leak rule against a live database -----------------------------------


def test_errors_and_logs_never_contain_the_dsn(pg, make_config, tmp_path, caplog):
    caplog.set_level("DEBUG")
    s = pg.schema
    pg.run(f"CREATE TABLE {s}.t(a interval)")
    cfg = make_config()
    dsn = cfg.dsn.get_secret_value()
    failures = []
    for sel in (_sel(pg, "t", ["a"]), _sel(pg, "nope", ["a"])):
        with pytest.raises(PostgresSnapshotError) as ei:
            snapshot_tables_to_parquet(cfg, [sel], tmp_path / "d")
        failures.append(ei.value)
    rendered = "\n".join(
        "".join(traceback.format_exception(e)) + str(e) + repr(e) for e in failures
    ) + "\n".join(r.getMessage() for r in caplog.records)
    for part in dsn.split():
        assert part not in rendered
    assert "password" not in rendered


# ----- case-collision and array lower-bound regressions --------------------


def test_case_colliding_columns_are_rejected_not_misresolved(pg, make_config, tmp_path):
    """DuckDB resolves identifiers case-insensitively, so ("a", "A") would
    export a's value for either request. Reject, including when only one of
    the colliding columns is selected."""
    s = pg.schema
    pg.run(f'CREATE TABLE {s}.c("a" int, "A" int); INSERT INTO {s}.c VALUES (11, 22)')
    for cols in (["A"], ["a"], ["a", "A"]):
        with pytest.raises(PostgresSnapshotError) as ei:
            snapshot_tables_to_parquet(make_config(), [_sel(pg, "c", cols)], tmp_path / "d")
        assert ei.value.code == "ambiguous_name"
    assert not (tmp_path / "d").exists() or not any((tmp_path / "d").iterdir())


def test_case_colliding_unselected_column_is_rejected(pg, make_config, tmp_path):
    s = pg.schema
    pg.run(f'CREATE TABLE {s}.c2("id" int, "Id" int, "x" int); INSERT INTO {s}.c2 VALUES (1, 2, 3)')
    with pytest.raises(PostgresSnapshotError) as ei:
        snapshot_tables_to_parquet(make_config(), [_sel(pg, "c2", ["x", "id"])], tmp_path / "d")
    assert ei.value.code == "ambiguous_name"


def test_case_colliding_table_names_are_rejected(pg, make_config, tmp_path):
    s = pg.schema
    pg.run(
        f'CREATE TABLE {s}."t" (a int); CREATE TABLE {s}."T" (a int); INSERT INTO {s}."t" VALUES (1)'
    )
    with pytest.raises(PostgresSnapshotError) as ei:
        snapshot_tables_to_parquet(make_config(), [_sel(pg, "t", ["a"])], tmp_path / "d")
    assert ei.value.code == "ambiguous_name"


def test_case_differing_request_does_not_resolve_to_another_relation(pg, make_config, tmp_path):
    s = pg.schema
    pg.run(f'CREATE TABLE {s}."Mixed" (a int); INSERT INTO {s}."Mixed" VALUES (1)')
    with pytest.raises(PostgresSnapshotError) as ei:
        snapshot_tables_to_parquet(make_config(), [_sel(pg, "mixed", ["a"])], tmp_path / "d")
    assert ei.value.code == "relation_not_found"


def test_array_with_nonstandard_lower_bound_is_rejected(pg, make_config, tmp_path):
    s = pg.schema
    pg.run(
        f"CREATE TABLE {s}.lb(id int, v int[]); "
        f"INSERT INTO {s}.lb VALUES (1, '[0:2]={{10,20,30}}'::int[])"
    )
    with pytest.raises(PostgresSnapshotError) as ei:
        snapshot_tables_to_parquet(
            make_config(), [_sel(pg, "lb", ["id", "v"], ["id"])], tmp_path / "d"
        )
    assert ei.value.code == "value_not_representable"
    assert ei.value.location == f"{s}.lb.v"


def test_standard_arrays_empty_and_null_still_pass(pg, make_config, tmp_path):
    s = pg.schema
    pg.run(
        f"CREATE TABLE {s}.ok(id int, v int[]); INSERT INTO {s}.ok VALUES "
        "(1, '{1,2}'), (2, '{}'), (3, NULL)"
    )
    out = snapshot_tables_to_parquet(
        make_config(), [_sel(pg, "ok", ["id", "v"], ["id"])], tmp_path / "d"
    )
    assert _read(out[f"{s}.ok"], "v") == [[1, 2], [], None]
