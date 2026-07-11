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
    artifacts = reply_artifact_index(store)
    pending = []
    for row in rows:
        decorated = _decorate_pending_row(row, store, artifacts=artifacts)
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


def _decorate_pending_row(
    row: ArchiveRow,
    store: MessageStore,
    *,
    artifacts: dict[tuple[int, int], ArchiveRow] | None = None,
) -> ArchiveRow:
    chat_id = _int_or_none(row.get("chat_id"))
    source_rowid = _int_or_none(row.get("message_rowid"))
    artifact = None
    if chat_id is not None and source_rowid is not None:
        index = artifacts if artifacts is not None else reply_artifact_index(store)
        artifact = index.get((chat_id, source_rowid))
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
    return reply_artifact_index(store).get((chat_id, source_rowid))


def reply_artifact_index(store: MessageStore) -> dict[tuple[int, int], ArchiveRow]:
    """Read each approval artifact once, keyed by its triggering message."""
    indexed: dict[tuple[int, int], ArchiveRow] = {}
    candidates = [
        ("draft", store.data_dir / "chats", "*/drafts/*.md"),
        ("outbox", store.data_dir / "outbox", "*.md"),
        ("sent", store.data_dir / "sent", "*.md"),
        ("error", store.data_dir / "errors", "*.md"),
        ("no_reply_needed", store.data_dir / "no_reply", "*.md"),
        ("archived", store.data_dir / "draft_archive", "*.md"),
    ]
    for status, root, pattern in candidates:
        if not root.exists():
            continue
        for path in sorted(root.glob(pattern)):
            artifact = _read_artifact(path, status=status)
            if artifact is None:
                continue
            chat_id = _int_or_none(artifact.get("chat_id"))
            source_rowid = _int_or_none(artifact.get("source_rowid"))
            if chat_id is not None and source_rowid is not None:
                indexed.setdefault((chat_id, source_rowid), artifact)
    return indexed


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
