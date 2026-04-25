from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agent.models import Message
from agent.store import MessageStore, _parse_frontmatter
from agent.summarizer import Summarizer

NOW = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def test_summarizer_writes_daily_digest(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.write_chat_context(
        7,
        {
            "chat_id": 7,
            "name": "Alex",
            "relationship": "close friend",
            "last_active": NOW.isoformat(),
            "notes": "Likes direct planning.",
        },
    )
    store.append_chat_history(
        7,
        Message(
            rowid=1,
            chat_id=7,
            guid="GUID-1",
            sender="+14155550101",
            text="Still on for Thursday?",
            date=NOW,
            is_from_me=False,
            service="iMessage",
            has_attachments=False,
        ),
    )

    count = Summarizer(store, now=NOW).write_daily_digest()

    assert count == 1
    path = tmp_path / "digests" / "2026-04-25.md"
    meta, body = _parse_frontmatter(path.read_text())
    assert meta["date"] == "2026-04-25"
    assert "Alex" in body
    assert "Still on for Thursday?" in body
