from pathlib import Path


def f(cfg):
    p = Path(cfg["path"])
    return list(p.iterdir())
