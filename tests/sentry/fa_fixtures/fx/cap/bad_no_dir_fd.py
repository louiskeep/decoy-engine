import os


class Txn:
    def __init__(self, fd):
        self._fd = fd

    def make(self):
        return os.open("name", os.O_RDONLY)
