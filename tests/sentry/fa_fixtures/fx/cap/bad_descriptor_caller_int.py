import os


def peek(fd: int):
    st = os.fstat(fd)
    os.close(fd)
    return st
