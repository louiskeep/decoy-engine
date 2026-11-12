"""Seeded scratch_native membership shapes (F25 plan 4.1): the derived rule sees each.

The wrapper is imported under another name and a local function is named `scratch_native`, so
only resolution, never the bare name, makes a call wrapped.
"""

from typing import Any

import pyarrow.parquet as pq

from decoy_engine.execution.out_of_core._duckdb import connect_duckdb
from decoy_engine.fileaccess import scratch_native as wrap
from decoy_engine.fileaccess import scratch_native_iter as wrap_iter
from fx.plain.scratch_read_evasions import _load
from fx.plain.scratch_release_evasions import native_wrapped


def scratch_native(fn, *args):
    return fn(*args)


def hands_a_connection(arena):
    conn = wrap(connect_duckdb, temp_dir=arena)
    native_wrapped(conn)
    return _load(conn)


class ClassAttributeConnection:
    def __init__(self, arena):
        self._conn = wrap(connect_duckdb, temp_dir=arena)

    def run(self):
        return self._conn.execute("SELECT 3")


class MethodReturnsConnection:
    def __init__(self, arena):
        self._arena = arena

    def _ensure_conn(self):
        return wrap(connect_duckdb, temp_dir=self._arena)

    def query(self):
        conn = self._ensure_conn()
        return conn.execute("SELECT 4")

    def handed(self):
        return _returned_helper(self._ensure_conn())


def _returned_helper(conn):
    return conn.fetchall()


class AttrFromLocalConnection:
    def open(self, arena):
        conn = wrap(connect_duckdb, temp_dir=arena)
        self._conn = conn

    def drop(self, name):
        return self._conn.unregister(name)


def annotated_assignment_connection(arena):
    conn: Any = wrap(connect_duckdb, temp_dir=arena)
    return conn.execute("SELECT 5")


def by_getattr_and_bound_method(arena):
    conn = wrap(connect_duckdb, temp_dir=arena)
    getattr(conn, "sql")("SELECT 6")
    fetch = conn.fetchall
    return fetch()


def wrapper_resolved_not_by_name(arena):
    conn = wrap(connect_duckdb, temp_dir=arena)
    return scratch_native(conn.execute, "SELECT 7")


# -- lazy reads (plan 4.1 "Lazy reads") ---------------------------------------------------


def to_arrow_reader_bare(arena):
    conn = wrap(connect_duckdb, temp_dir=arena)
    for batch in wrap(conn.execute, "SELECT 8").to_arrow_reader(10):
        yield batch


def reader_for_loop(arena):
    conn = wrap(connect_duckdb, temp_dir=arena)
    result = wrap(conn.execute, "SELECT 9")
    reader = wrap(result.to_arrow_reader, 10)
    for batch in reader:
        yield batch


def yield_from_reader(arena):
    conn = wrap(connect_duckdb, temp_dir=arena)
    result = wrap(conn.execute, "SELECT 10")
    reader = wrap(result.to_arrow_reader, 10)
    yield from reader


def next_on_reader(arena):
    conn = wrap(connect_duckdb, temp_dir=arena)
    result = wrap(conn.execute, "SELECT 11")
    reader = wrap(result.to_arrow_reader, 10)
    return next(reader)


def read_next_batch(arena):
    conn = wrap(connect_duckdb, temp_dir=arena)
    result = wrap(conn.execute, "SELECT 12")
    reader = wrap(result.to_arrow_reader, 10)
    return reader.read_next_batch()


def bare_connect_duckdb(arena):
    return connect_duckdb(temp_dir=arena)


def iter_wrapped_passes(arena):
    conn = wrap(connect_duckdb, temp_dir=arena)
    result = wrap(conn.execute, "SELECT 13")
    reader = wrap(result.to_arrow_reader, 10)
    for batch in wrap_iter(reader):
        yield batch


# -- scratch reads that pass (plan 4.1 "Scratch file reads"); this module is a member, so in scope


def wrapped_passes(path):
    return wrap(pq.read_metadata, path).num_rows


def reader_access_passes(arena, read):
    return read(arena.reader(), "relation.parquet")


def read_parquet_in_sql_passes(arena, path):
    conn = wrap(connect_duckdb, temp_dir=arena)
    return wrap(conn.execute, f"SELECT count(*) FROM read_parquet('{path}')")
