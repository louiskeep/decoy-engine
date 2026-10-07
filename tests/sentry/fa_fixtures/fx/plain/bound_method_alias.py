from pathlib import Path


def f(cfg):
    p = Path(cfg["path"])
    m = p.read_bytes
    return m()
