"""Opt-in Domain 91 ROS publisher → Gateway → Redis integration."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

if os.getenv("RUN_ROS2_INTEGRATION") != "1" or os.getenv("RUN_REDIS_INTEGRATION") != "1":
    pytest.skip("Set RUN_ROS2_INTEGRATION=1 and RUN_REDIS_INTEGRATION=1 to run this test.", allow_module_level=True)
if os.getenv("ROS_DOMAIN_ID") != "91":
    pytest.skip("Telemetry ROS integration requires isolated ROS_DOMAIN_ID=91.", allow_module_level=True)

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import JointState

from shared.realtime.redis_client import create_realtime_redis_client
from telemetry_gateway.ros_subscriber import RosTelemetrySubscriber
from telemetry_gateway.telemetry import LatestValueTelemetryHandoff, RedisTelemetrySink


@pytest.mark.redis_integration
def test_domain91_fr5_joint_state_latest_value_reaches_redis_at_most_20hz() -> None:
    async def run() -> None:
        suffix = uuid.uuid4().hex
        key_namespace = f"smart_factory:test:telemetry-ros:{suffix}"
        channel_prefix = f"smart_factory.test.{suffix}."
        client = create_realtime_redis_client()
        sink = RedisTelemetrySink(client, key_namespace=key_namespace, channel_prefix=channel_prefix)
        handoff = LatestValueTelemetryHandoff(sink)
        rclpy.init(args=None)
        publisher_node = Node("test_telemetry_fr5_publisher")
        publisher = publisher_node.create_publisher(
            JointState,
            "/fr5/joint_states",
            QoSProfile(
                depth=10,
                reliability=ReliabilityPolicy.BEST_EFFORT,
                durability=DurabilityPolicy.VOLATILE,
                history=HistoryPolicy.KEEP_LAST,
            ),
        )
        subscriber = RosTelemetrySubscriber(loop=asyncio.get_running_loop(), offer_update=handoff.offer)
        key = f"{key_namespace}:robot:fr5:joint_state"
        channel = f"{channel_prefix}telemetry.robot_joint_state"
        pubsub = client.raw_client.pubsub()
        try:
            await pubsub.subscribe(channel)
            await pubsub.get_message(timeout=1.0)
            handoff.start()
            subscriber.start()
            await asyncio.sleep(0.1)
            for value in range(10):
                message = JointState()
                message.name = ["fr5_joint_1"]
                message.position = [float(value)]
                publisher.publish(message)
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            latest = await client.get_json(key)
            assert latest is not None
            assert latest["robot_id"] == "fr5"
            assert latest["positions"] == [9.0]
            received = []
            while True:
                pubsub_message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05)
                if pubsub_message is None:
                    break
                received.append(pubsub_message)
            # A 10-sample burst over 0.1 seconds is bounded to 20Hz output,
            # while the final Redis state remains the latest (position 9).
            assert 1 <= len(received) <= 3
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            subscriber.stop()
            publisher_node.destroy_node()
            await handoff.stop()
            await client.delete(key)
            await client.close()
            if rclpy.ok():
                rclpy.shutdown()

    asyncio.run(run())
