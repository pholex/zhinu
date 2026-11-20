"""库存：扣减与回补。"""


def take(stock: dict[str, int], name: str, count: int) -> None:
    if count <= 0:
        raise ValueError("数量必须为正")
    if stock.get(name, 0) < count:
        raise ValueError(f"{name} 库存不足")
    stock[name] -= count


def put_back(stock: dict[str, int], name: str, count: int) -> None:
    if count <= 0:
        raise ValueError("数量必须为正")
    stock[name] = stock.get(name, 0) + count
