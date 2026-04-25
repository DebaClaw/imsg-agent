"""
archiver.py - Non-GenAI iMessage archive backfill and monitor.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Protocol

from .archive_store import IMessageArchive
from .models import Chat, Message
from .rpc_client import IMsgRPCConnectionError

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
        history_page_size: int = 1_000,
    ) -> tuple[int, int]:
        """Fetch chats and historical messages into SQLite."""
        chats = await self._rpc.list_chats(limit=chat_limit)
        message_count = 0
        for chat in chats:
            self._archive.upsert_chat(chat)
            archived_for_chat = await self._backfill_chat_history(
                chat_id=chat.id,
                history_limit=history_limit,
                history_page_size=history_page_size,
            )
            message_count += archived_for_chat
            logger.info(
                "Archived chat_id=%d messages=%d total_messages=%d",
                chat.id,
                archived_for_chat,
                message_count,
            )
        return len(chats), message_count

    async def _backfill_chat_history(
        self,
        *,
        chat_id: int,
        history_limit: int,
        history_page_size: int,
    ) -> int:
        total = 0
        end: str | None = None
        seen_oldest: tuple[int, str] | None = None
        page_size = max(1, history_page_size)
        current_page_size = page_size

        while total < history_limit:
            limit = min(current_page_size, history_limit - total)
            try:
                messages = await self._rpc.get_history(
                    chat_id=chat_id,
                    limit=limit,
                    end=end,
                    include_attachments=True,
                )
            except IMsgRPCConnectionError:
                if limit <= 1:
                    logger.exception(
                        "Skipping chat_id=%d page after timeout at page_size=1 end=%s",
                        chat_id,
                        end,
                    )
                    break
                current_page_size = max(1, limit // 2)
                logger.warning(
                    "Timed out fetching chat_id=%d page_size=%d end=%s; retrying with %d",
                    chat_id,
                    limit,
                    end,
                    current_page_size,
                )
                continue
            if not messages:
                break

            for message in messages:
                self._archive.upsert_message(message)
            total += len(messages)
            current_page_size = page_size

            oldest = min(messages, key=lambda message: (message.date, message.rowid))
            oldest_key = (oldest.rowid, oldest.date.isoformat())
            if oldest_key == seen_oldest:
                logger.warning(
                    "Stopping chat_id=%d backfill because pagination did not advance",
                    chat_id,
                )
                break
            seen_oldest = oldest_key
            end = (oldest.date - timedelta(microseconds=1)).isoformat()

            logger.info(
                "Archived page chat_id=%d page_messages=%d total_for_chat=%d next_end=%s",
                chat_id,
                len(messages),
                total,
                end,
            )
            if len(messages) < limit:
                break

        return total

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
