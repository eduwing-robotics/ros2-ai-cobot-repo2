"""HTTP transport for the finalized Incoming Material QA v0.1 request ACK."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Callable

import httpx

from shared.config import Settings, get_settings
from shared.schemas.vision import IncomingMaterialQARequest

logger = logging.getLogger(__name__)


class IncomingMaterialQAHttpError(RuntimeError):
    """Base FMS → Vision HTTP request failure."""


class IncomingMaterialQAHttpConflictError(IncomingMaterialQAHttpError):
    """Vision reported same request ID with a conflicting request contract."""


class IncomingMaterialQAHttpTransportError(IncomingMaterialQAHttpError):
    """The request was not acknowledged after configured same-payload retries."""


@dataclass(frozen=True, slots=True)
class IncomingMaterialQAAcknowledgement:
    status_code: int
    idempotent: bool = False


class IncomingMaterialQAHttpClient:
    """Send only the v0.1 request and wait only for its HTTP acknowledgement.

    Vision later delivers the terminal inspection result to the callback API.  A
    202 here is never treated as QA completion or RELEASE.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep

    @property
    def request_url(self) -> str:
        return (
            f"{self._settings.vision_incoming_qa_base_url.rstrip('/')}"
            f"/{self._settings.vision_incoming_qa_request_path.lstrip('/')}"
        )

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._settings.vision_incoming_qa_timeout_seconds)
        return self._client

    def send_request(self, request: IncomingMaterialQARequest) -> IncomingMaterialQAAcknowledgement:
        """POST a saved request snapshot; all retries retain exactly this payload."""

        payload = request.model_dump(mode="json")
        attempts = max(1, self._settings.vision_incoming_qa_max_attempts)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                logger.info(
                    "Incoming QA request send attempt: inspection_request_id=%s delivery_item_id=%s inspection_cycle=%s attempt=%s/%s",
                    request.inspection_request_id,
                    request.delivery_item_id,
                    request.inspection_cycle,
                    attempt,
                    attempts,
                )
                response = self._get_client().post(self.request_url, json=payload)
                if response.status_code == 202:
                    logger.info("Incoming QA request acknowledged: inspection_request_id=%s status=202", request.inspection_request_id)
                    return IncomingMaterialQAAcknowledgement(status_code=202)
                # The deployed Vision service may return an idempotent success on
                # a repeated identical request.  It is still only an ACK.
                if response.status_code == 200:
                    logger.info("Incoming QA request idempotently acknowledged: inspection_request_id=%s status=200", request.inspection_request_id)
                    return IncomingMaterialQAAcknowledgement(status_code=200, idempotent=True)
                if response.status_code == 409:
                    raise IncomingMaterialQAHttpConflictError(
                        f"Vision Incoming QA request conflict for {request.inspection_request_id}."
                    )
                if response.status_code >= 500:
                    raise IncomingMaterialQAHttpTransportError(
                        f"Vision Incoming QA server error HTTP {response.status_code}."
                    )
                raise IncomingMaterialQAHttpError(
                    f"Vision Incoming QA rejected request HTTP {response.status_code}: {response.text[:300]}"
                )
            except IncomingMaterialQAHttpConflictError:
                logger.error("Incoming QA request conflict: inspection_request_id=%s", request.inspection_request_id)
                raise
            except (httpx.TimeoutException, httpx.NetworkError, IncomingMaterialQAHttpTransportError) as exc:
                last_error = exc
                if attempt == attempts:
                    break
                logger.warning(
                    "Incoming QA request retrying: inspection_request_id=%s attempt=%s/%s error=%s",
                    request.inspection_request_id,
                    attempt,
                    attempts,
                    exc,
                )
                self._sleep(self._settings.vision_incoming_qa_retry_delay_seconds)
            except httpx.HTTPError as exc:
                last_error = exc
                break
        raise IncomingMaterialQAHttpTransportError(
            f"Vision Incoming QA request was not acknowledged for {request.inspection_request_id}: {last_error}"
        )

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
