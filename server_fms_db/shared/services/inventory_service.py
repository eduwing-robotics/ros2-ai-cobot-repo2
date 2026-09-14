"""Transactional inventory operations.

This module intentionally has no FastAPI, FMS, ROS, or HTTP dependency.  It keeps the
current stock projection (``inventory``) and the immutable reason history
(``inventory_movements``) in the same SQLAlchemy transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, joinedload

from shared.models.factory import Inventory, InventoryMovement, MovementType, Part, ProductionJob


class InventoryServiceError(RuntimeError):
    """Base error for domain-level inventory operations."""


class InvalidInventoryInputError(InventoryServiceError):
    """Raised when a quantity, target identifier, or reason is invalid."""


class PartNotFoundError(InventoryServiceError):
    """Raised when the requested part does not exist."""


class JobNotFoundError(InventoryServiceError):
    """Raised when an optional movement job reference does not exist."""


class InsufficientInventoryError(InventoryServiceError):
    """Raised before an OUT movement would make current inventory negative."""

    def __init__(self, *, part_code: str, requested_quantity: int, available_quantity: int) -> None:
        self.part_code = part_code
        self.requested_quantity = requested_quantity
        self.available_quantity = available_quantity
        super().__init__(
            f"Insufficient inventory for part_code={part_code}: "
            f"requested={requested_quantity}, available={available_quantity}."
        )


_Result = TypeVar("_Result")


class InventoryService:
    """Manage stock projection and IN/OUT movement history with one transaction per write.

    The caller provides a SQLAlchemy session.  ``stock_in`` and ``stock_out`` own their
    transaction boundary: they commit on success and rollback on every exception.
    Read methods do not commit or mutate session state.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """Read-only query-session access for domain projections."""
        return self._session

    def get_all_inventory(self) -> list[Inventory]:
        """Return existing inventory rows with their associated parts loaded."""

        statement = (
            select(Inventory)
            .options(joinedload(Inventory.part))
            .order_by(Inventory.part_code)
        )
        return list(self._session.scalars(statement))



    def get_inventory_by_part_code(self, part_code: str) -> Inventory | None:
        normalized_code = self._normalize_part_code(part_code)
        part = self._session.scalar(select(Part).where(Part.part_code == normalized_code))
        if part is None:
            raise PartNotFoundError(f"Part not found: part_code={normalized_code!r}.")
        return self._session.get(Inventory, part.part_code)

    def get_inventory_by_identifier(self, identifier: str) -> tuple[Part, Inventory | None] | None:
        """Resolve one authoritative part by exact code, then one exact display name.

        This is a read-only lookup for user-facing inventory queries. Duplicate
        display names intentionally resolve to ``None`` rather than selecting an
        arbitrary material. A known part with no inventory projection is still
        returned so callers can truthfully report zero stock.
        """
        normalized = self._normalize_part_code(identifier)
        part = self._session.scalar(select(Part).where(Part.part_code == normalized))
        if part is None:
            matches = list(
                self._session.scalars(
                    select(Part).where(Part.part_name == normalized).limit(2)
                )
            )
            if len(matches) != 1:
                return None
            part = matches[0]
        return part, self._session.get(Inventory, part.part_code)

    def stock_in(
        self,
        *,
        quantity: int,
        reason: str,
        part_code: str,
        job_id: int | None = None,
    ) -> Inventory:
        """Increase stock and persist an IN movement in the same transaction."""

        return self._change_stock(
            movement_type=MovementType.IN,
            quantity=quantity,
            reason=reason,
            part_code=part_code,
            job_id=job_id,
        )

    def stock_out(
        self,
        *,
        quantity: int,
        reason: str,
        part_code: str,
        job_id: int | None = None,
    ) -> Inventory:
        """Decrease stock without allowing a negative quantity, then persist an OUT movement."""

        return self._change_stock(
            movement_type=MovementType.OUT,
            quantity=quantity,
            reason=reason,
            part_code=part_code,
            job_id=job_id,
        )

    def adjust(self, *args: object, **kwargs: object) -> None:
        """Reserve ADJUST until its quantity semantics are agreed by the team."""

        raise NotImplementedError(
            "ADJUST is not implemented because its quantity semantics are not yet defined."
        )

    def _change_stock(
        self,
        *,
        movement_type: MovementType,
        quantity: int,
        reason: str,
        part_code: str,
        job_id: int | None,
    ) -> Inventory:
        self._validate_quantity(quantity)
        normalized_reason = self._normalize_reason(reason)


        def operation() -> Inventory:
            # Locking Part also serializes the first stock-in case where no Inventory row
            # exists yet. PostgreSQL honors FOR UPDATE; SQLite test databases ignore it.
            part = self._get_part_for_update(part_code=part_code)
            self._require_job(job_id)
            inventory = self._get_inventory_for_update(part.part_code)

            if movement_type is MovementType.IN:
                if inventory is None:
                    inventory = Inventory(part_code=part.part_code, quantity=quantity)
                    self._session.add(inventory)
                else:
                    inventory.quantity += quantity
            else:
                available_quantity = inventory.available_quantity if inventory is not None else 0
                if inventory is None or available_quantity < quantity:
                    raise InsufficientInventoryError(
                        part_code=part.part_code,
                        requested_quantity=quantity,
                        available_quantity=available_quantity,
                    )
                inventory.quantity -= quantity

            # Flush the projection before constructing the audit row.  If movement creation
            # or its insert fails, the surrounding transaction still rolls this flush back.
            self._session.flush()
            self._create_movement(
                part_code=part.part_code,
                job_id=job_id,
                movement_type=movement_type,
                quantity=quantity,
                reason=normalized_reason,
            )
            self._session.flush()
            return inventory

        return self._run_write_transaction(operation)

    def _run_write_transaction(self, operation: Callable[[], _Result]) -> _Result:
        try:
            result = operation()
            self._session.commit()
            return result
        except Exception:
            self._session.rollback()
            raise

    def _get_part_for_update(self, *, part_code: str) -> Part:
        statement = select(Part).where(Part.part_code == self._normalize_part_code(part_code))
        part = self._session.scalar(statement.with_for_update())
        if part is None:
            raise PartNotFoundError(f"Part not found: part_code={part_code!r}.")
        return part

    def _get_inventory_for_update(self, part_code: str) -> Inventory | None:
        statement = select(Inventory).where(Inventory.part_code == part_code).with_for_update()
        return self._session.scalar(statement)

    def _require_job(self, job_id: int | None) -> None:
        if job_id is None:
            return
        if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
            raise InvalidInventoryInputError("job_id must be a positive integer when provided.")
        if self._session.get(ProductionJob, job_id) is None:
            raise JobNotFoundError(f"Production job not found: job_id={job_id}.")

    def _create_movement(
        self,
        *,
        part_code: str,
        job_id: int | None,
        movement_type: MovementType,
        quantity: int,
        reason: str,
    ) -> InventoryMovement:
        movement = InventoryMovement(
            part_code=part_code,
            job_id=job_id,
            movement_type=movement_type,
            quantity=quantity,
            reason=reason,
        )
        self._session.add(movement)
        return movement

    @staticmethod
    def _validate_quantity(quantity: int) -> None:
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise InvalidInventoryInputError("quantity must be a positive integer.")

    @staticmethod




    @staticmethod
    def _normalize_part_code(part_code: str | None) -> str:
        if not isinstance(part_code, str) or not (normalized := part_code.strip()):
            raise InvalidInventoryInputError("part_code must be a non-empty string.")
        return normalized

    @staticmethod
    def _normalize_reason(reason: str) -> str:
        if not isinstance(reason, str) or not (normalized := reason.strip()):
            raise InvalidInventoryInputError("reason must be a non-empty string.")
        return normalized
