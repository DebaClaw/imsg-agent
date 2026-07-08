from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.archive_store import IMessageArchive
from agent.config import Config
from agent.models import Chat, Draft, Message
from agent.store import MessageStore
from agent.web_app import WebAPIError, WebService

NOW = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)


def _config(tmp_path: Path, *, openai_api_key: str | None = None) -> Config:
    return Config(
        imsg_binary=Path("imsg"),
        data_dir=tmp_path,
        rpc_timeout_seconds=30,
        rpc_read_limit_bytes=1024,
        watch_debounce_ms=250,
        history_limit=50,
        chat_context_messages=20,
        auto_approve=False,
        default_service="auto",
        max_inbox_age_hours=48,
        openai_api_key=openai_api_key,
        draft_model="gpt-5.5",
        maintenance_interval_seconds=5.0,
        nudge_after_hours=72,
    )


def _service(tmp_path: Path) -> WebService:
    return WebService(
        config=_config(tmp_path),
        data_dir=tmp_path,
        db_path=tmp_path / "imessage.sqlite",
    )


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


def _seed_archive(tmp_path: Path) -> None:
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.upsert_chat(_chat())
        archive.upsert_message(_message())


def test_web_pending_reads_archive_and_draft_artifact(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    MessageStore(tmp_path).write_draft(_draft())

    rows = _service(tmp_path).pending(limit=5)

    assert len(rows) == 1
    assert rows[0]["chat_id"] == 7
    assert rows[0]["draft_status"] == "draft_unapproved"
    assert rows[0]["proposed_text"] == "Yep, I can help with that."


def test_web_chat_returns_context_and_messages(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    MessageStore(tmp_path).write_chat_context(
        7,
        {
            "chat_id": 7,
            "relationship": "close friend",
            "tone": "warm",
            "professional": False,
            "notes": "Met through the hiking group.",
        },
    )

    payload = _service(tmp_path).chat(7)

    assert payload["context"]["relationship"] == "close friend"
    assert payload["notes"] == "Met through the hiking group."
    assert payload["messages"][0]["text"] == "Can you help with this?"


def test_web_edit_draft_keeps_it_unapproved(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.write_draft(_draft(approved=True))

    payload = _service(tmp_path).edit_draft("draft-1", text="Updated reply.")
    draft = store.read_draft(tmp_path / "chats" / "7" / "drafts" / "draft-1.md")

    assert payload["status"] == "edited"
    assert draft is not None
    assert draft.proposed_text == "Updated reply."
    assert draft.approved is False


def test_web_approve_moves_draft_to_outbox_without_sending(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.write_chat_context(7, {"chat_id": 7, "professional": True})
    store.write_draft(_draft())

    payload = _service(tmp_path).approve_draft("draft-1", text="Approved reply.")
    outbox = store.list_outbox()
    item = store.read_outbox_item(outbox[0])

    assert payload["status"] == "queued"
    assert payload["sent"] is False
    assert payload["professional"] is True
    assert item is not None
    assert item.text == "Approved reply."
    assert not (tmp_path / "chats" / "7" / "drafts" / "draft-1.md").exists()
    assert not (tmp_path / "sent" / "draft-1.md").exists()


def test_web_request_draft_requires_api_key(tmp_path: Path) -> None:
    _seed_archive(tmp_path)

    with pytest.raises(WebAPIError, match="OPENAI_API_KEY"):
        _service(tmp_path).request_draft(message_rowid=100)


def test_web_reject_draft_records_no_reply_decision(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    store = MessageStore(tmp_path)
    store.write_draft(_draft())

    payload = _service(tmp_path).reject_draft("draft-1", reasoning="Already handled.")

    rows = _service(tmp_path).pending(limit=5)
    assert payload["status"] == "rejected"
    assert rows == []
    assert not (tmp_path / "chats" / "7" / "drafts" / "draft-1.md").exists()
    assert list((tmp_path / "no_reply").glob("*.md"))


def test_web_mark_no_reply_hides_missing_pending_item(tmp_path: Path) -> None:
    _seed_archive(tmp_path)

    payload = _service(tmp_path).mark_no_reply(
        chat_id=7,
        source_rowid=100,
        reasoning="No response needed.",
    )

    assert payload["status"] == "no_reply_recorded"
    assert _service(tmp_path).pending(limit=5) == []


def test_web_update_chat_context_persists_fields_and_notes(tmp_path: Path) -> None:
    _seed_archive(tmp_path)

    payload = _service(tmp_path).update_chat_context(
        7,
        fields={
            "relationship": "close friend",
            "tone": "warm",
            "professional": False,
            "ignored": "nope",
        },
        notes="Met through the hiking group.",
    )

    context, notes = MessageStore(tmp_path).read_chat_context_document(7)
    assert payload["status"] == "saved"
    assert context["relationship"] == "close friend"
    assert context["tone"] == "warm"
    assert context["professional"] is False
    assert "ignored" not in context
    assert notes == "Met through the hiking group."


def test_web_update_chat_context_seeds_archive_identity_without_existing_context(
    tmp_path: Path,
) -> None:
    _seed_archive(tmp_path)

    payload = _service(tmp_path).update_chat_context(
        7,
        fields={
            "relationship": "friend",
            "tone": "casual",
            "professional": False,
            "auto_approve": False,
            "do_not_draft": False,
        },
        notes="",
    )

    context, notes = MessageStore(tmp_path).read_chat_context_document(7)
    assert payload["status"] == "saved"
    assert context["chat_id"] == 7
    assert context["name"] == "Alex"
    assert context["identifier"] == "iMessage;-;+18015550101"
    assert context["service"] == "iMessage"
    assert context["participants"] == ["+18015550101"]
    assert context["relationship"] == "friend"
    assert context["tone"] == "casual"
    assert context["professional"] is False
    assert context["auto_approve"] is False
    assert context["do_not_draft"] is False
    assert notes == ""


def test_web_update_chat_context_preserves_notes_when_notes_omitted(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    MessageStore(tmp_path).write_chat_context(
        7,
        {
            "chat_id": 7,
            "relationship": "friend",
            "notes": "Keep this note.",
        },
    )

    _service(tmp_path).update_chat_context(7, fields={"tone": "warmer"})

    context, notes = MessageStore(tmp_path).read_chat_context_document(7)
    assert context["relationship"] == "friend"
    assert context["tone"] == "warmer"
    assert notes == "Keep this note."
