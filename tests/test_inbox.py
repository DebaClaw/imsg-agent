"""
Tests for inbox.py — mocks nothing; uses real MessageStore on tmp_path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.inbox import InboxProcessor
from agent.models import Message
from agent.store import MessageStore


def _msg(rowid: int = 1, chat_id: int = 7, text: str = "Hello") -> Message:
    return Message(
        rowid=rowid,
        chat_id=chat_id,
        guid=f"GUID-{rowid}",
        sender="+14155550101",
        text=text,
        date=datetime(2026, 4, 4, 10, 30, 0, tzinfo=timezone.utc),
        is_from_me=False,
        service="iMessage",
        has_attachments=False,
    )


class TestInboxProcessor:
    def test_new_message_returns_true(self, tmp_path: Path) -> None:
        processor = InboxProcessor(MessageStore(tmp_path))
        assert processor.process(_msg()) is True

    def test_new_message_creates_inbox_file(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        InboxProcessor(store).process(_msg(rowid=42))
        assert store.inbox_exists(42, 7)

    def test_duplicate_returns_false(self, tmp_path: Path) -> None:
        processor = InboxProcessor(MessageStore(tmp_path))
        processor.process(_msg())
        assert processor.process(_msg()) is False

    def test_duplicate_does_not_create_second_file(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        processor = InboxProcessor(store)
        processor.process(_msg(rowid=5))
        processor.process(_msg(rowid=5))
        files = list((tmp_path / "inbox").glob("*.md"))
        assert len(files) == 1

    def test_chat_context_last_seen_rowid_updated(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        InboxProcessor(store).process(_msg(rowid=99))
        assert store.read_chat_context(7)["last_seen_rowid"] == 99

    def test_chat_context_last_active_set(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        InboxProcessor(store).process(_msg())
        ctx = store.read_chat_context(7)
        assert "last_active" in ctx

    def test_chat_history_contains_text(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        InboxProcessor(store).process(_msg(text="Are we still on for Thursday?"))
        assert "Are we still on for Thursday?" in store.read_chat_history(7)

    def test_multiple_messages_different_rowids(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        processor = InboxProcessor(store)
        for i in range(5):
            assert processor.process(_msg(rowid=i + 1, text=f"msg {i}")) is True
        assert len(list((tmp_path / "inbox").glob("*.md"))) == 5

    def test_context_not_updated_on_duplicate(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        processor = InboxProcessor(store)
        processor.process(_msg(rowid=10))
        # Manually bump last_seen_rowid to something higher to detect if it gets reset
        ctx = store.read_chat_context(7)
        ctx["last_seen_rowid"] = 999
        store.write_chat_context(7, ctx)
        # Process duplicate — should not touch context
        processor.process(_msg(rowid=10))
        assert store.read_chat_context(7)["last_seen_rowid"] == 999

    def test_history_rolling_window_respected(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        processor = InboxProcessor(store, max_history=3)
        for i in range(5):
            processor.process(_msg(rowid=i + 1, text=f"Message {i}"))
        history = store.read_chat_history(7)
        assert "Message 4" in history
        assert "Message 0" not in history
