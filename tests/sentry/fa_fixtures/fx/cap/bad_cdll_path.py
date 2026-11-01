import ctypes


def load(path):
    return ctypes.CDLL(path)
