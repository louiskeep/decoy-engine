import os


def f(cfg, name):
    return getattr(os, name)(cfg["path"])
