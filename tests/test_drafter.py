from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.drafter import Drafter, DraftResponse, _parse_model_json
from agent.models import Message
from agent.store import MessageStore, _parse_frontmatter

NOW = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


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
            proposed_text="Yes, still on for Thursday.",
            reasoning="They asked for a simple confirmation.",
        )


def _msg(
    rowid: int = 1,
    chat_id: int = 7,
    text: str = "Are we still on for Thursday?",
    *,
    date: datetime = NOW,
    is_from_me: bool = False,
) -> Message:
    return Message(
        rowid=rowid,
        chat_id=chat_id,
        guid=f"GUID-{rowid}",
        sender="+14155550101",
        text=text,
        date=date,
        is_from_me=is_from_me,
        service="iMessage",
        has_attachments=False,
    )


def _seed_chat(store: MessageStore, chat_id: int = 7, **context: object) -> None:
    base = {
        "chat_id": chat_id,
        "name": "Alex",
        "identifier": "iMessage;-;+14155550101",
        "relationship": "close friend",
        "tone": "casual and warm",
        "professional": False,
        "auto_approve": False,
        "notes": "Alex prefers direct plans.",
    }
    base.update(context)
    store.write_chat_context(chat_id, base)
    store.append_chat_history(chat_id, _msg(rowid=99, chat_id=chat_id, text="Previous text"))


@pytest.mark.asyncio
async def test_process_inbox_writes_unapproved_draft(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _seed_chat(store)
    store.write_inbox(_msg())
    client = FakeDraftingClient()

    draft = await Drafter(store, client, default_model="gpt-5.5", now=NOW).process_inbox(
        store.inbox_path(1, 7)
    )

    assert draft is not None
    assert draft.approved is False
    assert draft.model == "gpt-5.5"
    assert draft.source_rowid == 1
    path = tmp_path / "chats" / "7" / "drafts" / f"{draft.uuid}.md"
    meta, body = _parse_frontmatter(path.read_text())
    assert meta["approved"] is False
    assert meta["reasoning"] == "They asked for a simple confirmation."
    assert body.strip() == "Yes, still on for Thursday."


@pytest.mark.asyncio
async def test_context_isolation_only_reads_target_chat(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _seed_chat(store, chat_id=7, notes="Alex context only.")
    _seed_chat(store, chat_id=8, name="Morgan", notes="SECRET OTHER CHAT")
    store.write_inbox(_msg(chat_id=7))
    client = FakeDraftingClient()

    await Drafter(store, client, now=NOW).process_inbox(store.inbox_path(1, 7))

    assert len(client.calls) == 1
    assert "Alex context only." in client.calls[0]["input_text"]
    assert "SECRET OTHER CHAT" not in client.calls[0]["input_text"]


@pytest.mark.asyncio
async def test_do_not_draft_skips_api_call(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _seed_chat(store, do_not_draft=True)
    store.write_inbox(_msg())
    client = FakeDraftingClient()

    draft = await Drafter(store, client, now=NOW).process_inbox(store.inbox_path(1, 7))

    assert draft is None
    assert client.calls == []


@pytest.mark.asyncio
async def test_old_inbox_message_skips_api_call(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _seed_chat(store)
    store.write_inbox(_msg(date=NOW - timedelta(hours=50)))
    client = FakeDraftingClient()

    draft = await Drafter(
        store,
        client,
        max_inbox_age_hours=48,
        now=NOW,
    ).process_inbox(store.inbox_path(1, 7))

    assert draft is None
    assert client.calls == []


@pytest.mark.asyncio
async def test_auto_approval_requires_non_professional_chat(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _seed_chat(store, auto_approve=True, professional=False)
    store.write_inbox(_msg())
    client = FakeDraftingClient()

    draft = await Drafter(store, client, now=NOW).process_inbox(store.inbox_path(1, 7))

    assert draft is not None
    assert draft.approved is True
    assert draft.auto_approved is True


@pytest.mark.asyncio
async def test_auto_approval_blocked_when_professional_unknown(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _seed_chat(store, auto_approve=True)
    ctx = store.read_chat_context(7)
    ctx.pop("professional", None)
    store.write_chat_context(7, ctx)
    store.write_inbox(_msg())
    client = FakeDraftingClient()

    draft = await Drafter(store, client, now=NOW).process_inbox(store.inbox_path(1, 7))

    assert draft is not None
    assert draft.approved is False


@pytest.mark.asyncio
async def test_group_chat_requires_explicit_drafting_opt_in(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _seed_chat(
        store,
        participants=["+14155550101", "+14155550102"],
        auto_approve=True,
        professional=False,
    )
    store.write_inbox(_msg())
    client = FakeDraftingClient()

    draft = await Drafter(store, client, now=NOW).process_inbox(store.inbox_path(1, 7))

    assert draft is None
    assert client.calls == []


@pytest.mark.asyncio
async def test_group_chat_can_draft_with_do_not_draft_false_but_not_auto_approve(
    tmp_path: Path,
) -> None:
    store = MessageStore(tmp_path)
    _seed_chat(
        store,
        participants=["+14155550101", "+14155550102"],
        auto_approve=True,
        do_not_draft=False,
        professional=False,
    )
    store.write_inbox(_msg())
    client = FakeDraftingClient()

    draft = await Drafter(store, client, now=NOW).process_inbox(store.inbox_path(1, 7))

    assert draft is not None
    assert draft.approved is False


@pytest.mark.asyncio
async def test_existing_draft_for_source_prevents_duplicate(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _seed_chat(store)
    store.write_inbox(_msg())
    client = FakeDraftingClient()
    drafter = Drafter(store, client, now=NOW)

    first = await drafter.process_inbox(store.inbox_path(1, 7))
    second = await drafter.process_inbox(store.inbox_path(1, 7))

    assert first is not None
    assert second is None
    assert len(client.calls) == 1


def test_parse_model_json() -> None:
    parsed = _parse_model_json(
        '{"proposed_text": "Sounds good", "reasoning": "Simple acknowledgement"}'
    )

    assert parsed.proposed_text == "Sounds good"
    assert parsed.reasoning == "Simple acknowledgement"
