import pytest

from miniapp.calculator import safe_divide


def test_safe_divide_contract():
    assert safe_divide(9, 3) == 3
    with pytest.raises(ValueError, match="division by zero"):
        safe_divide(1, 0)
