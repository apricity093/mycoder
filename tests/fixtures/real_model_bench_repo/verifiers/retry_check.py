import pytest

from miniapp.retry import run_with_retries


def test_retry_budget_and_success():
    calls = []

    def eventually_succeeds():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("retry")
        return "ok"

    assert run_with_retries(eventually_succeeds, 3) == "ok"
    assert len(calls) == 3


def test_retry_rejects_invalid_budget_and_reraises_last_error():
    with pytest.raises(ValueError):
        run_with_retries(lambda: None, 0)

    calls = []

    def always_fails():
        calls.append(1)
        raise LookupError("final")

    with pytest.raises(LookupError, match="final"):
        run_with_retries(always_fails, 2)
    assert len(calls) == 2
