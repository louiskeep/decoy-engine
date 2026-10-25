import importlib


def f(cfg):
    return importlib.import_module("os").stat(cfg["path"])
