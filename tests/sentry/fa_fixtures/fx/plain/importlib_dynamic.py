import importlib


def f(cfg, name):
    return importlib.import_module(name).stat(cfg["path"])
