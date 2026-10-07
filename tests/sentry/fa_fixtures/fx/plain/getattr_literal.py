import os


def f(cfg):
    return os.stat(cfg["path"])
