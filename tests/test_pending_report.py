from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agent.archive_store import IMessageArchive
from agent.models import Chat, Draft, Message
from agent.pending_report import pending_replies
from agent.store import MessageStore

NOW = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)


def _chat() -> Chat:
    return Chat(
        id=7,
        identifier="iMessage;-;+18015550101",
        name="Alex",
        service="iMessage",
        last_message_at=NOW,
        participants=["+18015550101"],
    )


def _message(rowid: int = 100) -> Message:
    return Message(
        rowid=rowid,
        chat_id=7,
        guid=f"GUID-{rowid}",
        sender="+18015550101",
        text="Can you help with this?",
        date=NOW,
        is_from_me=False,
        service="iMessage",
        has_attachments=False,
        chat_name="Alex",
        participants=["+18015550101"],
    )


def _draft(*, approved: bool = False) -> Draft:
    return Draft(
        uuid="draft-1",
        chat_id=7,
        target_identifier="iMessage;-;+18015550101",
        created_at=NOW,
        proposed_text="Yep, I can help with that.",
        reasoning="They asked a straightforward question.",
        prompt_version="v1",
        approved=approved,
        source_rowid=100,
        model="gpt-5.5",
    )


def test_pending_replies_includes_matching_draft(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    archive.upsert_chat(_chat())
    archive.upsert_message(_message())
    store = MessageStore(tmp_path)
    store.write_draft(_draft())

    rows = pending_replies(archive, store, limit=5)

    assert len(rows) == 1
    assert rows[0]["chat_id"] == 7
    assert rows[0]["message_rowid"] == 100
    assert rows[0]["last_text"] == "Can you help with this?"
    assert rows[0]["draft_status"] == "draft_unapproved"
    assert rows[0]["draft_uuid"] == "draft-1"
    assert rows[0]["proposed_text"] == "Yep, I can help with that."
    assert rows[0]["reasoning"] == "They asked a straightforward question."
    assert str(rows[0]["draft_path"]).endswith("chats/7/drafts/draft-1.md")
    archive.close()


def test_pending_replies_marks_missing_draft(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    archive.upsert_chat(_chat())
    archive.upsert_message(_message())
    store = MessageStore(tmp_path)

    rows = pending_replies(archive, store, limit=5)

    assert len(rows) == 1
    assert rows[0]["draft_status"] == "missing"
    assert rows[0]["proposed_text"] == ""
    archive.close()


def test_pending_replies_reports_outbox_artifact(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    archive.upsert_chat(_chat())
    archive.upsert_message(_message())
    store = MessageStore(tmp_path)
    draft = _draft(approved=True)
    store.write_draft(draft)
    store.move_draft_to_outbox(draft)

    rows = pending_replies(archive, store, limit=5)

    assert len(rows) == 1
    assert rows[0]["draft_status"] == "outbox"
    assert rows[0]["draft_uuid"] == "draft-1"
    assert rows[0]["proposed_text"] == "Yep, I can help with that."
    archive.close()
