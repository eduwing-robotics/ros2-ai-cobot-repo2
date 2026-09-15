from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timezone

import pytest

from fms_server.vision_status import (
    VisionStatusDatagramProtocol,
    VisionStatusHandler,
    VisionStatusStore,
    VisionStatusUdpReceiver,
)
from shared.schemas.vision import VisionStatus, VisionStatusMessage


def payload(*, status: str = "ACTIVE", **overrides: object) -> dict[str, object]:
    message: dict[str, object] = {
        "schema_version": "0.1",
        "message_type": "global_vision_status",
        "camera_id": "global_camera",
        "status": status,
        "frame_seq": 123,
        "last_frame_age_sec": 0.032,
        "inference_error_count": 0,
        "last_error": None,
        "status_stamp_sec": 0,
        "status_stamp_nanosec": 0,
    }
    message.update(overrides)
    return message


def send_to_protocol(protocol: VisionStatusDatagramProtocol, body: bytes) -> None:
    protocol.datagram_received(body, ("127.0.0.1", 34567))


def test_valid_payload_updates_latest_status_with_fms_receive_time() -> None:
    handler = VisionStatusHandler()
    protocol = VisionStatusDatagramProtocol(handler)

    send_to_protocol(protocol, json.dumps(payload()).encode("utf-8"))

    snapshot = handler.store.get("global_camera")
    assert snapshot is not None
    assert snapshot.status is VisionStatus.ACTIVE
    assert snapshot.frame_seq == 123
    assert snapshot.server_received_at.tzinfo is timezone.utc


@pytest.mark.parametrize("status", list(VisionStatus))
def test_all_declared_status_values_are_accepted(status: VisionStatus) -> None:
    handler = VisionStatusHandler()
    protocol = VisionStatusDatagramProtocol(handler)

    send_to_protocol(protocol, json.dumps(payload(status=status.value)).encode("utf-8"))

    snapshot = handler.store.get("global_camera")
    assert snapshot is not None
    assert snapshot.status is status


@pytest.mark.parametrize(
    "body",
    [
        b"{broken-json",
        json.dumps(payload(status="NOT_ALLOWED")).encode("utf-8"),
        json.dumps(payload(schema_version="0.2")).encode("utf-8"),
        json.dumps(payload()).replace('"frame_seq": 123, ', "").encode("utf-8"),
        json.dumps(payload(message_type="other_message")).encode("utf-8"),
        b"\xff\xfe",
    ],
)
def test_invalid_packets_are_dropped_without_overwriting_last_status(body: bytes, caplog: pytest.LogCaptureFixture) -> None:
    handler = VisionStatusHandler()
    protocol = VisionStatusDatagramProtocol(handler)
    handler.store.update(
        VisionStatusMessage.model_validate(payload(frame_seq=7)),
        server_received_at=datetime.now(timezone.utc),
    )

    send_to_protocol(protocol, body)

    snapshot = handler.store.get("global_camera")
    assert snapshot is not None
    assert snapshot.frame_seq == 7
    assert "Invalid vision UDP packet" in caplog.text



@pytest.mark.parametrize(
    ("invalid_message", "expected_field", "expected_reason"),
    [
        (
            {key: value for key, value in payload().items() if key != "frame_seq"},
            "frame_seq",
            "Field required",
        ),
        (
            {**payload(), "unexpected_debug_token": "do-not-put-this-in-warning"},
            "unexpected_debug_token",
            "Extra inputs are not permitted",
        ),
        (payload(status="NOT_ALLOWED"), "status", "Input should be"),
        (payload(frame_seq="not-an-integer"), "frame_seq", "Input should be a valid integer"),
    ],
)
def test_schema_validation_warning_identifies_field_and_reason_without_raw_payload(
    invalid_message: dict[str, object],
    expected_field: str,
    expected_reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = VisionStatusHandler()
    protocol = VisionStatusDatagramProtocol(handler)
    caplog.set_level("WARNING", logger="fms_server.vision_status")

    send_to_protocol(protocol, json.dumps(invalid_message).encode("utf-8"))

    assert handler.store.get("global_camera") is None
    assert "Invalid vision UDP packet" in caplog.text
    assert "127.0.0.1" in caplog.text
    assert "34567" in caplog.text
    assert f"field={expected_field}" in caplog.text
    assert expected_reason in caplog.text
    assert "do-not-put-this-in-warning" not in caplog.text

def test_store_keeps_latest_message_per_camera() -> None:
    store = VisionStatusStore()
    store.update(VisionStatusMessage.model_validate(payload(frame_seq=1)))
    store.update(VisionStatusMessage.model_validate(payload(frame_seq=2)))

    snapshot = store.get("global_camera")
    assert snapshot is not None
    assert snapshot.frame_seq == 2
    assert list(store.get_all()) == ["global_camera"]


def test_async_udp_receiver_accepts_local_datagram_and_closes() -> None:
    async def run_test() -> None:
        handler = VisionStatusHandler()
        receiver = VisionStatusUdpReceiver(host="127.0.0.1", port=0, handler=handler)
        await receiver.start()
        assert receiver.is_running
        assert receiver.bound_port is not None

        encoded = json.dumps(payload(frame_seq=999)).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(encoded, ("127.0.0.1", receiver.bound_port))

        for _ in range(20):
            if handler.store.get("global_camera") is not None:
                break
            await asyncio.sleep(0.01)

        snapshot = handler.store.get("global_camera")
        assert snapshot is not None
        assert snapshot.frame_seq == 999
        receiver.close()
        assert not receiver.is_running

    asyncio.run(run_test())
