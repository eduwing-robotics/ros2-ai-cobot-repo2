"""Product lookup service for translating product codes to Product entities."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import Product


class ProductLookupError(RuntimeError):
    """Base error for product lookup operations."""


class InvalidProductCodeError(ProductLookupError):
    """Raised when the product code format is invalid (e.g., empty or None)."""


class ProductNotFoundError(ProductLookupError):
    """Raised when the requested product code does not exist in the database."""


class ProductLookupService:
    """Service to look up DB Product entities from a canonical product code."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_code(self, product_code: str | None) -> Product:
        """Finds a product by its exact canonical product_code.

        Args:
            product_code: The exact DB product_code (e.g., 'HOUSE_A').

        Returns:
            The Product entity.

        Raises:
            InvalidProductCodeError: If product_code is None or empty.
            ProductNotFoundError: If the product does not exist.
        """
        if not product_code or not str(product_code).strip():
            raise InvalidProductCodeError(f"Invalid product code: {product_code!r}")

        code_str = str(product_code).strip()

        product = self._session.scalar(
            select(Product).where(Product.product_code == code_str)
        )

        if not product:
            raise ProductNotFoundError(f"Product not found for code: {code_str!r}")

        return product
