"""
sender.py - Send approved outbox items via imsg rpc.

Scans approved draft files into outbox, sends outbox items, and archives results.
This module does NOT decide what to say; it only executes approved files.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .models import OutboxItem
from .rpc_client import IMsgRPCClient
from .store import MessageStore

logger = logging.getLogger(__name__)


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
    def __init__(self, store: MessageStore, rpc: IMsgRPCClient, *, service: str = "auto") -> None:
        self._store = store
        self._rpc = rpc
        self._service = service

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

            # Prewrite the sent archive so an archive failure aborts before the RPC send.
            self._store.move_to_sent(item)
            try:
                await self._rpc.send(
                    text=item.text,
                    file=item.attachment_path,
                    service=self._service,
                    chat_id=item.chat_id,
                )
            except Exception as exc:
                self._store.move_sent_to_errors(item.uuid, reason=str(exc))
                logger.warning("Failed to send outbox uuid=%s: %s", item.uuid, exc)
                continue

            self._store.discard_outbox_item(item.uuid)
            sent += 1
            logger.info("Sent outbox uuid=%s chat_id=%d", item.uuid, item.chat_id)
        return sent

    def _validate_attachment(self, item: OutboxItem) -> bool:
        if not item.attachment_path:
            return True
        resolved = Path(item.attachment_path).expanduser().resolve()
        allowed = (self._store.data_dir / "outbox" / "attachments").resolve()
        return resolved == allowed or allowed in resolved.parents
