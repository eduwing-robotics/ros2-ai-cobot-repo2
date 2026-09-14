"""Temporary product aliases. Replace with PostgreSQL products/product_aliases later."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    code: str
    canonical_name: str
    aliases: tuple[str, ...]


PRODUCTS = (
    Product("HOUSE_A", "A형 초소형 주택", (
        "A형", "A 형", "A타입", "A 타입", "A형 주택", "A 타입 주택", "A형 초소형 주택",
        "초소형 주택 A형", "HOUSE A", "HOUSE_A", "에이형", "에이 타입",
    )),
    Product("HOUSE_B", "B형 초소형 주택", (
        "B형", "B 형", "B타입", "B 타입", "B형 주택", "B 타입 주택", "B형 초소형 주택",
        "초소형 주택 B형", "HOUSE B", "HOUSE_B", "비형", "비 타입",
    )),
)


def find_product(*texts: str | None) -> Product | None:
    normalized = " ".join(text for text in texts if text).lower()
    for product in PRODUCTS:
        if any(alias.lower() in normalized for alias in product.aliases):
            return product
    return None
