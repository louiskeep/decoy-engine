import pyarrow as pa


def f(p):
    return pa.input_stream(p)
