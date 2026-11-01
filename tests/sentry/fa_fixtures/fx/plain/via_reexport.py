from fx.pkg_reexport import read_table


def f(cfg):
    return read_table(cfg["path"])
