import os


def validate_component(component, key):
    return component


class Txn:
    def __init__(self, dir_fd: int):
        self._fd = dir_fd

    def create_file(self, component: str):
        validate_component(component, "k")
        return os.open(component, os.O_CREAT, dir_fd=self._fd)

    def peek(self):
        st = os.fstat(self._fd)
        for entry in os.scandir(self._fd):
            pass
        os.close(self._fd)
        return st

    def publish(self, state):
        os.replace(state.stage_name, "leaf", src_dir_fd=self._fd, dst_dir_fd=self._fd)
