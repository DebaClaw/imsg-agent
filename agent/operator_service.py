"""
operator_service.py - Shared operator-facing service layer.

This module holds the read/write operations used by management interfaces such as
the CLI, local web app, and a future TUI. It is intentionally local-first: archive
visibility comes from SQLite, and reply review actions mutate the existing Markdown
approval artifacts instead of sending messages directly.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from .archive_agent import ArchiveAgentWorker
from .archive_store import ArchiveRow, IMessageArchive
from .business_research import BusinessResearchError, OpenAIBusinessResearcher
from .config import Config
from .contact_enrichment import (
    contacts_from_json,
    contacts_mcp_tool,
    load_contacts_from_contacts_mcp,
    normalize_identifier,
)
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
            attention = archive.attention_items(limit=max(limit, 50))
            recent_candidates = archive.recent_chats(limit=max(limit * 10, 100))
            recent = recent_candidates[:limit]
            orbit = self._orbit_rows(attention, recent_candidates, store, limit=limit)
            return {
                "status": self._status_from_archive(archive),
                "attention": orbit,
                "pending": self._pending_rows(archive, store, limit=limit),
                "recent": recent,
                "operator": self.operator_profile(),
                "preferences": self.observatory_preferences(),
                "revision": self._data_revision(),
            }

    def orbit(
        self,
        *,
        limit: int = 16,
        offset: int = 0,
        direction: str = "incoming",
        days: int = 7,
        include_spam: bool = False,
    ) -> JSON:
        if limit < 1 or limit > 32:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "limit must be 1 to 32")
        if offset < 0:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "offset must not be negative")
        if direction not in {"incoming", "outgoing", "either"}:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "Unknown Orbit direction")
        if days not in {0, 1, 7, 30, 90}:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "days must be 0, 1, 7, 30, or 90")
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat() if days else None
        store = MessageStore(self.data_dir)
        with IMessageArchive(self.db_path) as archive:
            page = archive.orbit_threads(
                limit=limit,
                offset=offset,
                direction=direction,
                since=since,
                exclude_chat_ids=None if include_spam else store.ignored_spam_chat_ids(),
            )
        items = self._orbit_rows(cast(JSONList, page["items"]), [], store, limit=limit)
        return {
            **page,
            "items": items,
            "direction": direction,
            "days": days,
            "include_spam": include_spam,
        }

    def pending(
        self,
        *,
        limit: int = 20,
        days: int | None = None,
        relationships: list[str] | None = None,
        include_archived: bool | None = None,
    ) -> JSONList:
        with IMessageArchive(self.db_path) as archive:
            return self._pending_rows(
                archive,
                MessageStore(self.data_dir),
                limit=limit,
                days=days,
                relationships=relationships,
                include_archived=include_archived,
            )

    def operator_profile(self) -> JSON:
        profile = MessageStore(self.data_dir).read_operator_profile()
        contact_id = str(profile.get("contact_id") or "").strip()
        contact: JSON = {}
        if contact_id:
            with IMessageArchive(self.db_path) as archive:
                contact = archive.contact(contact_id)
        if contact:
            profile["contact"] = contact
            profile["display_name"] = str(contact.get("full_name") or self._operator_name(profile))
            profile["avatar_data_uri"] = str(
                contact.get("photo_data_uri") or self._profile_vcard_avatar(profile)
            )
        else:
            profile["display_name"] = self._operator_name(profile)
            profile["avatar_data_uri"] = self._profile_vcard_avatar(profile)
        profile["identity"] = {
            "name": self._operator_name(profile),
            "contact_id": contact_id,
            "aliases": self._string_list(profile.get("aliases")),
        }
        return profile

    def update_operator_profile(self, fields: JSON) -> JSON:
        store = MessageStore(self.data_dir)
        profile = store.read_operator_profile()
        allowed = {"name", "display_name", "vcard_path", "contact_id", "aliases", "avatar_data_uri"}
        for key, value in fields.items():
            if key in allowed:
                profile[key] = value
                if key == "display_name":
                    profile["name"] = value
        store.write_operator_profile(profile)
        return {"status": "saved", "operator": self.operator_profile()}

    def observatory_preferences(self) -> JSON:
        return MessageStore(self.data_dir).read_observatory_preferences()

    def update_observatory_preferences(self, fields: JSON) -> JSON:
        store = MessageStore(self.data_dir)
        preferences = store.read_observatory_preferences()
        if "pending_days" in fields:
            days = fields["pending_days"]
            if not isinstance(days, int) or days < 0 or days > 3650:
                raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "pending_days must be 0 to 3650")
            preferences["pending_days"] = days
        if "relationship_types" in fields:
            relationships = fields["relationship_types"]
            if not isinstance(relationships, list) or not all(
                isinstance(item, str) for item in relationships
            ):
                raise OperatorServiceError(
                    HTTPStatus.BAD_REQUEST,
                    "relationship_types must be a list",
                )
            preferences["relationship_types"] = relationships
        for key in ("group_pending_by_relationship", "show_archived_drafts"):
            if key in fields:
                if not isinstance(fields[key], bool):
                    raise OperatorServiceError(HTTPStatus.BAD_REQUEST, f"{key} must be a boolean")
                preferences[key] = fields[key]
        store.write_observatory_preferences(preferences)
        return {"status": "saved", "preferences": store.read_observatory_preferences()}

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

    def changes(self, *, since: str = "") -> JSON:
        revision = self._data_revision()
        return {"revision": revision, "changed": revision != since}

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

    def chat(
        self,
        chat_id: int,
        *,
        limit: int = 80,
        before: str | None = None,
        before_rowid: int | None = None,
    ) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            newest_first = archive.chat_messages(
                chat_id,
                limit=limit + 1,
                before=before,
                before_rowid=before_rowid,
            )
            has_more = len(newest_first) > limit
            messages = list(reversed(newest_first[:limit]))
            seed = archive.chat_context_seed(chat_id)
            contacts = archive.chat_contacts(chat_id)
        store = MessageStore(self.data_dir)
        operator_contact_id = str(store.read_operator_profile().get("contact_id") or "")
        contacts = [
            contact
            for contact in contacts
            if str(contact.get("contact_id") or "") != operator_contact_id
        ]
        context, notes = store.read_chat_context_document(chat_id)
        review, review_notes = store.read_contact_review(chat_id)
        recipients = self._recipient_participants(seed.get("participants") or [])
        return {
            "chat": seed,
            "recipients": recipients,
            "context": context,
            "notes": notes,
            "messages": messages,
            "has_more": has_more,
            "next_before_rowid": messages[0]["message_rowid"] if messages else None,
            "contacts": contacts,
            "contact_review": review,
            "contact_review_notes": review_notes,
        }

    def contacts(self, *, limit: int = 50, query: str = "") -> JSONList:
        importance = MessageStore(self.data_dir).read_contact_importance()
        with IMessageArchive(self.db_path) as archive:
            rows = archive.contacts(limit=limit, query=query)
        return [{**row, "importance": importance.get(str(row["contact_id"]), 0)} for row in rows]

    def contacts_page(self, *, limit: int = 50, offset: int = 0, query: str = "") -> JSON:
        if limit < 1 or limit > 100:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "limit must be 1 to 100")
        if offset < 0:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "offset must not be negative")
        with IMessageArchive(self.db_path) as archive:
            page = archive.contacts_page(limit=limit, offset=offset, query=query)
        importance = MessageStore(self.data_dir).read_contact_importance()
        page_items = cast(JSONList, page["items"])
        items = [
            {**row, "importance": importance.get(str(row["contact_id"]), 0)}
            for row in page_items
        ]
        return {**page, "items": items}

    def contact(self, contact_id: str) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            detail = archive.contact(contact_id)
        if not detail:
            raise OperatorServiceError(HTTPStatus.NOT_FOUND, "Contact not found")
        importance = MessageStore(self.data_dir).read_contact_importance()
        detail["importance"] = importance.get(contact_id, 0)
        return detail

    def update_contact_importance(self, contact_id: str, importance: int) -> JSON:
        if importance < 0 or importance > 5:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "importance must be 0 to 5")
        with IMessageArchive(self.db_path) as archive:
            if not archive.contact(contact_id):
                raise OperatorServiceError(HTTPStatus.NOT_FOUND, "Contact not found")
        store = MessageStore(self.data_dir)
        values = store.read_contact_importance()
        values[contact_id] = importance
        store.write_contact_importance(values)
        return {"status": "saved", "contact_id": contact_id, "importance": importance}

    def sync_contacts(self) -> JSON:
        try:
            raw = load_contacts_from_contacts_mcp(
                command=self.config.contacts_command,
                store_path=self.config.contacts_store,
            )
        except (RuntimeError, ValueError) as exc:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        contacts = contacts_from_json(raw)
        with IMessageArchive(self.db_path) as archive:
            synced = archive.replace_contacts(contacts)
            enriched = archive.enrich_chat_contacts()
        return {"status": "synced", **asdict(synced), "matches": asdict(enriched)}

    def research_business_contact(self, *, name: str, location: str) -> JSON:
        name = name.strip()
        location = location.strip()
        if not name:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "Business name is required")
        if not location:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "Location is required")
        if not self.config.openai_api_key:
            raise OperatorServiceError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "OPENAI_API_KEY is required for business research",
            )
        try:
            return OpenAIBusinessResearcher(
                api_key=self.config.openai_api_key,
                model=self.config.draft_model,
            ).research(name=name, location=location)
        except BusinessResearchError as exc:
            raise OperatorServiceError(HTTPStatus.BAD_GATEWAY, str(exc)) from exc

    def create_contact(self, fields: JSON) -> JSON:
        full_name = str(fields.get("fullName") or "").strip()
        if not full_name:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "fullName is required")
        payload = self._contact_payload(fields, include_name=True)
        try:
            created = contacts_mcp_tool(
                command=self.config.contacts_command,
                tool="create_contact",
                arguments=payload,
                store_path=self.config.contacts_store,
            )
        except RuntimeError as exc:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        synced = self.sync_contacts()
        return {"status": "created", "contact": created, "sync": synced}

    def update_contact(self, contact_id: str, fields: JSON) -> JSON:
        payload = self._contact_payload(fields, include_name=False)
        if not payload:
            raise OperatorServiceError(
                HTTPStatus.BAD_REQUEST,
                "At least one contact field is required",
            )
        try:
            updated = contacts_mcp_tool(
                command=self.config.contacts_command,
                tool="update_contact",
                arguments={"id": contact_id, **payload},
                store_path=self.config.contacts_store,
            )
        except RuntimeError as exc:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        synced = self.sync_contacts()
        return {"status": "updated", "contact": updated, "sync": synced}

    def delete_contact(self, contact_id: str) -> JSON:
        try:
            deleted = contacts_mcp_tool(
                command=self.config.contacts_command,
                tool="delete_contact",
                arguments={"id": contact_id},
                store_path=self.config.contacts_store,
            )
        except RuntimeError as exc:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        synced = self.sync_contacts()
        return {"status": "deleted", "contact": deleted, "sync": synced}

    def link_contact(self, *, chat_id: int, contact_id: str) -> JSON:
        try:
            with IMessageArchive(self.db_path) as archive:
                archive.link_chat_contact(chat_id, contact_id)
                contacts = archive.chat_contacts(chat_id)
        except ValueError as exc:
            raise OperatorServiceError(HTTPStatus.NOT_FOUND, str(exc)) from exc
        return {"status": "linked", "chat_id": chat_id, "contacts": contacts}

    def unlink_contact(self, *, chat_id: int, contact_id: str) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            archive.unlink_chat_contact(chat_id, contact_id)
            contacts = archive.chat_contacts(chat_id)
        return {"status": "unlinked", "chat_id": chat_id, "contacts": contacts}

    @staticmethod
    def _contact_payload(fields: JSON, *, include_name: bool) -> JSON:
        allowed = {
            "fullName",
            "givenName",
            "familyName",
            "emails",
            "phones",
            "addresses",
            "organization",
            "birthday",
            "notes",
            "categories",
        }
        payload = {key: value for key, value in fields.items() if key in allowed}
        if include_name:
            payload["fullName"] = str(fields.get("fullName") or "").strip()
        return payload

    def update_chat_context(
        self,
        chat_id: int,
        *,
        fields: dict[str, object],
        notes: str | None = None,
    ) -> JSON:
        store = MessageStore(self.data_dir)
        current, current_notes = store.read_chat_context_document(chat_id)
        with IMessageArchive(self.db_path) as archive:
            seed = archive.chat_context_seed(chat_id)
        allowed = {
            "relationship",
            "tone",
            "professional",
            "auto_approve",
            "do_not_draft",
            "favorite",
            "agent_notes",
            "model",
            "name",
            "service",
            "participants",
        }
        updated = dict(current)
        recipients = self._recipient_participants(seed.get("participants") or [])
        if seed:
            updated.setdefault("name", seed.get("name") or f"chat {chat_id}")
            updated.setdefault("identifier", seed.get("identifier") or "")
            updated.setdefault("service", seed.get("service") or "")
            updated.setdefault("participants", recipients)
            updated.setdefault("is_group", bool(seed.get("is_group")))
            if seed.get("last_message_at"):
                updated.setdefault("last_active", seed.get("last_message_at"))
        for key, value in fields.items():
            if key in allowed:
                updated[key] = (
                    self._recipient_participants(value)
                    if key == "participants" and isinstance(value, list)
                    else value
                )
        updated.setdefault("chat_id", chat_id)
        updated["notes"] = current_notes if notes is None else notes
        store.write_chat_context(chat_id, updated)
        context, body = store.read_chat_context_document(chat_id)
        return {"status": "saved", "chat_id": chat_id, "context": context, "notes": body}

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _operator_name(profile: JSON) -> str:
        return str(profile.get("name") or profile.get("display_name") or "Me")

    @staticmethod
    def _participant_key(value: object) -> str:
        raw = str(value or "").strip()
        normalized = normalize_identifier(raw)
        if normalized is not None:
            return ":".join(normalized)
        return raw.lower()

    def _recipient_participants(self, participants: object) -> list[str]:
        values = self._string_list(participants)
        profile = self.operator_profile()
        operator_values = [
            "Me",
            str(profile.get("display_name") or ""),
            *self._string_list(profile.get("aliases")),
        ]
        contact = profile.get("contact")
        if isinstance(contact, dict):
            operator_values.append(str(contact.get("full_name") or ""))
            points = contact.get("points")
            if isinstance(points, list):
                for point in points:
                    if isinstance(point, dict):
                        operator_values.extend(
                            [str(point.get("value") or ""), str(point.get("original_value") or "")]
                        )
        operator_keys = {self._participant_key(value) for value in operator_values if value}
        return [value for value in values if self._participant_key(value) not in operator_keys]

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
        return self.archive_draft(uuid, reason="Archived by operator.")

    def archive_draft(self, uuid: str, *, reason: str = "Archived by operator.") -> JSON:
        store = MessageStore(self.data_dir)
        path = self._draft_path(uuid)
        draft = self._read_draft(store, path)
        archive_path = store.archive_draft(draft, reason=reason)
        return {
            "status": "archived",
            "draft_uuid": uuid,
            "chat_id": draft.chat_id,
            "archive_path": str(archive_path),
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
        store.archive_draft(draft, reason=reasoning)
        return {
            "status": "rejected",
            "draft_uuid": uuid,
            "chat_id": draft.chat_id,
            "source_rowid": draft.source_rowid,
        }

    def review_contact(
        self,
        *,
        chat_id: int,
        decision: str,
        notes: str = "",
    ) -> JSON:
        if decision not in {"keep_local", "ignore_spam", "prepare_contact"}:
            raise OperatorServiceError(HTTPStatus.BAD_REQUEST, "Unknown contact decision")
        with IMessageArchive(self.db_path) as archive:
            seed = archive.chat_context_seed(chat_id)
        if not seed:
            raise OperatorServiceError(HTTPStatus.NOT_FOUND, "Chat not found")
        store = MessageStore(self.data_dir)
        identifier = str(seed.get("identifier") or "")
        review_path = store.write_contact_review(
            chat_id=chat_id,
            decision=decision,
            identifier=identifier,
            notes=notes,
        )
        result: JSON = {"status": "recorded", "decision": decision, "path": str(review_path)}
        if decision == "prepare_contact":
            candidate = store.write_contact_candidate(
                chat_id=chat_id,
                name=str(seed.get("name") or ""),
                identifier=identifier,
            )
            result["candidate_path"] = str(candidate)
        if decision == "ignore_spam":
            current, current_notes = store.read_chat_context_document(chat_id)
            defaults = {
                "relationship": "unwanted contact / spam",
                "tone": (
                    "Do not engage. Only send a concise decline or unsubscribe request "
                    "when the operator explicitly asks."
                ),
                "agent_notes": (
                    "Do not draft replies for this sender. If the operator explicitly requests "
                    "a reply, keep it brief and ask not to be contacted again."
                ),
            }
            fields: JSON = {
                key: value
                for key, value in defaults.items()
                if not str(current.get(key) or "").strip()
            }
            fields["do_not_draft"] = True
            fields["auto_approve"] = False
            context_result = self.update_chat_context(
                chat_id,
                fields=fields,
                notes=(
                    current_notes
                    if current_notes.strip()
                    else (
                        "Unwanted sender. Do not engage or draft a response without "
                        "explicit operator direction."
                    )
                ),
            )
            result["context"] = context_result["context"]
            result["notes"] = context_result["notes"]
        return result

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

    def _pending_rows(
        self,
        archive: IMessageArchive,
        store: MessageStore,
        *,
        limit: int,
        days: int | None = None,
        relationships: list[str] | None = None,
        include_archived: bool | None = None,
    ) -> JSONList:
        preferences = store.read_observatory_preferences()
        pending_days = int(preferences.get("pending_days") or 0) if days is None else days
        allowed_relationships = (
            {
                item.strip().lower()
                for item in preferences.get("relationship_types", [])
                if item.strip()
            }
            if relationships is None
            else {item.strip().lower() for item in relationships if item.strip()}
        )
        show_archived = bool(preferences.get("show_archived_drafts"))
        if include_archived is not None:
            show_archived = include_archived
        cutoff = datetime.now(UTC) - timedelta(days=pending_days) if pending_days > 0 else None
        rows: JSONList = []
        for row in pending_replies(
            archive,
            store,
            limit=max(limit * 10, 100),
            max_missing_age_hours=self.config.max_inbox_age_hours,
        ):
            if row.get("draft_status") == "archived" and not show_archived:
                continue
            chat_id = int(str(row["chat_id"]))
            context = store.read_chat_context(chat_id)
            relationship = (
                str(context.get("relationship") or "unclassified").strip()
                or "unclassified"
            )
            row["relationship"] = relationship
            if allowed_relationships and relationship.lower() not in allowed_relationships:
                continue
            if cutoff is not None:
                timestamp = str(row.get("last_message_at") or "")
                try:
                    if datetime.fromisoformat(timestamp.replace("Z", "+00:00")) < cutoff:
                        continue
                except ValueError:
                    pass
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    def _orbit_rows(
        self,
        attention: JSONList,
        recent: JSONList,
        store: MessageStore,
        *,
        limit: int,
    ) -> JSONList:
        orbit_by_chat = {int(str(row["chat_id"])): dict(row) for row in attention}
        for row in recent:
            chat_id = int(str(row["chat_id"]))
            context = store.read_chat_context(chat_id)
            if bool(context.get("favorite")) and chat_id not in orbit_by_chat:
                orbit_by_chat[chat_id] = dict(row)
        orbit: JSONList = []
        importance = store.read_contact_importance()
        operator = self.operator_profile()
        operator_names = self._operator_display_names(operator)
        for row in orbit_by_chat.values():
            context = store.read_chat_context(int(str(row["chat_id"])))
            row = dict(row)
            row["contacts"] = self._without_operator_names(row.get("contacts"), operator_names)
            row["favorite"] = bool(context.get("favorite"))
            row["relationship"] = str(context.get("relationship") or "unclassified")
            row["relationship_priority"] = self._relationship_priority(row["relationship"])
            contact_ids = [item for item in str(row.get("contact_ids") or "").split(",") if item]
            row["importance"] = max(
                (importance.get(contact_id, 0) for contact_id in contact_ids), default=0
            )
            inbound = int(str(row.get("inbound_90d") or 0))
            outbound = int(str(row.get("outbound_90d") or 0))
            row["two_way_score"] = min(5, min(inbound, outbound)) if contact_ids else 0
            row["orbit_score"] = (
                int(str(row.get("score") or 0))
                + int(str(row["importance"])) * 12
                + int(str(row["two_way_score"])) * 4
                + int(str(row["relationship_priority"])) * 3
                + (20 if bool(row["favorite"]) else 0)
            )
            if row["two_way_score"]:
                row["reason"] = f"{str(row.get('reason') or '')}; two-way discussion".strip("; ")
            orbit.append(row)
        orbit.sort(
            key=lambda row: (
                bool(row["favorite"]),
                int(str(row["orbit_score"])),
                str(row.get("last_message_at") or ""),
            ),
            reverse=True,
        )
        return orbit[:limit]

    @staticmethod
    def _operator_display_names(profile: JSON) -> set[str]:
        values = {
            "me",
            str(profile.get("display_name") or "").strip().casefold(),
            str(profile.get("name") or "").strip().casefold(),
        }
        contact = profile.get("contact")
        if isinstance(contact, dict):
            values.add(str(contact.get("full_name") or "").strip().casefold())
        return {value for value in values if value}

    @staticmethod
    def _without_operator_names(value: object, operator_names: set[str]) -> str:
        names = [item.strip() for item in str(value or "").split(",") if item.strip()]
        return ", ".join(name for name in names if name.casefold() not in operator_names)

    @staticmethod
    def _relationship_priority(relationship: object) -> int:
        value = str(relationship).lower()
        if any(term in value for term in ("spouse", "wife", "husband", "partner", "family")):
            return 4
        if "close friend" in value:
            return 3
        if "friend" in value:
            return 2
        if any(term in value for term in ("boss", "coworker", "customer", "vendor")):
            return 1
        return 0

    def _status_from_archive(self, archive: IMessageArchive) -> JSON:
        return {
            "data_dir": str(self.data_dir),
            "db_path": str(self.db_path),
            "archive": archive.archive_stats(),
            "services": [service_status("monitor"), service_status("worker")],
        }

    def _data_revision(self) -> str:
        paths = [
            self.db_path,
            self.data_dir / "state.json",
            self.data_dir / "outbox",
            self.data_dir / "sent",
            self.data_dir / "errors",
            self.data_dir / "no_reply",
            self.data_dir / "draft_archive",
        ]
        stamps = []
        for path in paths:
            try:
                stamps.append(str(path.stat().st_mtime_ns))
            except OSError:
                stamps.append("0")
        return ":".join(stamps)

    @staticmethod
    def _profile_vcard_avatar(profile: JSON) -> str:
        configured = str(profile.get("avatar_data_uri") or "")
        if configured:
            return configured
        path_value = str(profile.get("vcard_path") or "").strip()
        if not path_value:
            return ""
        path = Path(path_value).expanduser()
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            return ""
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return ""
        photo_lines: list[str] = []
        collecting = False
        mime = "image/jpeg"
        for line in lines:
            upper = line.upper()
            if upper.startswith("PHOTO"):
                collecting = True
                if "TYPE=PNG" in upper:
                    mime = "image/png"
                photo_lines.append(line.split(":", 1)[-1])
                continue
            if collecting and line.startswith(" "):
                photo_lines.append(line.strip())
                continue
            if collecting:
                break
        encoded = "".join(photo_lines).strip()
        if not encoded:
            return ""
        try:
            base64.b64decode(encoded, validate=True)
        except ValueError:
            return ""
        return f"data:{mime};base64,{encoded}"

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
