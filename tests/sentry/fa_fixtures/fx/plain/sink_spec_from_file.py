import importlib.util


def f(p):
    return importlib.util.spec_from_file_location("m", p)
