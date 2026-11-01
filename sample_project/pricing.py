"""简单商品定价。可以在修复 add 之后，尝试本文件的另一个 BUG。"""


def discounted_price(price: float, discount: float) -> float:
    """返回折后价；discount 在 [0, 1] 之间，例如 0.2 代表八折。"""
    if not 0 <= discount <= 1:
        raise ValueError("discount must be in [0, 1]")
    if price < 0:
        raise ValueError("price must be non-negative")
    return round(price * (1 + discount), 2)  # BUG-2: 应减去折扣
