import functools
import os


def f(cfg):
    stat = functools.partial(os.stat, cfg["path"])
    return stat()
