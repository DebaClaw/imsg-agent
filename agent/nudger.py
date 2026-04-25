"""
nudger.py - Surface quiet conversations without drafting replies.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .store import MessageStore

logger = logging.getLogger(__name__)


class Nudger:
    def __init__(
        self,
        store: MessageStore,
        *,
        quiet_after_hours: int = 72,
        now: datetime | None = None,
    ) -> None:
        self._store = store
        self._quiet_after = timedelta(hours=quiet_after_hours)
        self._now = now

    def run_pass(self) -> int:
        written = 0
        now = self._current_time()
        nudge_date = now.date().isoformat()
        for chat_id in self._store.list_chat_ids():
            context = self._store.read_chat_context(chat_id)
            if bool(context.get("do_not_nudge")):
                continue
            last_active = _parse_dt(context.get("last_active"))
            if last_active is None or now - last_active < self._quiet_after:
                continue
            if not _last_message_needs_reply(self._store.read_chat_history(chat_id)):
                continue
            if self._store.nudge_exists(chat_id=chat_id, nudge_date=nudge_date):
                continue
            name = str(context.get("name") or f"chat {chat_id}")
            body = (
                f"# Follow up with {name}\n\n"
                f"Last active: {last_active.isoformat()}\n\n"
                "The most recent message appears to be from them, and the conversation "
                "has been quiet longer than the configured threshold."
            )
            self._store.write_nudge(
                chat_id=chat_id,
                nudge_date=nudge_date,
                reason="quiet_unanswered_conversation",
                body=body,
            )
            written += 1
            logger.info("Wrote nudge for chat_id=%d", chat_id)
        return written

    def _current_time(self) -> datetime:
        return (self._now or datetime.now(UTC)).astimezone(UTC)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _last_message_needs_reply(history: str) -> bool:
    entries = [entry for entry in history.split("<!-- rowid:") if entry.strip()]
    if not entries:
        return False
    last = entries[-1]
    return "**← " in last
