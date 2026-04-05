"""
inbox.py — Consume new messages from rpc_client and write to store.

Owns the ingest half of the agent lifecycle:
    new Message → deduplicate → write inbox file → update history → update context
"""
from __future__ import annotations

import logging

from .models import Message
from .store import MessageStore

logger = logging.getLogger(__name__)


class InboxProcessor:
    """
    Processes incoming Message objects into the data store.

    Deduplication guarantee: if inbox_exists() returns True for a given
    (rowid, chat_id), the message is silently skipped. This makes the
    processor idempotent — restarting with an old cursor produces no duplicates.
    """

    def __init__(self, store: MessageStore, max_history: int = 20) -> None:
        self._store = store
        self._max_history = max_history

    def process(self, message: Message) -> bool:
        """
        Ingest one message.

        Returns:
            True  — message was new and written to disk.
            False — message already existed (duplicate), skipped.
        """
        if self._store.inbox_exists(message.rowid, message.chat_id):
            logger.debug(
                "Skipping duplicate rowid=%d chat_id=%d", message.rowid, message.chat_id
            )
            return False

        # 1. Write inbox file
        self._store.write_inbox(message)

        # 2. Append to rolling chat history
        self._store.append_chat_history(message.chat_id, message, self._max_history)

        # 3. Update chat context (last seen rowid + last active timestamp)
        ctx = self._store.read_chat_context(message.chat_id)
        ctx["chat_id"] = message.chat_id
        ctx["last_seen_rowid"] = message.rowid
        ctx["last_active"] = message.date.isoformat()
        self._store.write_chat_context(message.chat_id, ctx)

        logger.info(
            "Ingested message rowid=%d chat_id=%d sender=%r len=%d",
            message.rowid,
            message.chat_id,
            message.sender,
            len(message.text),
        )
        return True
