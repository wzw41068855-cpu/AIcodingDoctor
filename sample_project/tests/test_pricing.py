"""验收测试：Agent 不得修改此文件。"""
import pytest
from pricing import discounted_price


def test_discount_20_percent():
    assert discounted_price(100, 0.2) == 80.0


def test_no_discount():
    assert discounted_price(50, 0) == 50.0


def test_discount_100_percent():
    assert discounted_price(80, 1) == 0.0


def test_invalid_discount():
    with pytest.raises(ValueError):
        discounted_price(100, 1.5)
