def f(cfg):
    return __import__("os").stat(cfg["path"])
