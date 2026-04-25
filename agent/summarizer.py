"""
summarizer.py - Write lightweight daily digests from the markdown store.
"""
from __future__ import annotations

from datetime import UTC, datetime

from .store import MessageStore


class Summarizer:
    def __init__(self, store: MessageStore, *, now: datetime | None = None) -> None:
        self._store = store
        self._now = now

    def write_daily_digest(self) -> int:
        now = (self._now or datetime.now(UTC)).astimezone(UTC)
        digest_date = now.date().isoformat()
        sections = [f"# Conversation Digest - {digest_date}"]
        count = 0
        for chat_id in self._store.list_chat_ids():
            context, notes = self._store.read_chat_context_document(chat_id)
            history = self._store.read_chat_history(chat_id).strip()
            if not history:
                continue
            name = str(context.get("name") or f"chat {chat_id}")
            last_active = str(context.get("last_active") or "unknown")
            sections.append(
                "\n".join(
                    [
                        f"## {name}",
                        f"- chat_id: {chat_id}",
                        f"- last_active: {last_active}",
                        f"- relationship: {context.get('relationship') or 'unknown'}",
                        f"- notes: {notes.strip() or 'none'}",
                        "",
                        "Recent history:",
                        history[-1200:],
                    ]
                )
            )
            count += 1
        self._store.write_digest(digest_date=digest_date, body="\n\n".join(sections) + "\n")
        return count
