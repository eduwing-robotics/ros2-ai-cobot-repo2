"""Logical TurtleBot locations resolved from immutable Delivery policy."""

from __future__ import annotations

from dataclasses import dataclass

from shared.models.factory import JobMaterialDelivery


SUPPLY_GROUP_TO_PICKUP_CODE: dict[str, str] = {
    "OUTER_WALLS": "RACK1",
    "INNER_WALL": "RACK2",
}
DROP_CODE = "DROP"


class TransportLocationResolutionError(ValueError):
    """A Delivery cannot be translated to the approved logical transport contract."""


@dataclass(frozen=True, slots=True)
class LogicalTransportLocations:
    pickup_code: str
    dropoff_code: str


def resolve_policy_transport_locations(delivery: JobMaterialDelivery) -> LogicalTransportLocations:
    group = delivery.supply_group_code.strip() if isinstance(delivery.supply_group_code, str) else ""
    pickup_code = SUPPLY_GROUP_TO_PICKUP_CODE.get(group)
    if pickup_code is None:
        raise TransportLocationResolutionError(
            f"Unsupported supply_group_code for logical TurtleBot transport: {group!r}."
        )
    destination = (
        delivery.supply_destination_code.strip()
        if isinstance(delivery.supply_destination_code, str)
        else ""
    )
    if destination != DROP_CODE:
        raise TransportLocationResolutionError(
            f"Logical TurtleBot transport requires supply_destination_code={DROP_CODE!r}; got {destination!r}."
        )
    return LogicalTransportLocations(pickup_code=pickup_code, dropoff_code=destination)


def resolve_empty_return_locations(delivery: JobMaterialDelivery) -> LogicalTransportLocations:
    """Resolve the inverse route for one completed policy Delivery.

    The forward resolver remains the single authority for supply-group-to-rack
    mapping and the approved DROP destination.  Empty-pallet cleanup simply
    reverses that already validated logical route.
    """
    forward = resolve_policy_transport_locations(delivery)
    return LogicalTransportLocations(
        pickup_code=forward.dropoff_code,
        dropoff_code=forward.pickup_code,
    )


def resolve_legacy_transport_locations(delivery: JobMaterialDelivery) -> LogicalTransportLocations:
    """Keep legacy fake lifecycle executable without leaking physical coordinates.

    Legacy rows have no supply policy/source location. Their stable delivery code is
    passed as an opaque logical pickup identity; only policy Deliveries use rack mapping.
    """
    if not delivery.delivery_code:
        raise TransportLocationResolutionError("Legacy Delivery has no logical delivery_code.")
    return LogicalTransportLocations(pickup_code=delivery.delivery_code, dropoff_code=DROP_CODE)
