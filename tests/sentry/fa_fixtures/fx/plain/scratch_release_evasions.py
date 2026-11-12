"""Seeded scratch release and native-boundary evasions (F25 S1b): the key sentry sees each."""

from decoy_engine.fileaccess import ScratchArena as Arena
from decoy_engine.fileaccess import scratch_native


def release_by_alias(arena):
    return Arena.local_path(arena, "x")


def release_by_getattr(arena):
    return getattr(arena, "local_path")("x")


def release_by_bound_method(arena):
    release = arena.local_path
    return release("x")


def native_wrapped(conn):
    return scratch_native(conn.execute, "SELECT 1")


def native_bare(conn):
    return conn.execute("SELECT 1")


def native_by_getattr(conn):
    return getattr(conn, "sql")("SELECT 1")


def native_by_bound_method(conn):
    fetch = conn.fetchall
    return fetch()
