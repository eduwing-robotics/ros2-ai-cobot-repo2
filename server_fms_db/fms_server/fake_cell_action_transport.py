"""Deterministic ExecuteTask-contract Fake transport for tests and local drills."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from time import sleep

from fms_server.cell_action_transport import (
    CellActionTransport,
    CellTaskAcceptedCallback,
    CellTaskCommand,
    CellTaskExecutionResult,
    CellTaskFeedback,
    CellTaskFeedbackCallback,
    CellTaskRawFeedback,
    CellTaskRawResult,
)


@dataclass(frozen=True, slots=True)
class FakeCellActionExchange:
    """One raw ExecuteTask Result plus optional raw Feedback observations.

    A normalized result remains accepted for a short compatibility window in tests,
    while contract-alignment coverage uses ``CellTaskRawResult`` directly.
    """

    result: CellTaskRawResult | CellTaskExecutionResult
    feedback: Sequence[CellTaskRawFeedback] = field(default_factory=tuple)
    execution_delay_seconds: float = 0.0
    # A default fake SUCCESS represents the confirmed contract: all requested
    # slot identities completed. Mismatch tests can explicitly disable this.
    complete_requested_slots: bool = True


class FakeCellActionTransport(CellActionTransport):
    """No-I/O transport exposing Goal commands and real-contract-shaped observations."""

    def __init__(self, exchanges: Sequence[FakeCellActionExchange]) -> None:
        self._exchanges = list(exchanges)
        self.commands: list[CellTaskCommand] = []

    def execute(
        self,
        command: CellTaskCommand,
        *,
        goal_accepted_callback: CellTaskAcceptedCallback | None = None,
        feedback_callback: CellTaskFeedbackCallback | None = None,
    ) -> CellTaskExecutionResult:
        self.commands.append(command)
        if not self._exchanges:
            raise AssertionError("FakeCellActionTransport received more commands than configured exchanges.")
        exchange = self._exchanges.pop(0)
        result = exchange.result.normalize() if isinstance(exchange.result, CellTaskRawResult) else exchange.result
        if result.succeeded is True and not result.completed_slots and exchange.complete_requested_slots:
            requested_parts = json.loads(command.parts_json)
            result = CellTaskExecutionResult.success(
                detail=result.detail,
                completed_slots=tuple(item["slot"] for item in requested_parts),
            )
        if result.accepted and goal_accepted_callback is not None:
            goal_accepted_callback()
        if exchange.execution_delay_seconds < 0:
            raise ValueError("FakeCellActionExchange.execution_delay_seconds must be non-negative.")
        if exchange.execution_delay_seconds:
            sleep(exchange.execution_delay_seconds)
        if feedback_callback is not None:
            for raw_feedback in exchange.feedback:
                feedback_callback(CellTaskFeedback.from_raw(command=command, raw=raw_feedback))
        return result
