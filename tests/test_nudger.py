from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.models import Message
from agent.nudger import Nudger
from agent.store import MessageStore, _parse_frontmatter

NOW = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _msg(rowid: int, *, is_from_me: bool = False) -> Message:
    return Message(
        rowid=rowid,
        chat_id=7,
        guid=f"GUID-{rowid}",
        sender="+14155550101",
        text="Checking in",
        date=NOW - timedelta(hours=80),
        is_from_me=is_from_me,
        service="iMessage",
        has_attachments=False,
    )


def test_nudger_writes_notice_for_quiet_unanswered_chat(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.write_chat_context(
        7,
        {
            "chat_id": 7,
            "name": "Alex",
            "last_active": (NOW - timedelta(hours=80)).isoformat(),
        },
    )
    store.append_chat_history(7, _msg(1))

    count = Nudger(store, quiet_after_hours=72, now=NOW).run_pass()

    assert count == 1
    path = tmp_path / "nudges" / "2026-04-25-7.md"
    meta, body = _parse_frontmatter(path.read_text())
    assert meta["reason"] == "quiet_unanswered_conversation"
    assert "Follow up with Alex" in body


def test_nudger_does_not_repeat_same_day(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.write_chat_context(
        7,
        {"chat_id": 7, "last_active": (NOW - timedelta(hours=80)).isoformat()},
    )
    store.append_chat_history(7, _msg(1))
    nudger = Nudger(store, quiet_after_hours=72, now=NOW)

    assert nudger.run_pass() == 1
    assert nudger.run_pass() == 0


def test_nudger_skips_when_operator_sent_last_message(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.write_chat_context(
        7,
        {"chat_id": 7, "last_active": (NOW - timedelta(hours=80)).isoformat()},
    )
    store.append_chat_history(7, _msg(1, is_from_me=True))

    count = Nudger(store, quiet_after_hours=72, now=NOW).run_pass()

    assert count == 0
    assert not (tmp_path / "nudges").exists()
