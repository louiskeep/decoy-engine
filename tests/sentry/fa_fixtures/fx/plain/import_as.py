import pyarrow.parquet as x


def f(cfg):
    return x.read_table(cfg["path"])
