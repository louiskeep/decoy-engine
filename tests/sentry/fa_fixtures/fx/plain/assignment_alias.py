import pyarrow.parquet as pq


def f(cfg):
    reader = pq.read_table
    return reader(cfg["path"])
