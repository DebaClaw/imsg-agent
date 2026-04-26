from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from agent.archive_store import IMessageArchive
from agent.contact_enrichment import contacts_from_json
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
    assert attachment["archived"] == 0
    assert attachment["local_path"] == ""
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


def test_archive_copies_attachment_file(tmp_path: Path) -> None:
    source = tmp_path / "source image.jpg"
    source.write_bytes(b"image-data")
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    message = _message()
    message.attachments[0].original_path = str(source)
    message.attachments[0].transfer_name = "source image.jpg"

    archive.upsert_message(message)

    db = sqlite3.connect(archive.path)
    db.row_factory = sqlite3.Row
    attachment = db.execute("SELECT * FROM attachments").fetchone()
    local_path = Path(str(attachment["local_path"]))

    assert archive.count_saved_attachments() == 1
    assert attachment["archived"] == 1
    assert attachment["archive_error"] == ""
    assert local_path.exists()
    assert local_path.read_bytes() == b"image-data"
    assert local_path.parent == tmp_path / "attachments" / str(message.rowid)
    archive.close()
    db.close()


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


def test_archive_syncs_contacts_and_enriches_chats(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    archive.upsert_chat(_chat())
    contacts = contacts_from_json(
        [
            {
                "id": "contact-1",
                "fullName": "Alex Example",
                "phones": [{"value": "+14155550101", "type": "mobile"}],
            }
        ]
    )

    sync_result = archive.replace_contacts(contacts)
    enrich_result = archive.enrich_chat_contacts()

    assert sync_result.contacts == 1
    assert sync_result.contact_points == 1
    assert archive.count_contacts() == 1
    assert archive.count_contact_points() == 1
    assert enrich_result.chats == 1
    assert enrich_result.matched == 1
    assert archive.count_chat_contact_matches("matched") == 1

    db = sqlite3.connect(archive.path)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM chat_contact_matches").fetchone()
    assert row["chat_id"] == 7
    assert row["contact_id"] == "contact-1"
    assert row["matched_value"] == "+14155550101"
    archive.close()
    db.close()


def test_archive_records_ambiguous_and_unresolved_contact_matches(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    archive.upsert_chat(_chat())
    archive.upsert_chat(
        Chat(
            id=8,
            identifier="iMessage;-;+15551234567",
            name="Unknown",
            service="iMessage",
            last_message_at=NOW,
        )
    )
    contacts = contacts_from_json(
        [
            {
                "id": "contact-1",
                "fullName": "Alex One",
                "phones": [{"value": "+14155550101"}],
            },
            {
                "id": "contact-2",
                "fullName": "Alex Two",
                "phones": [{"value": "+14155550101"}],
            },
        ]
    )

    archive.replace_contacts(contacts)
    result = archive.enrich_chat_contacts()

    assert result.ambiguous == 1
    assert result.unresolved == 1
    assert archive.count_chat_contact_matches("ambiguous") == 2
    assert archive.count_chat_contact_matches("unresolved") == 1
    archive.close()
