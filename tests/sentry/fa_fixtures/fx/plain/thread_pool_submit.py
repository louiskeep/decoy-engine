from concurrent.futures import ThreadPoolExecutor


def f(work, items):
    with ThreadPoolExecutor(max_workers=2) as pool:
        return [pool.submit(work, i) for i in items]
