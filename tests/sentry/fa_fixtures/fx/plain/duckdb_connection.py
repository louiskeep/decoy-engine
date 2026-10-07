import duckdb


def f():
    return duckdb.connect(":memory:")
