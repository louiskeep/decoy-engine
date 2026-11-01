def read_it(req):
    with open(req.locator) as fh:
        return fh.read()
