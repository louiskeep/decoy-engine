from pathlib import Path


def f(cfg):
    p = Path(cfg["path"])
    return p.stat()
