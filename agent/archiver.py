"""
archiver.py - Non-GenAI iMessage archive backfill and monitor.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Protocol

from .archive_store import IMessageArchive
from .models import Chat, Message

logger = logging.getLogger(__name__)


class ArchiveRPC(Protocol):
    async def list_chats(self, limit: int = 20) -> list[Chat]: ...

    async def get_history(
        self,
        chat_id: int,
        limit: int = 50,
        participants: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        include_attachments: bool = False,
    ) -> list[Message]: ...

    def subscribe(
        self,
        chat_id: int | None = None,
        since_rowid: int | None = None,
        include_reactions: bool = False,
        include_attachments: bool = False,
    ) -> AsyncGenerator[Message, None]: ...


class IMessageArchiver:
    def __init__(self, archive: IMessageArchive, rpc: ArchiveRPC) -> None:
        self._archive = archive
        self._rpc = rpc

    async def backfill(
        self,
        *,
        chat_limit: int = 10_000,
        history_limit: int = 100_000,
    ) -> tuple[int, int]:
        """Fetch chats and historical messages into SQLite."""
        chats = await self._rpc.list_chats(limit=chat_limit)
        message_count = 0
        for chat in chats:
            self._archive.upsert_chat(chat)
            messages = await self._rpc.get_history(
                chat_id=chat.id,
                limit=history_limit,
                include_attachments=True,
            )
            for message in messages:
                self._archive.upsert_message(message)
            message_count += len(messages)
            logger.info(
                "Archived chat_id=%d messages=%d total_messages=%d",
                chat.id,
                len(messages),
                message_count,
            )
        return len(chats), message_count

    async def monitor(self, *, since_rowid: int | None = None) -> None:
        """Watch for new messages and archive them forever."""
        cursor = self._archive.read_cursor() if since_rowid is None else since_rowid
        async for message in self._rpc.subscribe(
            since_rowid=cursor if cursor > 0 else None,
            include_attachments=True,
        ):
            self._archive.upsert_message(message)
            logger.info(
                "Archived live message rowid=%d chat_id=%d attachments=%d",
                message.rowid,
                message.chat_id,
                len(message.attachments),
            )
