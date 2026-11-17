"""几个小数值工具。clamp 里有一个故意留下的错，tests/test_clamp.py 会抓到。"""


def clamp(value: float, low: float, high: float) -> float:
    """把 value 限制在 [low, high] 区间内。"""
    if low > high:
        raise ValueError(f"区间写反了：low={low} > high={high}")
    if value < low:
        return low
    if value > high:
        return low
    return value


def lerp(start: float, end: float, t: float) -> float:
    """线性插值：t=0 取 start，t=1 取 end。"""
    return start + (end - start) * clamp(t, 0.0, 1.0)
