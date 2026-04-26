from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.archive_store import IMessageArchive
from agent.archiver import IMessageArchiver
from agent.models import Attachment, Chat, Message
from agent.rpc_client import IMsgRPCConnectionError

NOW = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _chat(chat_id: int = 7) -> Chat:
    return Chat(
        id=chat_id,
        identifier=f"iMessage;-;+14155550{chat_id}",
        name=f"Chat {chat_id}",
        service="iMessage",
        last_message_at=NOW,
    )


def _message(rowid: int = 1, chat_id: int = 7) -> Message:
    return Message(
        rowid=rowid,
        chat_id=chat_id,
        guid=f"GUID-{rowid}",
        sender="+14155550101",
        text="Hello",
        date=NOW,
        is_from_me=False,
        service="iMessage",
        has_attachments=True,
        attachments=[
            Attachment(
                filename="photo.jpg",
                transfer_name="photo.jpg",
                uti="public.jpeg",
                mime_type="image/jpeg",
                total_bytes=100,
                is_sticker=False,
                original_path="/tmp/photo.jpg",
                missing=False,
            )
        ],
    )


class FakeRPC:
    def __init__(self) -> None:
        self.history_include_attachments: list[bool] = []
        self.history_limits: list[int] = []
        self.history_ends: list[str | None] = []
        self.subscribe_include_attachments: bool | None = None
        self.subscribe_since_rowid: int | None = None

    async def list_chats(self, limit: int = 20) -> list[Chat]:
        return [_chat(7), _chat(8)]

    async def get_history(
        self,
        chat_id: int,
        limit: int = 50,
        participants: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        include_attachments: bool = False,
    ) -> list[Message]:
        self.history_include_attachments.append(include_attachments)
        self.history_limits.append(limit)
        self.history_ends.append(end)
        if end is not None:
            return []
        return [_message(rowid=chat_id * 10, chat_id=chat_id)]

    async def subscribe(
        self,
        chat_id: int | None = None,
        since_rowid: int | None = None,
        include_reactions: bool = False,
        include_attachments: bool = False,
    ) -> AsyncGenerator[Message, None]:
        self.subscribe_include_attachments = include_attachments
        self.subscribe_since_rowid = since_rowid
        yield _message(rowid=99, chat_id=7)


