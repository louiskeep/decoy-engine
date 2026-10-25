from concurrent.futures import ThreadPoolExecutor


def f(work, item):
    return ThreadPoolExecutor(max_workers=1).submit(work, item)
