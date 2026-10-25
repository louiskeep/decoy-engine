import pyarrow as pa


def f(p):
    return pa.output_stream(p)
