"""
operator_service.py - Shared operator-facing service layer.

This module holds the read/write operations used by management interfaces such as
the CLI, local web app, and a future TUI. It is intentionally local-first: archive
visibility comes from SQLite, and reply review actions mutate the existing Markdown
approval artifacts instead of sending messages directly.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import uuid4

from .archive_agent import ArchiveAgentWorker
from .archive_store import ArchiveRow, IMessageArchive
from .config import Config
from .drafter import Drafter, OpenAIResponsesDraftingClient
from .manage_cli import (
    archive_db_path,
    log_path,
    service_restart,
    service_start,
    service_status,
    service_stop,
)
from .models import Draft, Message
from .pending_report import _decorate_pending_row, pending_replies
from .store import MessageStore

JSON = dict[str, Any]
JSONList = list[JSON]


class OperatorServiceError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class OperatorService:
    def __init__(
        self,
        *,
        config: Config,
        data_dir: Path | None = None,
        db_path: Path | None = None,
    ) -> None:
        self.config = config
        self.data_dir = Path(data_dir or config.data_dir).expanduser()
        self.db_path = Path(db_path or archive_db_path(config)).expanduser()

    def status(self) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            stats = archive.archive_stats()
        return {
            "data_dir": str(self.data_dir),
            "db_path": str(self.db_path),
            "archive": stats,
            "services": [service_status("monitor"), service_status("worker")],
        }

    def overview(self, *, limit: int = 12) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            store = MessageStore(self.data_dir)
            return {
                "status": self.status(),
                "attention": archive.attention_items(limit=limit),
                "pending": pending_replies(
                    archive,
                    store,
                    limit=limit,
                    max_missing_age_hours=self.config.max_inbox_age_hours,
                ),
                "recent": archive.recent_chats(limit=limit),
                "views": self.saved_views(limit=limit),
            }

    def pending(self, *, limit: int = 20) -> JSONList:
        with IMessageArchive(self.db_path) as archive:
            return pending_replies(
                archive,
                MessageStore(self.data_dir),
                limit=limit,
                max_missing_age_hours=self.config.max_inbox_age_hours,
            )

    def attention(self, *, limit: int = 50) -> JSONList:
        with IMessageArchive(self.db_path) as archive:
            return archive.attention_items(limit=limit)

    def recent(self, *, limit: int = 50) -> JSONList:
        with IMessageArchive(self.db_path) as archive:
            return archive.recent_chats(limit=limit)

    def needs_reply(self, *, limit: int = 50) -> JSONList:
        with IMessageArchive(self.db_path) as archive:
            return archive.needs_reply(limit=limit)

    def issues(self, *, limit: int = 50) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            return {
                "unresolved_contacts": archive.unresolved_contact_chats(limit=limit),
                "attachment_issues": archive.attachment_issues(limit=limit),
            }

    def saved_views(self, *, limit: int = 30) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            attention = archive.attention_items(limit=limit)
            recent = archive.recent_chats(limit=limit)
            needs_reply = archive.needs_reply(limit=limit)
            quiet = [
                row
                for row in attention
                if float(str(row.get("hours_waiting") or 0)) >= self.config.nudge_after_hours
            ]
        return {
            "unanswered": needs_reply,
            "recently_active": recent,
            "quiet_relationships": quiet,
            "attachment_issues": self.issues(limit=limit)["attachment_issues"],
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        chat_id: int | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> JSONList:
        if not query.strip():
            return []
        with IMessageArchive(self.db_path) as archive:
            return archive.search_messages(
                query,
                limit=limit,
                chat_id=chat_id,
                since=since,
                until=until,
            )

    def chat(self, chat_id: int, *, limit: int = 80) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            messages = list(reversed(archive.chat_messages(chat_id, limit=limit)))
            seed = archive.chat_context_seed(chat_id)
        context, notes = MessageStore(self.data_dir).read_chat_context_document(chat_id)
        return {
            "chat": seed,
            "context": context,
            "notes": notes,
            "messages": messages,
        }

    def update_chat_context(
        self,
        chat_id: int,
        *,
        fields: dict[str, object],
        notes: str | None = None,
    ) -> JSON:
        store = MessageStore(self.data_dir)
        current, current_notes = store.read_chat_context_document(chat_id)
        allowed = {
            "relationship",
            "tone",
            "professional",
            "auto_approve",
            "do_not_draft",
            "agent_notes",
            "model",
            "name",
            "service",
            "participants",
        }
        updated = dict(current)
        for key, value in fields.items():
            if key in allowed:
                updated[key] = value
        updated.setdefault("chat_id", chat_id)
        updated["notes"] = current_notes if notes is None else notes
        store.write_chat_context(chat_id, updated)
        context, body = store.read_chat_context_document(chat_id)
        return {"status": "saved", "chat_id": chat_id, "context": context, "notes": body}

    def approve_draft(self, uuid: str, *, text: str | None = None) -> JSON:
        store = MessageStore(self.data_dir)
        path = self._draft_path(uuid)
        draft = self._read_draft(store, path)
        if text is not None:
            draft.proposed_text = text.strip()
        if not draft.proposed_text:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "Draft text cannot be empty")
        draft.approved = True
        store.move_draft_to_outbox(draft)
        context = store.read_chat_context(draft.chat_id)
        return {
            "status": "queued",
            "draft_uuid": draft.uuid,
            "chat_id": draft.chat_id,
            "outbox_path": str(self.data_dir / "outbox" / f"{draft.uuid}.md"),
            "professional": context.get("professional"),
            "sent": False,
        }

    def edit_draft(self, uuid: str, *, text: str) -> JSON:
        if not text.strip():
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "Draft text cannot be empty")
        store = MessageStore(self.data_dir)
        path = self._draft_path(uuid)
        draft = self._read_draft(store, path)
        draft.proposed_text = text.strip()
        draft.approved = False
        store.write_draft(draft)
        return {
            "status": "edited",
            "draft": self._draft_payload(draft, path),
        }

    def discard_draft(self, uuid: str) -> JSON:
        path = self._draft_path(uuid)
        path.unlink()
        return {
            "status": "discarded",
            "draft_uuid": uuid,
        }

    def reject_draft(self, uuid: str, *, reasoning: str = "Operator rejected the draft.") -> JSON:
        store = MessageStore(self.data_dir)
        path = self._draft_path(uuid)
        draft = self._read_draft(store, path)
        if draft.source_rowid is not None:
            store.write_no_reply_decision(
                uuid=str(uuid4()),
                chat_id=draft.chat_id,
                target_identifier=draft.target_identifier,
                source_rowid=draft.source_rowid,
                created_at=datetime.now(UTC),
                reasoning=reasoning,
                model=draft.model,
            )
        path.unlink()
        return {
            "status": "rejected",
            "draft_uuid": uuid,
            "chat_id": draft.chat_id,
            "source_rowid": draft.source_rowid,
        }

    def mark_no_reply(
        self,
        *,
        chat_id: int,
        source_rowid: int,
        reasoning: str,
        model: str | None = None,
    ) -> JSON:
        if not reasoning.strip():
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "reasoning is required")
        with IMessageArchive(self.db_path) as archive:
            message = archive.message_by_rowid(source_rowid)
        if message is None:
            raise OperatorServiceError(HTTPStatus.NOT_FOUND, "Message not found")
        if message.chat_id != chat_id:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "Message does not belong to chat")
        decision_uuid = str(uuid4())
        MessageStore(self.data_dir).write_no_reply_decision(
            uuid=decision_uuid,
            chat_id=chat_id,
            target_identifier=message.chat_identifier or message.sender,
            source_rowid=source_rowid,
            created_at=datetime.now(UTC),
            reasoning=reasoning.strip(),
            model=model,
        )
        return {
            "status": "no_reply_recorded",
            "uuid": decision_uuid,
            "chat_id": chat_id,
            "source_rowid": source_rowid,
        }

    def request_draft(
        self,
        *,
        chat_id: int | None = None,
        message_rowid: int | None = None,
    ) -> JSON:
        if not self.config.openai_api_key:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "OPENAI_API_KEY is not set")
        if chat_id is None and message_rowid is None:
            raise OperatorServiceError(
                HTTPStatus.BAD_REQUEST,
                "chat_id or message_rowid is required",
            )
        with IMessageArchive(self.db_path) as archive:
            message = self._draft_target_message(
                archive,
                chat_id=chat_id,
                message_rowid=message_rowid,
            )
            if message is None:
                raise OperatorServiceError(HTTPStatus.NOT_FOUND, "No target message found")
            if chat_id is not None and (message.is_from_me or message.is_reaction):
                raise OperatorServiceError(
                    HTTPStatus.CONFLICT,
                    "Latest message is not an inbound reply target",
                )
            store = MessageStore(self.data_dir)
            drafter = Drafter(
                store,
                OpenAIResponsesDraftingClient(api_key=self.config.openai_api_key),
                default_model=self.config.draft_model,
                max_inbox_age_hours=0,
                auto_approve_default=self.config.auto_approve,
            )
            worker = ArchiveAgentWorker(
                archive=archive,
                store=store,
                drafter=drafter,
                history_limit=self.config.chat_context_messages,
            )
            draft = asyncio.run(worker.draft_archived_message(message))
            return self._draft_result(store, message=message, draft=draft)

    def logs(self, service: str, *, errors: bool = False, lines: int = 80) -> JSON:
        path = log_path(service, data_dir=self.data_dir, errors=errors)
        return {
            "service": service,
            "errors": errors,
            "path": str(path),
            "exists": path.exists(),
            "lines": self._tail_lines(path, lines),
        }

    def service_action(self, service: str, action: str) -> JSON:
        if action == "start":
            service_start(service)
        elif action == "stop":
            service_stop(service)
        elif action == "restart":
            service_restart(service)
        else:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "Unknown service action")
        return {"status": action, "service": service, "state": service_status(service)}

    def _draft_target_message(
        self,
        archive: IMessageArchive,
        *,
        chat_id: int | None,
        message_rowid: int | None,
    ) -> Message | None:
        if message_rowid is not None:
            return archive.message_by_rowid(message_rowid)
        if chat_id is not None:
            return archive.latest_message_for_chat(chat_id)
        return None

    def _draft_result(
        self,
        store: MessageStore,
        *,
        message: Message,
        draft: Draft | None,
    ) -> JSON:
        if draft is not None:
            path = store.data_dir / "chats" / str(draft.chat_id) / "drafts" / f"{draft.uuid}.md"
            return {
                "status": "draft_created",
                "chat_id": message.chat_id,
                "message_rowid": message.rowid,
                "draft": self._draft_payload(draft, path),
            }
        artifact = self._reply_artifact_for_source(
            store,
            chat_id=message.chat_id,
            source_rowid=message.rowid,
        )
        if artifact is not None:
            return {
                "status": str(artifact.get("draft_status") or "handled"),
                "chat_id": message.chat_id,
                "message_rowid": message.rowid,
                "artifact": artifact,
            }
        return {
            "status": "skipped",
            "chat_id": message.chat_id,
            "message_rowid": message.rowid,
        }

    def _reply_artifact_for_source(
        self,
        store: MessageStore,
        *,
        chat_id: int,
        source_rowid: int,
    ) -> ArchiveRow | None:
        decorated = _decorate_pending_row(
            {
                "chat_id": chat_id,
                "message_rowid": source_rowid,
            },
            store,
        )
        if decorated.get("draft_status") == "missing":
            return None
        return decorated

    def _draft_path(self, uuid: str) -> Path:
        if "/" in uuid or "\\" in uuid or uuid in {"", ".", ".."}:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "Invalid draft uuid")
        matches = sorted((self.data_dir / "chats").glob(f"*/drafts/{uuid}.md"))
        if not matches:
            raise OperatorServiceError(HTTPStatus.NOT_FOUND, "Draft not found")
        return matches[0]

    def _read_draft(self, store: MessageStore, path: Path) -> Draft:
        draft = store.read_draft(path)
        if draft is None:
            raise OperatorServiceError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "Draft could not be parsed",
            )
        return draft

    def _draft_payload(self, draft: Draft, path: Path) -> JSON:
        payload = asdict(draft)
        payload["created_at"] = draft.created_at.isoformat()
        payload["path"] = str(path)
        return payload

    def _tail_lines(self, path: Path, limit: int) -> list[str]:
        if not path.exists():
            return [f"(missing: {path})"]
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
