import pyarrow.parquet as pq


def read_it(self):
    return pq.read_table(self._path)
