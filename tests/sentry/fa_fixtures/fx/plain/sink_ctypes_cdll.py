import ctypes


def f(p):
    return ctypes.CDLL(p)
