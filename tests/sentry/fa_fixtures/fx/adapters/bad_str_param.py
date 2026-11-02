import pyarrow.parquet as pq


def read_it(path: str):
    return pq.read_table(path)
