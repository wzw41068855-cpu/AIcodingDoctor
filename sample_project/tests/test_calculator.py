"""验收测试：Agent 不得修改此文件。"""
from calculator import add, multiply


def test_add_positive():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-2, 3) == 1


def test_add_zero():
    assert add(0, 5) == 5


def test_multiply():
    assert multiply(3, 4) == 12
