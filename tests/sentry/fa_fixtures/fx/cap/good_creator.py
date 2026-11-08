import os


def validate_component(component, key):
    return component


def make_it(parent_fd: int, name: str) -> int:
    validate_component(name, "k")
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    os.chmod(name, 0o700, dir_fd=parent_fd)
    return os.open(name, os.O_RDONLY, dir_fd=parent_fd)
