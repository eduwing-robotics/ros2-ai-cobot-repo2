#!/usr/bin/env python3
"""Persistent Incoming QA v0.2 transaction registry."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class TransactionStoreV2:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(
            self.db_path
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._lock, self._connect() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS incoming_qa_requests_v2 (
                    inspection_request_id TEXT PRIMARY KEY,
                    inspection_cycle INTEGER NOT NULL,
                    inspection_mode TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    runtime_status TEXT NOT NULL,
                    result_json TEXT,
                    reply_host TEXT,
                    reply_port INTEGER,
                    updated_at TEXT NOT NULL
                )
                """
            )

            c.execute(
                """
                CREATE TABLE IF NOT EXISTS incoming_qa_request_items_v2 (
                    inspection_request_id TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    delivery_item_id INTEGER NOT NULL,
                    inspection_cycle INTEGER NOT NULL,
                    expected_part_code TEXT NOT NULL,
                    expected_class_name TEXT NOT NULL,
                    expected_quantity INTEGER NOT NULL,
                    PRIMARY KEY (
                        inspection_request_id,
                        slot_id
                    ),
                    UNIQUE (
                        delivery_item_id,
                        inspection_cycle
                    )
                )
                """
            )

            c.commit()

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as c:
            row = c.execute(
                """
                SELECT *
                FROM incoming_qa_requests_v2
                WHERE inspection_request_id = ?
                """,
                (request_id,),
            ).fetchone()

        return (
            dict(row)
            if row is not None
            else None
        )

    def register(
        self,
        *,
        request_id: str,
        inspection_cycle: int,
        inspection_mode: str,
        payload: dict[str, Any],
        payload_sha256: str,
        accepted_at: str,
        reply_host: str,
        reply_port: int,
    ) -> str:
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        items = payload["items"]

        with self._lock, self._connect() as c:
            existing = c.execute(
                """
                SELECT payload_sha256
                FROM incoming_qa_requests_v2
                WHERE inspection_request_id = ?
                """,
                (request_id,),
            ).fetchone()

            if existing is not None:
                if (
                    existing["payload_sha256"]
                    == payload_sha256
                ):
                    return "DUPLICATE"

                return "CONFLICT"

            try:
                c.execute(
                    """
                    INSERT INTO incoming_qa_requests_v2 (
                        inspection_request_id,
                        inspection_cycle,
                        inspection_mode,
                        payload_json,
                        payload_sha256,
                        accepted_at,
                        runtime_status,
                        reply_host,
                        reply_port,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'REQUESTED', ?, ?, ?)
                    """,
                    (
                        request_id,
                        inspection_cycle,
                        inspection_mode,
                        payload_json,
                        payload_sha256,
                        accepted_at,
                        reply_host,
                        reply_port,
                        accepted_at,
                    ),
                )

                for item in items:
                    c.execute(
                        """
                        INSERT INTO incoming_qa_request_items_v2 (
                            inspection_request_id,
                            slot_id,
                            delivery_item_id,
                            inspection_cycle,
                            expected_part_code,
                            expected_class_name,
                            expected_quantity
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            request_id,
                            item["slot_id"],
                            item["delivery_item_id"],
                            inspection_cycle,
                            item["expected_part_code"],
                            item["expected_class_name"],
                            item["expected_quantity"],
                        ),
                    )

                c.commit()

            except sqlite3.IntegrityError:
                c.rollback()
                return "CYCLE_CONFLICT"

        return "CREATED"

    def set_status(
        self,
        request_id: str,
        status: str,
        updated_at: str,
    ):
        with self._lock, self._connect() as c:
            c.execute(
                """
                UPDATE incoming_qa_requests_v2
                SET runtime_status = ?,
                    updated_at = ?
                WHERE inspection_request_id = ?
                """,
                (
                    status,
                    updated_at,
                    request_id,
                ),
            )
            c.commit()

    def set_result(
        self,
        request_id: str,
        result: dict[str, Any],
        updated_at: str,
    ):
        result_json = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        with self._lock, self._connect() as c:
            c.execute(
                """
                UPDATE incoming_qa_requests_v2
                SET runtime_status = 'COMPLETED',
                    result_json = ?,
                    updated_at = ?
                WHERE inspection_request_id = ?
                """,
                (
                    result_json,
                    updated_at,
                    request_id,
                ),
            )
            c.commit()
