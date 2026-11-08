import os


def make_it(parent_fd: int, name: str) -> int:
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    os.chmod(name, 0o700, dir_fd=parent_fd)
    return os.open(name, os.O_RDONLY, dir_fd=parent_fd)
