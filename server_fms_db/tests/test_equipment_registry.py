from __future__ import annotations

from api_server.services.unity_realtime import JOINT_KEYS, STATUS_KEYS
from shared.config import Settings
from shared.equipment_registry import EquipmentConfig, get_equipment_registry


def test_robot_equipment_registry_preserves_current_telemetry_wire_ids_and_topics() -> None:
    registry = get_equipment_registry(Settings())

    assert registry["fr5"].robot_id == "fr5"
    assert registry["fr5"].joint_state_topic == "/fr5/joint_states"
    assert registry["zkbot1"].robot_id == "zkbot1"
    assert registry["zkbot1"].joint_state_topic == "/zkbot1/joint_states"
    assert registry["zkbot1"].status_topic == "/zkbot1/status"
    assert registry["zkbot1"].ready_topic == "/zkbot1/ready"
    assert registry["zkbot2"].robot_id == "zkbot2"
    assert registry["zkbot2"].joint_state_topic == "/zkbot2/joint_states"
    assert registry["zkbot2"].status_topic == "/zkbot2/status"
    assert registry["zkbot2"].ready_topic == "/zkbot2/ready"


def test_turtlebot_static_identity_interfaces_and_environment_backed_override() -> None:
    registry = get_equipment_registry(Settings(turtlebot_robot_id="turtlebot_demo"))
    turtlebot = registry["turtlebot_demo"]

    assert turtlebot.robot_id == "turtlebot_demo"
    assert turtlebot.robot_type == "TURTLEBOT"
    assert turtlebot.role == "MATERIAL_TRANSPORT"
    assert turtlebot.execute_transport_action == "/forklift/execute_transport"
    assert turtlebot.return_home_action == "/forklift/return_home"
    assert turtlebot.frame_id == "map"


def test_registry_has_no_runtime_state_fields_and_unity_keys_use_the_same_ids() -> None:
    assert set(EquipmentConfig.__dataclass_fields__).isdisjoint({"connected", "ready", "busy", "positions", "pose"})
    assert JOINT_KEYS == (
        "telemetry:robot:fr5:joint_state",
        "telemetry:robot:zkbot1:joint_state",
        "telemetry:robot:zkbot2:joint_state",
    )
    assert STATUS_KEYS == (
        "telemetry:robot:fr5:status",
        "telemetry:robot:zkbot1:status",
        "telemetry:robot:zkbot1:ready",
        "telemetry:robot:zkbot2:status",
        "telemetry:robot:zkbot2:ready",
    )