@pytest.mark.asyncio
async def test_backfill_archives_all_chats_with_attachments(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    rpc = FakeRPC()

    chats, messages = await IMessageArchiver(archive, rpc).backfill()

    assert chats == 2
    assert messages == 2
    assert archive.count_chats() == 2
    assert archive.count_messages() == 2
    assert archive.count_attachments() == 2
    assert rpc.history_include_attachments == [True, True]
    assert rpc.history_limits == [100, 100]
    assert rpc.history_ends == [None, None]
    archive.close()


@pytest.mark.asyncio
async def test_backfill_can_skip_attachment_metadata(tmp_path: Path) -> None:
    class NoAttachmentRPC(FakeRPC):
        async def list_chats(self, limit: int = 20) -> list[Chat]:
            return [_chat(7)]

        async def get_history(
            self,
            chat_id: int,
            limit: int = 50,
            participants: list[str] | None = None,
            start: str | None = None,
            end: str | None = None,
            include_attachments: bool = False,
        ) -> list[Message]:
            self.history_include_attachments.append(include_attachments)
            if end is not None:
                return []
            message = _message(rowid=1, chat_id=chat_id)
            message.has_attachments = False
            message.attachments = []
            return [message]

    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    rpc = NoAttachmentRPC()

    chats, messages = await IMessageArchiver(archive, rpc).backfill(
        include_attachments=False,
    )

    assert chats == 1
    assert messages == 1
    assert archive.count_messages() == 1
    assert archive.count_attachments() == 0
    assert rpc.history_include_attachments == [False]
    archive.close()


@pytest.mark.asyncio
async def test_backfill_pages_large_chat_history(tmp_path: Path) -> None:
    class PagingRPC(FakeRPC):
        async def list_chats(self, limit: int = 20) -> list[Chat]:
            return [_chat(7)]

        async def get_history(
            self,
            chat_id: int,
            limit: int = 50,
            participants: list[str] | None = None,
            start: str | None = None,
            end: str | None = None,
            include_attachments: bool = False,
        ) -> list[Message]:
            self.history_limits.append(limit)
            self.history_ends.append(end)
            if end is None:
                return [_message(rowid=3), _message(rowid=2)]
            return [_message(rowid=1)]

    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    rpc = PagingRPC()

    chats, messages = await IMessageArchiver(archive, rpc).backfill(
        history_limit=10,
        history_page_size=2,
        debug=True,
    )

    assert chats == 1
    assert messages == 3
    assert archive.count_messages() == 3
    assert rpc.history_limits == [2, 2]
    assert rpc.history_ends[0] is None
    assert rpc.history_ends[1] is not None
    archive.close()


@pytest.mark.asyncio
async def test_backfill_resumes_below_oldest_archived_message(tmp_path: Path) -> None:
    class ResumeRPC(FakeRPC):
        async def list_chats(self, limit: int = 20) -> list[Chat]:
            return [_chat(7)]

        async def get_history(
            self,
            chat_id: int,
            limit: int = 50,
            participants: list[str] | None = None,
            start: str | None = None,
            end: str | None = None,
            include_attachments: bool = False,
        ) -> list[Message]:
            self.history_ends.append(end)
            if end is None:
                return [_message(rowid=99, chat_id=chat_id)]
            message = _message(rowid=1, chat_id=chat_id)
            message.date = NOW - timedelta(days=1)
            return [message]

    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    newest = _message(rowid=100, chat_id=7)
    newest.date = NOW
    archive.upsert_message(newest)
    rpc = ResumeRPC()

    chats, messages = await IMessageArchiver(archive, rpc).backfill(history_limit=1)

    assert chats == 1
    assert messages == 1
    assert archive.count_messages_for_chat(7) == 2
    assert rpc.history_ends == [
        (newest.date - timedelta(microseconds=1)).isoformat()
    ]
    archive.close()


@pytest.mark.asyncio
async def test_backfill_retries_timeout_until_page_succeeds(tmp_path: Path) -> None:
    class TimeoutThenSmallerRPC(FakeRPC):
        async def list_chats(self, limit: int = 20) -> list[Chat]:
            return [_chat(7)]

        async def get_history(
            self,
            chat_id: int,
            limit: int = 50,
            participants: list[str] | None = None,
            start: str | None = None,
            end: str | None = None,
            include_attachments: bool = False,
        ) -> list[Message]:
            self.history_limits.append(limit)
            if limit > 1:
                raise IMsgRPCConnectionError("timeout")
            if end is not None:
                return []
            return [_message(rowid=1)]

    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    rpc = TimeoutThenSmallerRPC()

    chats, messages = await IMessageArchiver(archive, rpc).backfill(
        history_limit=2,
        history_page_size=4,
    )

    assert chats == 1
    assert messages == 1
    assert rpc.history_limits == [2, 1, 1]
    archive.close()


@pytest.mark.asyncio
async def test_backfill_skips_page_after_single_message_timeout(tmp_path: Path) -> None:
    class AlwaysTimeoutRPC(FakeRPC):
        async def list_chats(self, limit: int = 20) -> list[Chat]:
            return [_chat(7)]

        async def get_history(
            self,
            chat_id: int,
            limit: int = 50,
            participants: list[str] | None = None,
            start: str | None = None,
            end: str | None = None,
            include_attachments: bool = False,
        ) -> list[Message]:
            self.history_limits.append(limit)
            raise IMsgRPCConnectionError("timeout")

    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    rpc = AlwaysTimeoutRPC()

    chats, messages = await IMessageArchiver(archive, rpc).backfill(
        history_limit=10,
        history_page_size=4,
    )

    assert chats == 1
    assert messages == 0
    assert rpc.history_limits == [4, 2, 1, 1]
    archive.close()


@pytest.mark.asyncio
async def test_backfill_falls_back_to_no_attachments_for_single_message_timeout(
    tmp_path: Path,
) -> None:
    class AttachmentTimeoutRPC(FakeRPC):
        def __init__(self) -> None:
            super().__init__()
            self.include_attachments_calls: list[bool] = []

        async def list_chats(self, limit: int = 20) -> list[Chat]:
            return [_chat(7)]

        async def get_history(
            self,
            chat_id: int,
            limit: int = 50,
            participants: list[str] | None = None,
            start: str | None = None,
            end: str | None = None,
            include_attachments: bool = False,
        ) -> list[Message]:
            self.history_limits.append(limit)
            self.include_attachments_calls.append(include_attachments)
            if include_attachments:
                raise IMsgRPCConnectionError("timeout")
            if end is not None:
                return []
            message = _message(rowid=1, chat_id=chat_id)
            message.has_attachments = False
            message.attachments = []
            return [message]

    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    rpc = AttachmentTimeoutRPC()

    chats, messages = await IMessageArchiver(archive, rpc).backfill(
        history_limit=1,
        history_page_size=1,
    )

    assert chats == 1
    assert messages == 1
    assert archive.count_messages() == 1
    assert archive.count_attachments() == 0
    assert rpc.include_attachments_calls == [True, False]
    archive.close()


@pytest.mark.asyncio
async def test_monitor_archives_new_messages_with_attachments(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    archive.write_cursor(42)
    rpc = FakeRPC()

    await IMessageArchiver(archive, rpc).monitor()

    assert rpc.subscribe_since_rowid == 42
    assert rpc.subscribe_include_attachments is True
    assert archive.count_messages() == 1
    assert archive.count_attachments() == 1
    archive.close()


@pytest.mark.asyncio
async def test_monitor_can_skip_attachment_metadata(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    rpc = FakeRPC()

    await IMessageArchiver(archive, rpc).monitor(include_attachments=False)

    assert rpc.subscribe_include_attachments is False
    archive.close()
