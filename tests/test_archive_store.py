from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from agent.archive_store import IMessageArchive
from agent.models import Attachment, Chat, Message, Reaction

NOW = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _chat() -> Chat:
    return Chat(
        id=7,
        identifier="iMessage;-;+14155550101",
        name="Alex",
        service="iMessage",
        last_message_at=NOW,
        guid="chat-guid",
        participants=["+14155550101"],
    )


def _message(rowid: int = 123) -> Message:
    return Message(
        rowid=rowid,
        chat_id=7,
        guid=f"GUID-{rowid}",
        sender="+14155550101",
        text="Photo from today",
        date=NOW,
        is_from_me=False,
        service="iMessage",
        has_attachments=True,
        attachments=[
            Attachment(
                filename="~/Library/Messages/Attachments/photo.jpg",
                transfer_name="photo.jpg",
                uti="public.jpeg",
                mime_type="image/jpeg",
                total_bytes=2048,
                is_sticker=False,
                original_path="/Users/zob/Library/Messages/Attachments/photo.jpg",
                missing=False,
            )
        ],
        reactions=[
            Reaction(
                reaction_type="like",
                sender="+14155550102",
                is_from_me=False,
                date=NOW,
                emoji="thumbs up",
            )
        ],
        chat_identifier="iMessage;-;+14155550101",
        chat_guid="chat-guid",
        chat_name="Alex",
        participants=["+14155550101"],
    )


def test_archive_upserts_chat_message_attachment_and_reaction(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")

    archive.upsert_chat(_chat())
    archive.upsert_message(_message())

    assert archive.count_chats() == 1
    assert archive.count_messages() == 1
    assert archive.count_attachments() == 1
    assert archive.read_cursor() == 123

    db = sqlite3.connect(archive.path)
    db.row_factory = sqlite3.Row
    attachment = db.execute("SELECT * FROM attachments").fetchone()
    reaction = db.execute("SELECT * FROM reactions").fetchone()

    assert attachment["transfer_name"] == "photo.jpg"
    assert attachment["original_path"].endswith("photo.jpg")
    assert reaction["reaction_type"] == "like"
    archive.close()
    db.close()


def test_archive_upsert_is_idempotent(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    message = _message()

    archive.upsert_message(message)
    archive.upsert_message(message)

    assert archive.count_messages() == 1
    assert archive.count_attachments() == 1
    archive.close()


def test_archive_reports_oldest_message_for_chat(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    newer = _message(rowid=200)
    older = _message(rowid=100)
    older.date = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)

    archive.upsert_message(newer)
    archive.upsert_message(older)

    oldest = archive.oldest_message_for_chat(7)

    assert archive.count_messages_for_chat(7) == 2
    assert oldest == (100, older.date)
    assert archive.oldest_message_for_chat(999) is None
    archive.close()
