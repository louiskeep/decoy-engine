import os

READERS = {"stat": os.stat, "list": os.listdir}


def f(cfg, kind):
    return READERS[kind](cfg["path"])
