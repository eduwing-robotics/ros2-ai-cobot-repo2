from types import SimpleNamespace

import pytest

from fms_server.transport_location_resolver import (
    TransportLocationResolutionError,
    resolve_empty_return_locations,
    resolve_policy_transport_locations,
)


def _delivery(group: str, destination: str = "DROP"):
    return SimpleNamespace(supply_group_code=group, supply_destination_code=destination)


def test_outer_walls_resolves_to_rack1_and_drop() -> None:
    locations = resolve_policy_transport_locations(_delivery("OUTER_WALLS"))
    assert (locations.pickup_code, locations.dropoff_code) == ("RACK1", "DROP")


def test_inner_wall_resolves_to_rack2_and_drop() -> None:
    locations = resolve_policy_transport_locations(_delivery("INNER_WALL"))
    assert (locations.pickup_code, locations.dropoff_code) == ("RACK2", "DROP")


def test_unknown_supply_group_fails_closed_without_rack_fallback() -> None:
    with pytest.raises(TransportLocationResolutionError, match="Unsupported supply_group_code"):
        resolve_policy_transport_locations(_delivery("UNSUPPORTED_GROUP"))


def test_policy_destination_must_be_the_existing_drop_code() -> None:
    with pytest.raises(TransportLocationResolutionError, match="supply_destination_code"):
        resolve_policy_transport_locations(_delivery("OUTER_WALLS", "OTHER"))


def test_outer_walls_empty_return_resolves_drop_to_rack1() -> None:
    locations = resolve_empty_return_locations(_delivery("OUTER_WALLS"))
    assert (locations.pickup_code, locations.dropoff_code) == ("DROP", "RACK1")


def test_inner_wall_empty_return_resolves_drop_to_rack2() -> None:
    locations = resolve_empty_return_locations(_delivery("INNER_WALL"))
    assert (locations.pickup_code, locations.dropoff_code) == ("DROP", "RACK2")
