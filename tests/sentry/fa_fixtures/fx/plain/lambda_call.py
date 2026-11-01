import os


def f(cfg):
    return (lambda p: os.stat(p))(cfg["path"])
