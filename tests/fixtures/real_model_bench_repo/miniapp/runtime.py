def request_options(timeout=None):
    return {"timeout": 30 if timeout is None else timeout}
