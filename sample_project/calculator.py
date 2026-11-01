"""基础计算函数。这里有意放入 BUG，供 AI CodingDoctor 修复。"""


def add(a: int, b: int) -> int:
    """返回 a 与 b 之和。"""
    return a - b  # BUG-1: 应改为加法


def multiply(a: int, b: int) -> int:
    """返回 a 与 b 的乘积。"""
    return a * b
