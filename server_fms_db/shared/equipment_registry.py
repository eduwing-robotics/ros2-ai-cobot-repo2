"""Static robot/equipment identities and ROS interface names.

This registry deliberately excludes live telemetry, production state, and
physical pose data. ROS messages and PostgreSQL remain authoritative for those.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class EquipmentConfig:
    robot_id: str
    robot_type: str
    role: str
    joint_state_topic: str | None = None
    status_topic: str | None = None
    ready_topic: str | None = None
    execute_transport_action: str | None = None
    return_home_action: str | None = None
    frame_id: str | None = None
    mobile_robot_pose_topic: str | None = None


def get_equipment_registry(settings: Settings | None = None) -> dict[str, EquipmentConfig]:
    """Return immutable static interfaces keyed by canonical backend robot ID."""
    resolved = settings or get_settings()
    return {
        "fr5": EquipmentConfig(
            robot_id="fr5", robot_type="FR5", role="ASSEMBLY",
            joint_state_topic="/fr5/joint_states",
        ),
        "zkbot1": EquipmentConfig(
            robot_id="zkbot1", robot_type="ZKBOT", role="MATERIAL_FEED",
            joint_state_topic="/zkbot1/joint_states", status_topic="/zkbot1/status", ready_topic="/zkbot1/ready",
        ),
        "zkbot2": EquipmentConfig(
            robot_id="zkbot2", robot_type="ZKBOT", role="ASSEMBLY_SUPPORT",
            joint_state_topic="/zkbot2/joint_states", status_topic="/zkbot2/status", ready_topic="/zkbot2/ready",
        ),
        resolved.turtlebot_robot_id: EquipmentConfig(
            robot_id=resolved.turtlebot_robot_id, robot_type="TURTLEBOT", role="MATERIAL_TRANSPORT",
            execute_transport_action=resolved.forklift_execute_transport_action_name,
            return_home_action=resolved.forklift_return_home_action_name,
            frame_id=resolved.turtlebot_frame_id,
            mobile_robot_pose_topic=resolved.forklift_mobile_robot_pose_topic,
        ),
    }


def equipment(robot_id: str, settings: Settings | None = None) -> EquipmentConfig:
    return get_equipment_registry(settings)[robot_id]
