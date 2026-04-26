"""
archiver.py - Non-GenAI iMessage archive backfill and monitor.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from datetime import timedelta
from time import monotonic
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
        history_page_size: int = 100,
        include_attachments: bool = True,
        debug: bool = False,
    ) -> tuple[int, int]:
        """Fetch chats and historical messages into SQLite."""
        started = monotonic()
        logger.info(
            "Starting archive backfill chat_limit=%d history_limit=%d "
            "history_page_size=%d include_attachments=%s",
            chat_limit,
            history_limit,
            history_page_size,
            include_attachments,
        )
        chats = await self._rpc.list_chats(limit=chat_limit)
        logger.info("Found %d chats to archive", len(chats))
        message_count = 0
        for idx, chat in enumerate(chats, start=1):
            chat_started = monotonic()
            self._archive.upsert_chat(chat)
            logger.info(
                "Starting chat %d/%d chat_id=%d name=%r identifier=%r participants=%d",
                idx,
                len(chats),
                chat.id,
                chat.name,
                chat.identifier,
                len(chat.participants),
            )
            archived_for_chat = await self._backfill_chat_history(
                chat_name=chat.name,
                chat_id=chat.id,
                history_limit=history_limit,
                history_page_size=history_page_size,
                include_attachments=include_attachments,
                debug=debug,
            )
            message_count += archived_for_chat
            logger.info(
                "Finished chat_id=%d messages=%d total_messages=%d elapsed=%.2fs",
                chat.id,
                archived_for_chat,
                message_count,
                monotonic() - chat_started,
            )
        logger.info(
            "Backfill finished chats=%d messages=%d elapsed=%.2fs",
            len(chats),
            message_count,
            monotonic() - started,
        )
        return len(chats), message_count

    async def _backfill_chat_history(
        self,
        *,
        chat_name: str,
        chat_id: int,
        history_limit: int,
        history_page_size: int,
        include_attachments: bool,
        debug: bool,
    ) -> int:
        total = 0
        already_archived = self._archive.count_messages_for_chat(chat_id)
        resume_from = self._archive.oldest_message_for_chat(chat_id)
        end: str | None = None
        if resume_from is not None:
            oldest_rowid, oldest_date = resume_from
            end = (oldest_date - timedelta(microseconds=1)).isoformat()
            logger.info(
                "Resuming chat_id=%d name=%r below oldest archived message "
                "rowid=%d date=%s archived_messages=%d next_end=%s",
                chat_id,
                chat_name,
                oldest_rowid,
                oldest_date.isoformat(),
                already_archived,
                end,
            )
        seen_oldest: tuple[int, str] | None = None
        page_size = max(1, history_page_size)
        current_page_size = page_size
        page_number = 0

        while total < history_limit:
            limit = min(current_page_size, history_limit - total)
            page_number += 1
            request_started = monotonic()
            logger.info(
                "Fetching chat_id=%d name=%r page=%d limit=%d end=%s "
                "include_attachments=%s total_for_chat=%d",
                chat_id,
                chat_name,
                page_number,
                limit,
                end,
                include_attachments,
                total,
            )
            try:
                messages = await self._fetch_history_page(
                    chat_id=chat_id,
                    limit=limit,
                    end=end,
                    include_attachments=include_attachments,
                )
            except IMsgRPCConnectionError:
                elapsed = monotonic() - request_started
                if limit <= 1 and include_attachments:
                    try:
                        logger.warning(
                            "Attachment history timed out for chat_id=%d name=%r "
                            "page=%d page_size=1 end=%s elapsed=%.2fs; retrying "
                            "without attachment metadata",
                            chat_id,
                            chat_name,
                            page_number,
                            end,
                            elapsed,
                        )
                        request_started = monotonic()
                        messages = await self._fetch_history_page(
                            chat_id=chat_id,
                            limit=1,
                            end=end,
                            include_attachments=False,
                        )
                    except IMsgRPCConnectionError:
                        logger.exception(
                            "Skipping chat_id=%d name=%r page=%d after timeout at "
                            "page_size=1 with and without attachments end=%s",
                            chat_id,
                            chat_name,
                            page_number,
                            end,
                        )
                        break
                else:
                    current_page_size = max(1, limit // 2)
                    page_size = min(page_size, current_page_size)
                    if limit > 1:
                        logger.warning(
                            "Timed out fetching chat_id=%d name=%r page=%d page_size=%d "
                            "end=%s elapsed=%.2fs; lowering max page size to %d",
                            chat_id,
                            chat_name,
                            page_number,
                            limit,
                            end,
                            elapsed,
                            current_page_size,
                        )
                        continue
                    logger.exception(
                        "Skipping chat_id=%d name=%r page=%d after timeout at "
                        "page_size=1 include_attachments=%s end=%s",
                        chat_id,
                        chat_name,
                        page_number,
                        include_attachments,
                        end,
                    )
                    break
            elapsed = monotonic() - request_started
            if not messages:
                logger.info(
                    "No more messages chat_id=%d name=%r page=%d elapsed=%.2fs",
                    chat_id,
                    chat_name,
                    page_number,
                    elapsed,
                )
                break

            for message in messages:
                self._archive.upsert_message(message)
            total += len(messages)
            current_page_size = page_size

            oldest = min(messages, key=lambda message: (message.date, message.rowid))
            newest = max(messages, key=lambda message: (message.date, message.rowid))
            oldest_key = (oldest.rowid, oldest.date.isoformat())
            if oldest_key == seen_oldest:
                logger.warning(
                    "Stopping chat_id=%d name=%r because pagination did not advance "
                    "oldest_rowid=%d oldest_date=%s",
                    chat_id,
                    chat_name,
                    oldest.rowid,
                    oldest.date.isoformat(),
                )
                break
            seen_oldest = oldest_key
            end = (oldest.date - timedelta(microseconds=1)).isoformat()

            log = logger.info if debug else logger.debug
            log(
                "Archived page chat_id=%d name=%r page=%d messages=%d "
                "oldest_rowid=%d oldest_date=%s newest_rowid=%d newest_date=%s "
                "attachments=%d elapsed=%.2fs total_for_chat=%d next_end=%s",
                chat_id,
                chat_name,
                page_number,
                len(messages),
                oldest.rowid,
                oldest.date.isoformat(),
                newest.rowid,
                newest.date.isoformat(),
                sum(len(message.attachments) for message in messages),
                elapsed,
                total,
                end,
            )
            if len(messages) < limit:
                break

        return total

    async def _fetch_history_page(
        self,
        *,
        chat_id: int,
        limit: int,
        end: str | None,
        include_attachments: bool,
    ) -> list[Message]:
        return await self._rpc.get_history(
            chat_id=chat_id,
            limit=limit,
            end=end,
            include_attachments=include_attachments,
        )

    async def monitor(
        self,
        *,
        since_rowid: int | None = None,
        include_attachments: bool = True,
    ) -> None:
        """Watch for new messages and archive them forever."""
        cursor = self._archive.read_cursor() if since_rowid is None else since_rowid
        async for message in self._rpc.subscribe(
            since_rowid=cursor if cursor > 0 else None,
            include_attachments=include_attachments,
        ):
            self._archive.upsert_message(message)
            logger.info(
                "Archived live message rowid=%d chat_id=%d attachments=%d",
                message.rowid,
                message.chat_id,
                len(message.attachments),
            )
