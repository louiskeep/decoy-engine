import os

import pyarrow.parquet as pq

HOST_PATH = os.getenv("HOST_PATH")


def f():
    return pq.read_table(HOST_PATH)
