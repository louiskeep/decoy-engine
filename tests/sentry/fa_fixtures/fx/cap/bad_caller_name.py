import os


class Txn:
    def __init__(self, fd):
        self._fd = fd

    def create(self, component: str):
        return os.open(component, os.O_CREAT, dir_fd=self._fd)
