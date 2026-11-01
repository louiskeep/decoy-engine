import os


def publish(src, dst):
    os.link(src, dst)
    os.rename(src, dst)
    return list(os.scandir(dst))
