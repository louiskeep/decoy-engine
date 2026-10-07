import os

import pyarrow.parquet as pq


def read_it(handle: "FileHandle", **kwargs):
    return pq.read_table(handle.library_path(), **kwargs)


def write_it(table, txn: "OutputTxn", component: str):
    with os.fdopen(txn.create_file(component), "wb") as fh:
        pq.write_table(table, fh)


def by_proc_path(handle: "FileHandle"):
    path = handle.library_path()
    return pq.ParquetFile(path)
