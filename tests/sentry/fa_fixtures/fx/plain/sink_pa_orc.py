import pyarrow.orc as orc


def f(p):
    return orc.read_table(p)
