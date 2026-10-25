from pyarrow.parquet import read_table as rt


def f(cfg):
    return rt(cfg["path"])
