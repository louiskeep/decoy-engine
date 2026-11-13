"""Seeded bare scratch file reads (F25 plan 4.1 "Scratch file reads"), and a module that connects
and is handed a connection without ever naming the native wrapper (plan 4.1 membership)."""

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution.out_of_core._duckdb import connect_duckdb


def connect_site_unlisted(arena):
    conn = connect_duckdb(temp_dir=arena)
    return conn.execute("SELECT 1")


def _load(conn):
    return conn.execute("SELECT 2")


def read_metadata_bare(relation):
    return pq.read_metadata(relation.path).num_rows


def read_table_bare(path):
    return pq.read_table(path)


def ipc_open_stream_bare(source):
    return pa.ipc.open_stream(source)


def osfile_bare(path):
    return pa.OSFile(path, "rb")


def get_batch_bare(source, index):
    reader = pa.ipc.open_file(source)
    return reader.get_batch(index)
