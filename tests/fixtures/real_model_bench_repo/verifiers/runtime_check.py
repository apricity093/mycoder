from miniapp.config import DEFAULT_REQUEST_TIMEOUT
from miniapp.runtime import request_options


def test_runtime_uses_central_timeout_default():
    assert DEFAULT_REQUEST_TIMEOUT == 45
    assert request_options() == {"timeout": DEFAULT_REQUEST_TIMEOUT}
    assert request_options(timeout=7) == {"timeout": 7}


def test_runtime_does_not_duplicate_default_literal():
    import inspect
    import miniapp.runtime

    source = inspect.getsource(miniapp.runtime)
    assert "DEFAULT_REQUEST_TIMEOUT" in source
    assert "timeout\": 45" not in source
