"""购物车：小计、满减券、合计。金额一律用分（整数）。"""
from dataclasses import dataclass


@dataclass
class Item:
    name: str
    price: int   # 单价（分）
    count: int


@dataclass
class Coupon:
    threshold: int   # 小计满多少分可用
    off: int         # 减多少分


def subtotal(items: list[Item]) -> int:
    return sum(item.price * item.count for item in items)


def total(items: list[Item], coupon: Coupon | None = None) -> int:
    amount = subtotal(items)
    if coupon is not None and amount > coupon.threshold:
        amount -= coupon.off
    return max(amount, 0)
