import os


def make_other(parent_fd: int, name: str) -> None:
    os.mkdir(name, 0o700, dir_fd=parent_fd)
