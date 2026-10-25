import threading


def f(work):
    t = threading.Thread(target=work)
    t.start()
