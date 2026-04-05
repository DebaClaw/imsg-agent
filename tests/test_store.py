"""
Tests for store.py — all tests use pytest's tmp_path fixture.
No ~/imsg-data/ is touched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.models import Draft, Message, OutboxItem
from agent.store import MessageStore, _parse_frontmatter, _write_frontmatter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _draft(
    uuid: str = "test-uuid-1234",
    chat_id: int = 7,
    approved: bool = False,
) -> Draft:
    return Draft(
        uuid=uuid,
        chat_id=chat_id,
        target_identifier="iMessage;-;+14155550101",
        created_at=datetime(2026, 4, 4, 10, 31, 0, tzinfo=timezone.utc),
        proposed_text="Yes, see you Thursday!",
        reasoning="User asked about Thursday meeting.",
        prompt_version="v1",
        approved=approved,
        source_rowid=1,
    )


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def test_roundtrip_simple(self) -> None:
        meta = {"key": "value", "num": 42, "flag": True}
        body = "Hello, world!\nMore text."
        assert _parse_frontmatter(_write_frontmatter(meta, body)) == (meta, body)

    def test_no_frontmatter_passthrough(self) -> None:
        text = "Just a plain body."
        assert _parse_frontmatter(text) == ({}, text)

    def test_empty_body(self) -> None:
        meta = {"x": 1}
        content = _write_frontmatter(meta, "")
        parsed_meta, parsed_body = _parse_frontmatter(content)
        assert parsed_meta == meta
        assert parsed_body == ""

    def test_unicode_in_body(self) -> None:
        meta = {"sender": "+1"}
        body = "Hey 🎉 こんにちは"
        content = _write_frontmatter(meta, body)
        parsed_meta, parsed_body = _parse_frontmatter(content)
        assert parsed_body == body


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


class TestCursor:
    def test_defaults_to_zero_when_no_file(self, tmp_path: Path) -> None:
        assert MessageStore(tmp_path).read_cursor() == 0

    def test_write_and_read(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.write_cursor(12345)
        assert store.read_cursor() == 12345

    def test_overwrite_advances(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.write_cursor(100)
        store.write_cursor(200)
        assert store.read_cursor() == 200

    def test_no_tmp_file_left_after_write(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.write_cursor(1)
        assert not list(tmp_path.glob("**/*.tmp"))


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


class TestInbox:
    def test_write_creates_file(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.write_inbox(_msg())
        assert store.inbox_exists(1, 7)

    def test_inbox_path_naming(self, tmp_path: Path) -> None:
        assert MessageStore(tmp_path).inbox_path(12345, 7).name == "12345-7.md"

    def test_exists_false_before_write(self, tmp_path: Path) -> None:
        assert not MessageStore(tmp_path).inbox_exists(999, 7)

    def test_roundtrip_text(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        msg = _msg(rowid=42, text="Are we still on Thursday?")
        store.write_inbox(msg)
        recovered = store.read_inbox_message(store.inbox_path(42, 7))
        assert recovered is not None
        assert recovered.rowid == 42
        assert recovered.text == "Are we still on Thursday?"
        assert recovered.sender == "+14155550101"
        assert recovered.chat_id == 7
        assert recovered.is_from_me is False

    def test_roundtrip_optional_fields(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        msg = _msg(rowid=5)
        msg_with_reply = Message(
            **{**msg.__dict__, "reply_to_guid": "PARENT-GUID"}
        )
        store.write_inbox(msg_with_reply)
        recovered = store.read_inbox_message(store.inbox_path(5, 7))
        assert recovered is not None
        assert recovered.reply_to_guid == "PARENT-GUID"

    def test_no_partial_write(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.write_inbox(_msg())
        assert not list(tmp_path.glob("**/*.tmp"))

    def test_list_unprocessed_is_sorted(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.write_inbox(_msg(rowid=20))
        store.write_inbox(_msg(rowid=5))
        store.write_inbox(_msg(rowid=10))
        names = [p.name for p in store.list_unprocessed_inbox()]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# Chat context
# ---------------------------------------------------------------------------


class TestChatContext:
    def test_returns_empty_when_missing(self, tmp_path: Path) -> None:
        assert MessageStore(tmp_path).read_chat_context(7) == {}

    def test_roundtrip(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.write_chat_context(7, {"chat_id": 7, "name": "Alex", "service": "iMessage"})
        ctx = store.read_chat_context(7)
        assert ctx["chat_id"] == 7
        assert ctx["name"] == "Alex"

    def test_notes_not_mutated_in_caller_dict(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        original = {"chat_id": 7, "notes": "Close friend"}
        store.write_chat_context(7, original)
        # Original dict must be unchanged
        assert "notes" in original

    def test_notes_stored_in_body(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.write_chat_context(7, {"chat_id": 7, "notes": "Best friend"})
        raw = (tmp_path / "chats" / "7" / "context.md").read_text()
        _, body = _parse_frontmatter(raw)
        assert "Best friend" in body


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------


class TestChatHistory:
    def test_append_creates_file(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.append_chat_history(7, _msg())
        assert (tmp_path / "chats" / "7" / "history.md").exists()

    def test_text_present_after_append(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.append_chat_history(7, _msg(text="Are we still on Thursday?"))
        assert "Are we still on Thursday?" in store.read_chat_history(7)

    def test_rolling_window_drops_oldest(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        for i in range(5):
            store.append_chat_history(7, _msg(rowid=i, text=f"Message {i}"), max_messages=3)
        history = store.read_chat_history(7)
        assert "Message 4" in history
        assert "Message 3" in history
        assert "Message 2" in history
        assert "Message 0" not in history

    def test_no_tmp_file_left(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.append_chat_history(7, _msg())
        assert not list(tmp_path.glob("**/*.tmp"))


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


class TestDrafts:
    def test_write_creates_file(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.write_draft(_draft())
        assert (tmp_path / "chats" / "7" / "drafts" / "test-uuid-1234.md").exists()

    def test_unapproved_not_in_approved_list(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.write_draft(_draft(approved=False))
        assert store.list_approved_drafts() == []

    def test_approved_appears_in_list(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        store.write_draft(_draft(approved=True))
        assert len(store.list_approved_drafts()) == 1

    def test_roundtrip(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        d = _draft()
        store.write_draft(d)
        path = tmp_path / "chats" / "7" / "drafts" / "test-uuid-1234.md"
        recovered = store.read_draft(path)
        assert recovered is not None
        assert recovered.uuid == d.uuid
        assert recovered.proposed_text == d.proposed_text
        assert recovered.reasoning == d.reasoning

    def test_move_to_outbox(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        d = _draft(approved=True)
        store.write_draft(d)
        store.move_draft_to_outbox(d)
        assert not (tmp_path / "chats" / "7" / "drafts" / "test-uuid-1234.md").exists()
        assert (tmp_path / "outbox" / "test-uuid-1234.md").exists()


# ---------------------------------------------------------------------------
# Outbox / Sent / Errors
# ---------------------------------------------------------------------------


def _put_item_in_outbox(store: MessageStore, tmp_path: Path) -> OutboxItem:
    """Helper: write a draft, approve it, move to outbox, return the OutboxItem."""
    d = _draft(approved=True)
    store.write_draft(d)
    store.move_draft_to_outbox(d)
    path = tmp_path / "outbox" / f"{d.uuid}.md"
    item = store.read_outbox_item(path)
    assert item is not None
    return item


class TestOutbox:
    def test_list_outbox_finds_item(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        _put_item_in_outbox(store, tmp_path)
        assert len(store.list_outbox()) == 1

    def test_move_to_sent_removes_from_outbox(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        item = _put_item_in_outbox(store, tmp_path)
        store.move_to_sent(item)
        assert store.list_outbox() == []
        assert (tmp_path / "sent" / f"{item.uuid}.md").exists()

    def test_move_to_errors_removes_from_outbox(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        item = _put_item_in_outbox(store, tmp_path)
        store.move_to_errors(item, reason="send timeout")
        assert store.list_outbox() == []
        error_path = tmp_path / "errors" / f"{item.uuid}.md"
        assert error_path.exists()

    def test_error_file_contains_reason(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        item = _put_item_in_outbox(store, tmp_path)
        store.move_to_errors(item, reason="connection refused")
        error_path = tmp_path / "errors" / f"{item.uuid}.md"
        meta, _ = _parse_frontmatter(error_path.read_text())
        assert "connection refused" in meta["error"]

    def test_sent_file_contains_text(self, tmp_path: Path) -> None:
        store = MessageStore(tmp_path)
        item = _put_item_in_outbox(store, tmp_path)
        store.move_to_sent(item)
        sent_path = tmp_path / "sent" / f"{item.uuid}.md"
        _, body = _parse_frontmatter(sent_path.read_text())
        assert item.text in body
