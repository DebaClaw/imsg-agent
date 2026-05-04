"""
pending_report.py - Read-only pending reply report.

The archive database answers "which latest inbound messages need attention"; the
Markdown store answers "what draft, if any, exists for that inbound message."
This module joins those two views without mutating either store.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, SupportsInt

from .archive_store import ArchiveRow, IMessageArchive
from .store import MessageStore, _parse_frontmatter


def pending_replies(
    archive: IMessageArchive,
    store: MessageStore,
    *,
    limit: int = 5,
    max_missing_age_hours: int | None = None,
    now: datetime | None = None,
) -> list[ArchiveRow]:
    """Return latest-inbound chats decorated with matching draft artifacts."""
    rows = archive.attention_items(limit=max(limit * 10, 50))
    pending = []
    for row in rows:
        decorated = _decorate_pending_row(row, store)
        if decorated.get("draft_status") == "no_reply_needed":
            continue
        if _is_stale_missing(
            decorated,
            max_missing_age_hours=max_missing_age_hours,
            now=now,
        ):
            continue
        pending.append(decorated)
        if len(pending) >= limit:
            break
    return pending


def _decorate_pending_row(row: ArchiveRow, store: MessageStore) -> ArchiveRow:
    chat_id = _int_or_none(row.get("chat_id"))
    source_rowid = _int_or_none(row.get("message_rowid"))
    artifact = (
        _find_reply_artifact(store, chat_id=chat_id, source_rowid=source_rowid)
        if chat_id is not None and source_rowid is not None
        else None
    )
    decorated = dict(row)
    if artifact is None:
        decorated.update(
            {
                "draft_status": "missing",
                "draft_uuid": "",
                "draft_path": "",
                "proposed_text": "",
                "reasoning": "",
            }
        )
        return decorated
    decorated.update(artifact)
    return decorated


def _find_reply_artifact(
    store: MessageStore,
    *,
    chat_id: int,
    source_rowid: int,
) -> ArchiveRow | None:
    candidates = [
        ("draft", store.data_dir / "chats" / str(chat_id) / "drafts"),
        ("outbox", store.data_dir / "outbox"),
        ("sent", store.data_dir / "sent"),
        ("error", store.data_dir / "errors"),
        ("no_reply_needed", store.data_dir / "no_reply"),
    ]
    for status, root in candidates:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.md")):
            artifact = _read_artifact(path, status=status)
            if artifact is None:
                continue
            if artifact.get("chat_id") != chat_id:
                continue
            if artifact.get("source_rowid") == source_rowid:
                return artifact
    return None


def _read_artifact(path: Path, *, status: str) -> ArchiveRow | None:
    try:
        meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    chat_id = _int_or_none(meta.get("chat_id"))
    source_rowid = _int_or_none(meta.get("source_rowid"))
    if chat_id is None or source_rowid is None:
        return None
    uuid = str(meta.get("uuid") or meta.get("source_draft_uuid") or path.stem)
    draft_status = status
    if status == "draft":
        draft_status = "draft_approved" if bool(meta.get("approved")) else "draft_unapproved"
    artifact: dict[str, Any] = {
        "draft_status": draft_status,
        "draft_uuid": uuid,
        "draft_path": str(path),
        "proposed_text": body.strip(),
        "reasoning": str(meta.get("reasoning") or ""),
        "chat_id": chat_id,
        "source_rowid": source_rowid,
    }
    if meta.get("error"):
        artifact["draft_error"] = str(meta["error"])
    return artifact


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str | bytes | bytearray | SupportsInt):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_stale_missing(
    row: ArchiveRow,
    *,
    max_missing_age_hours: int | None,
    now: datetime | None,
) -> bool:
    if max_missing_age_hours is None or max_missing_age_hours <= 0:
        return False
    if row.get("draft_status") != "missing":
        return False
    timestamp = row.get("last_message_at")
    if not isinstance(timestamp, str):
        return False
    try:
        message_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    current = (now or datetime.now(UTC)).astimezone(UTC)
    return current - message_at.astimezone(UTC) > timedelta(hours=max_missing_age_hours)
