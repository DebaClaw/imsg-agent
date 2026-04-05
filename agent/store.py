"""
store.py — All reads and writes to ~/imsg-data/.

Single source of truth for filesystem I/O. No network calls, no subprocess calls.
All writes are atomic: data is written to a .tmp file then renamed in place,
so a crash mid-write never leaves a partial file.

Directory layout:
    {root}/state.json
    {root}/inbox/{rowid}-{chat_id}.md
    {root}/chats/{chat_id}/context.md
    {root}/chats/{chat_id}/history.md
    {root}/chats/{chat_id}/drafts/{uuid}.md
    {root}/outbox/{uuid}.md
    {root}/sent/{uuid}.md
    {root}/errors/{uuid}.md
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .models import Draft, Message, OutboxItem

logger = logging.getLogger(__name__)

_HISTORY_SEPARATOR = "<!-- rowid:"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """
    Split a markdown file into (frontmatter_dict, body_string).
    Returns ({}, text) if no frontmatter is present.
    """
    if not text.startswith("---\n"):
        return {}, text
    try:
        end = text.index("\n---\n", 4)
    except ValueError:
        return {}, text
    meta: dict[str, Any] = yaml.safe_load(text[4:end]) or {}
    body = text[end + 5:]
    return meta, body


def _write_frontmatter(meta: dict[str, Any], body: str) -> str:
    """Serialize frontmatter + body to a markdown string."""
    return f"---\n{yaml.dump(meta, default_flow_style=False, allow_unicode=True)}---\n{body}"


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via a .tmp sibling rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


# ---------------------------------------------------------------------------
# MessageStore
# ---------------------------------------------------------------------------


class MessageStore:
    """Read/write interface for the ~/imsg-data/ directory tree."""

    def __init__(self, data_dir: Path) -> None:
        self._root = Path(data_dir).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # State / Cursor
    # ------------------------------------------------------------------

    def read_cursor(self) -> int:
        """Return the last processed rowid, or 0 if no state exists."""
        path = self._root / "state.json"
        if not path.exists():
            return 0
        try:
            return int(json.loads(path.read_text(encoding="utf-8"))["cursor"])
        except (KeyError, ValueError, json.JSONDecodeError, OSError):
            return 0

    def write_cursor(self, rowid: int) -> None:
        """Atomically persist the cursor rowid."""
        _atomic_write(self._root / "state.json", json.dumps({"cursor": rowid}))

    # ------------------------------------------------------------------
    # Inbox
    # ------------------------------------------------------------------

    def inbox_path(self, rowid: int, chat_id: int) -> Path:
        return self._root / "inbox" / f"{rowid}-{chat_id}.md"

    def inbox_exists(self, rowid: int, chat_id: int) -> bool:
        return self.inbox_path(rowid, chat_id).exists()

    def write_inbox(self, message: Message) -> None:
        """Write a new message to inbox/. Caller should check inbox_exists first."""
        meta: dict[str, Any] = {
            "rowid": message.rowid,
            "chat_id": message.chat_id,
            "guid": message.guid,
            "sender": message.sender,
            "date": _fmt_dt(message.date),
            "is_from_me": message.is_from_me,
            "service": message.service,
            "has_attachments": message.has_attachments,
        }
        if message.reply_to_guid:
            meta["reply_to_guid"] = message.reply_to_guid
        if message.thread_originator_guid:
            meta["thread_originator_guid"] = message.thread_originator_guid
        if message.destination_caller_id:
            meta["destination_caller_id"] = message.destination_caller_id
        if message.is_reaction:
            meta["is_reaction"] = True
            if message.reaction_type:
                meta["reaction_type"] = message.reaction_type
        _atomic_write(
            self.inbox_path(message.rowid, message.chat_id),
            _write_frontmatter(meta, message.text),
        )

    def list_unprocessed_inbox(self) -> list[Path]:
        """Return all inbox .md files sorted by filename (rowid-chatid order)."""
        inbox_dir = self._root / "inbox"
        if not inbox_dir.exists():
            return []
        return sorted(inbox_dir.glob("*.md"))

    def read_inbox_message(self, path: Path) -> Message | None:
        """Parse an inbox .md file back into a Message. Returns None on parse error."""
        try:
            meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
            return Message(
                rowid=int(meta["rowid"]),
                chat_id=int(meta["chat_id"]),
                guid=meta.get("guid") or "",
                sender=meta.get("sender") or "",
                text=body.strip(),
                date=_parse_dt(meta.get("date")),
                is_from_me=bool(meta.get("is_from_me")),
                service=meta.get("service") or "",
                has_attachments=bool(meta.get("has_attachments")),
                reply_to_guid=meta.get("reply_to_guid") or None,
                thread_originator_guid=meta.get("thread_originator_guid") or None,
                destination_caller_id=meta.get("destination_caller_id") or None,
                is_reaction=bool(meta.get("is_reaction")),
                reaction_type=meta.get("reaction_type") or None,
            )
        except Exception as exc:
            logger.warning("Failed to parse inbox file %s: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # Chat context
    # ------------------------------------------------------------------

    def _chat_dir(self, chat_id: int) -> Path:
        return self._root / "chats" / str(chat_id)

    def read_chat_context(self, chat_id: int) -> dict[str, Any]:
        """Return parsed frontmatter from chats/{id}/context.md, or {} if absent."""
        path = self._chat_dir(chat_id) / "context.md"
        if not path.exists():
            return {}
        try:
            meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
            return meta
        except Exception as exc:
            logger.warning("Failed to read chat context for %d: %s", chat_id, exc)
            return {}

    def write_chat_context(self, chat_id: int, context: dict[str, Any]) -> None:
        """
        Write chats/{id}/context.md.

        The optional 'notes' key is written as the file body (freeform text);
        all other keys go into the YAML frontmatter. The original dict is not mutated.
        """
        notes = context.get("notes", "")
        meta = {k: v for k, v in context.items() if k != "notes"}
        _atomic_write(
            self._chat_dir(chat_id) / "context.md",
            _write_frontmatter(meta, str(notes)),
        )

    def append_chat_history(
        self, chat_id: int, message: Message, max_messages: int = 20
    ) -> None:
        """
        Append message to chats/{id}/history.md and trim to max_messages entries.

        History entries are separated by HTML comment markers so they can be
        counted and trimmed without a full parse.
        """
        path = self._chat_dir(chat_id) / "history.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""

        direction = "→ me" if message.is_from_me else f"← {message.sender}"
        timestamp = _fmt_dt(message.date)
        body_text = message.text or "_(no text)_"
        new_entry = (
            f"{_HISTORY_SEPARATOR}{message.rowid} -->\n"
            f"**{direction}** _{timestamp}_\n\n"
            f"{body_text}\n\n"
        )

        combined = existing + new_entry

        # Trim to max_messages by counting separator tokens
        parts = combined.split(_HISTORY_SEPARATOR)
        if len(parts) > max_messages + 1:
            # parts[0] is the header (possibly empty), parts[1:] are entries
            parts = parts[:1] + parts[-(max_messages):]
        trimmed = _HISTORY_SEPARATOR.join(parts)

        _atomic_write(path, trimmed)

    def read_chat_history(self, chat_id: int) -> str:
        """Return the raw history file contents, or empty string if absent."""
        path = self._chat_dir(chat_id) / "history.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Drafts
    # ------------------------------------------------------------------

    def write_draft(self, draft: Draft) -> None:
        """Write a draft to chats/{id}/drafts/{uuid}.md."""
        meta: dict[str, Any] = {
            "uuid": draft.uuid,
            "chat_id": draft.chat_id,
            "target_identifier": draft.target_identifier,
            "created_at": _fmt_dt(draft.created_at),
            "approved": draft.approved,
            "prompt_version": draft.prompt_version,
            "reasoning": draft.reasoning,
        }
        if draft.source_rowid is not None:
            meta["source_rowid"] = draft.source_rowid
        _atomic_write(
            self._chat_dir(draft.chat_id) / "drafts" / f"{draft.uuid}.md",
            _write_frontmatter(meta, draft.proposed_text),
        )

    def list_approved_drafts(self) -> list[Path]:
        """Return paths of all draft files where approved: true."""
        chats_dir = self._root / "chats"
        if not chats_dir.exists():
            return []
        approved = []
        for draft_file in sorted(chats_dir.glob("*/drafts/*.md")):
            try:
                meta, _ = _parse_frontmatter(draft_file.read_text(encoding="utf-8"))
                if meta.get("approved"):
                    approved.append(draft_file)
            except Exception:
                continue
        return approved

    def read_draft(self, path: Path) -> Draft | None:
        """Parse a draft .md file. Returns None on error."""
        try:
            meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
            return Draft(
                uuid=str(meta["uuid"]),
                chat_id=int(meta["chat_id"]),
                target_identifier=meta.get("target_identifier") or "",
                created_at=_parse_dt(meta.get("created_at")),
                proposed_text=body.strip(),
                reasoning=meta.get("reasoning") or "",
                prompt_version=meta.get("prompt_version") or "v1",
                approved=bool(meta.get("approved")),
                source_rowid=meta.get("source_rowid"),
            )
        except Exception as exc:
            logger.warning("Failed to parse draft %s: %s", path, exc)
            return None

    def move_draft_to_outbox(self, draft: Draft) -> None:
        """Move an approved draft from chats/{id}/drafts/ to outbox/."""
        src = self._chat_dir(draft.chat_id) / "drafts" / f"{draft.uuid}.md"
        dst = self._root / "outbox" / f"{draft.uuid}.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    # ------------------------------------------------------------------
    # Outbox / Sent / Errors
    # ------------------------------------------------------------------

    def list_outbox(self) -> list[Path]:
        """Return all .md files in outbox/, sorted by name."""
        outbox = self._root / "outbox"
        if not outbox.exists():
            return []
        return sorted(p for p in outbox.glob("*.md") if not p.name.startswith("."))

    def read_outbox_item(self, path: Path) -> OutboxItem | None:
        """Parse an outbox .md file. Returns None on error."""
        try:
            meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
            return OutboxItem(
                uuid=str(meta["uuid"]),
                chat_id=int(meta["chat_id"]),
                target_identifier=meta.get("target_identifier") or "",
                text=body.strip(),
                attachment_path=meta.get("attachment_path") or None,
                created_at=_parse_dt(meta.get("created_at")),
                source_draft_uuid=meta.get("source_draft_uuid") or None,
            )
        except Exception as exc:
            logger.warning("Failed to parse outbox item %s: %s", path, exc)
            return None

    def move_to_sent(self, item: OutboxItem, sent_at: datetime | None = None) -> None:
        """Archive a sent outbox item to sent/."""
        meta: dict[str, Any] = {
            "uuid": item.uuid,
            "chat_id": item.chat_id,
            "sent_at": _fmt_dt(sent_at or datetime.now(timezone.utc)),
        }
        if item.source_draft_uuid:
            meta["source_draft_uuid"] = item.source_draft_uuid
        dst = self._root / "sent" / f"{item.uuid}.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(dst, _write_frontmatter(meta, item.text))
        src = self._root / "outbox" / f"{item.uuid}.md"
        if src.exists():
            src.unlink()

    def move_to_errors(self, item: OutboxItem, reason: str) -> None:
        """Archive a failed outbox item to errors/ with the failure reason."""
        meta: dict[str, Any] = {
            "uuid": item.uuid,
            "chat_id": item.chat_id,
            "error": reason,
            "failed_at": _fmt_dt(datetime.now(timezone.utc)),
        }
        dst = self._root / "errors" / f"{item.uuid}.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(dst, _write_frontmatter(meta, item.text))
        src = self._root / "outbox" / f"{item.uuid}.md"
        if src.exists():
            src.unlink()
