"""
sender.py - Send approved outbox items via imsg rpc.

Scans approved draft files into outbox, sends outbox items, and archives results.
This module does NOT decide what to say; it only executes approved files.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .models import OutboxItem
from .rpc_client import IMsgRPCClient
from .store import MessageStore

logger = logging.getLogger(__name__)

_MAC_ABSOLUTE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class DeliveryVerification:
    rowid: int
    date: int


class DeliveryVerificationFailed(Exception):
    """Raised when an RPC send cannot be confirmed in Messages."""


class DeliveryVerifier(Protocol):
    async def verify(
        self,
        *,
        item: OutboxItem,
        attempt_started_at: datetime,
    ) -> DeliveryVerification:
        """Return the matching outgoing Messages row, or raise on failure."""


class MessagesDeliveryVerifier:
    """Poll Messages chat.db until the sent message is visible."""

    def __init__(
        self,
        chat_db_path: Path | None = None,
        *,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        self._chat_db_path = (
            chat_db_path
            if chat_db_path is not None
            else Path("~/Library/Messages/chat.db").expanduser()
        )
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    async def verify(
        self,
        *,
        item: OutboxItem,
        attempt_started_at: datetime,
    ) -> DeliveryVerification:
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        last_error: Exception | None = None

        while True:
            try:
                verification = self._find_matching_row(item, attempt_started_at)
                if verification is not None:
                    return verification
            except sqlite3.Error as exc:
                last_error = exc
                if self._looks_like_tcc_block(exc):
                    raise DeliveryVerificationFailed(
                        "Messages database could not be read; "
                        f"Full Disk Access may be blocked: {exc}"
                    ) from exc

            if asyncio.get_running_loop().time() >= deadline:
                detail = (
                    f"; last Messages database error: {last_error}"
                    if last_error is not None
                    else ""
                )
                raise DeliveryVerificationFailed(
                    "send returned but no outgoing Messages row was observed"
                    f" for chat_id={item.chat_id}{detail}"
                )
            await asyncio.sleep(self._poll_interval_seconds)

    def _find_matching_row(
        self,
        item: OutboxItem,
        attempt_started_at: datetime,
    ) -> DeliveryVerification | None:
        threshold = self._to_messages_date(attempt_started_at)
        uri = f"file:{self._chat_db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            row = db.execute(
                """
                SELECT m.ROWID, m.date
                FROM message AS m
                JOIN chat_message_join AS cmj ON cmj.message_id = m.ROWID
                WHERE cmj.chat_id = ?
                  AND m.is_from_me = 1
                  AND COALESCE(m.text, '') = ?
                  AND m.date >= ?
                ORDER BY m.date DESC, m.ROWID DESC
                LIMIT 1
                """,
                (item.chat_id, item.text, threshold),
            ).fetchone()
        if row is None:
            return None
        return DeliveryVerification(rowid=int(row[0]), date=int(row[1]))

    def _to_messages_date(self, value: datetime) -> int:
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        seconds = (dt.astimezone(UTC) - _MAC_ABSOLUTE_EPOCH).total_seconds()
        return int(seconds * 1_000_000_000)

    def _looks_like_tcc_block(self, exc: sqlite3.Error) -> bool:
        message = str(exc).lower()
        return "authorization" in message or "not authorized" in message


class ApprovalScanner:
    """Move approved draft files into outbox."""

    def __init__(self, store: MessageStore) -> None:
        self._store = store

    def run_pass(self) -> int:
        moved = 0
        for path in self._store.list_approved_drafts():
            draft = self._store.read_draft(path)
            if draft is None:
                continue
            if not draft.approved:
                continue
            self._store.move_draft_to_outbox(draft)
            moved += 1
            logger.info(
                "Moved approved draft uuid=%s chat_id=%d to outbox",
                draft.uuid,
                draft.chat_id,
            )
        return moved


class Sender:
    def __init__(
        self,
        store: MessageStore,
        rpc: IMsgRPCClient,
        *,
        service: str = "auto",
        verifier: DeliveryVerifier | None = None,
    ) -> None:
        self._store = store
        self._rpc = rpc
        self._service = service
        self._verifier = verifier or MessagesDeliveryVerifier()

    async def run_pass(self) -> int:
        sent = 0
        for path in self._store.list_outbox():
            item = self._store.read_outbox_item(path)
            if item is None:
                self._store.move_bad_outbox_to_errors(path, "failed to parse outbox item")
                continue
            if not self._validate_attachment(item):
                self._store.move_to_errors(
                    item,
                    "attachment outside allowed outbox attachments path",
                )
                continue

            attempt_started_at = datetime.now(UTC)
            try:
                await self._rpc.send(
                    text=item.text,
                    file=item.attachment_path,
                    service=self._service_for(item),
                    chat_id=item.chat_id,
                )
            except TimeoutError as exc:
                self._store.move_to_errors(item, f"RPC timeout/error: {exc}")
                logger.warning("RPC timed out for outbox uuid=%s: %s", item.uuid, exc)
                continue
            except Exception as exc:
                self._store.move_to_errors(item, f"RPC timeout/error: {exc}")
                logger.warning("RPC failed for outbox uuid=%s: %s", item.uuid, exc)
                continue

            try:
                verification = await self._verifier.verify(
                    item=item,
                    attempt_started_at=attempt_started_at,
                )
            except DeliveryVerificationFailed as exc:
                self._store.move_to_errors(item, str(exc))
                logger.warning("Failed to verify sent outbox uuid=%s: %s", item.uuid, exc)
                continue

            self._store.move_to_sent(item)
            self._store.discard_outbox_item(item.uuid)
            sent += 1
            logger.info(
                "Sent outbox uuid=%s chat_id=%d messages_rowid=%d",
                item.uuid,
                item.chat_id,
                verification.rowid,
            )
        return sent

    def _validate_attachment(self, item: OutboxItem) -> bool:
        if not item.attachment_path:
            return True
        resolved = Path(item.attachment_path).expanduser().resolve()
        allowed = (self._store.data_dir / "outbox" / "attachments").resolve()
        return resolved == allowed or allowed in resolved.parents

    def _service_for(self, item: OutboxItem) -> str:
        service = item.service or self._service or "auto"
        if self._service == "auto" and service.lower() in {"sms", "imessage"}:
            return "auto"
        return service
