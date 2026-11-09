import os


def _repair(name: str) -> None:
    os.chmod(name, 0o700)
