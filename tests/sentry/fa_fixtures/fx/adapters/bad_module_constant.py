import pyarrow.parquet as pq

PATH = "/etc/passwd"


def read_it(handle: "FileHandle"):
    return pq.read_table(PATH)
