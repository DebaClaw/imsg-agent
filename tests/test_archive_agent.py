from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.archive_agent import ArchiveAgentWorker
from agent.archive_store import IMessageArchive
from agent.drafter import Drafter, DraftResponse
from agent.models import Chat, Message
from agent.store import MessageStore

NOW = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)


class FakeDraftingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def create_draft(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
    ) -> DraftResponse:
        self.calls.append(
            {"model": model, "instructions": instructions, "input_text": input_text}
        )
        return DraftResponse(
            proposed_text="Yep, I can do that.",
            reasoning="They asked a straightforward question.",
        )


def _chat(*, is_group: bool = False) -> Chat:
    participants = ["+18015550101", "+18015550102"] if is_group else ["+18015550101"]
    return Chat(
        id=7,
        identifier=";+;" if is_group else "iMessage;-;+18015550101",
        name="Alex",
        service="iMessage",
        last_message_at=NOW,
        participants=participants,
        is_group=is_group,
    )


def _message(rowid: int = 100, *, is_from_me: bool = False) -> Message:
    return Message(
        rowid=rowid,
        chat_id=7,
        guid=f"GUID-{rowid}",
        sender="+18015550101",
        text="Can you help with this?",
        date=NOW,
        is_from_me=is_from_me,
        service="iMessage",
        has_attachments=False,
        chat_name="Alex",
        participants=["+18015550101"],
    )


@pytest.mark.asyncio
async def test_archive_agent_worker_drafts_from_sqlite_archive(tmp_path: Path) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    archive.upsert_chat(_chat())
    archive.upsert_message(_message(rowid=99, is_from_me=True))
    archive.upsert_message(_message(rowid=100))
    store = MessageStore(tmp_path / "data")
    store.write_chat_context(
        7,
        {
            "chat_id": 7,
            "name": "Alex",
            "identifier": "iMessage;-;+18015550101",
            "professional": False,
            "notes": "Alex likes concise replies.",
        },
    )
    client = FakeDraftingClient()
    drafter = Drafter(store, client, now=NOW)
    worker = ArchiveAgentWorker(
        archive=archive,
        store=store,
        drafter=drafter,
        history_limit=20,
    )

    processed = await worker.run_once()

    assert processed == 1
    assert archive.read_agent_cursor() == 100
    assert len(client.calls) == 1
    assert "Alex likes concise replies." in client.calls[0]["input_text"]
    assert "Can you help with this?" in client.calls[0]["input_text"]
    assert store.draft_exists_for_source(7, 100)
    archive.close()


@pytest.mark.asyncio
async def test_archive_agent_worker_defaults_group_chats_to_no_draft(
    tmp_path: Path,
) -> None:
    archive = IMessageArchive(tmp_path / "imessage.sqlite")
    archive.upsert_chat(_chat(is_group=True))
    archive.upsert_message(_message())
    store = MessageStore(tmp_path / "data")
    client = FakeDraftingClient()
    drafter = Drafter(store, client, now=NOW)
    worker = ArchiveAgentWorker(
        archive=archive,
        store=store,
        drafter=drafter,
        history_limit=20,
    )

    processed = await worker.run_once()

    assert processed == 1
    assert archive.read_agent_cursor() == 100
    assert client.calls == []
    assert store.read_chat_context(7)["do_not_draft"] is True
    archive.close()
