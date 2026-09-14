"""Opt-in, response-transparent timing for Voice API diagnostics."""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Iterator

# Uvicorn routes this logger at INFO to stderr by default, without changing the root logger.
logger = logging.getLogger("uvicorn.error")
_current_timing: ContextVar["VoiceTiming | None"] = ContextVar("voice_timing", default=None)


@dataclass
class VoiceTiming:
    endpoint: str
    correlation_id: str | None = None
    stages_ms: dict[str, float] = field(default_factory=dict)
    total_started: float = field(default_factory=time.perf_counter)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            self.stages_ms[name] = round(self.stages_ms.get(name, 0.0) + elapsed, 3)

    def record(self, **metadata: object) -> None:
        payload = {
            "event": "VOICE_TIMING",
            "endpoint": self.endpoint,
            "correlation_id": self.correlation_id,
            "total_ms": round((time.perf_counter() - self.total_started) * 1000, 3),
            "stages_ms": self.stages_ms,
            **{key: value for key, value in metadata.items() if value is not None},
        }
        logger.info("VOICE_TIMING %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))


@contextmanager
def activate_voice_timing(timing: VoiceTiming | None) -> Iterator[None]:
    token: Token[VoiceTiming | None] | None = None
    if timing is not None:
        token = _current_timing.set(timing)
    try:
        yield
    finally:
        if token is not None:
            _current_timing.reset(token)


@contextmanager
def voice_timing_stage(name: str) -> Iterator[None]:
    timing = _current_timing.get()
    if timing is None:
        yield
        return
    with timing.stage(name):
        yield
