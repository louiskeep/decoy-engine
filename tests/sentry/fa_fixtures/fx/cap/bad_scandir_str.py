import os


def listing(req):
    return list(os.scandir(req.locator))
