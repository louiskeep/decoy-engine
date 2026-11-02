import pyarrow as pa


def f(p):
    return pa.create_memory_map(p, 10)
