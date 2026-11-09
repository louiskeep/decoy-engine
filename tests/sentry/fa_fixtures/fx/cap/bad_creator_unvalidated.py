import os


def make_unvalidated(parent_fd: int, name: str) -> int:
    return os.open(name, os.O_RDONLY, dir_fd=parent_fd)
