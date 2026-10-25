import pyarrow.parquet as pq


def read_it(path):
    return pq.read_table(path)
