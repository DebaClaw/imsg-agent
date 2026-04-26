"""
archive_store.py - SQLite archive for iMessage chats, messages, attachments, and reactions.

Markdown remains the approval/drafting store. This database is a local searchable archive
of iMessage data received through `imsg rpc`.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .contact_enrichment import (
    ContactRecord,
    ContactsEnrichResult,
    ContactsSyncResult,
    normalize_identifier,
)
from .models import Attachment, Chat, Message, Reaction

SCHEMA_VERSION = 3
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class _CopiedAttachment:
    local_path: str
    archived: bool
    error: str


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _safe_filename(value: str, fallback: str) -> str:
    candidate = Path(value).name if value else fallback
    cleaned = SAFE_FILENAME_RE.sub("_", candidate).strip("._")
    return cleaned or fallback


class IMessageArchive:
    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()

    def close(self) -> None:
        self._db.close()

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY,
                identifier TEXT NOT NULL DEFAULT '',
                guid TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                service TEXT NOT NULL DEFAULT '',
                last_message_at TEXT NOT NULL DEFAULT '',
                participants_json TEXT NOT NULL DEFAULT '[]',
                is_group INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                rowid INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                guid TEXT NOT NULL DEFAULT '',
                sender TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL,
                is_from_me INTEGER NOT NULL,
                service TEXT NOT NULL DEFAULT '',
                has_attachments INTEGER NOT NULL DEFAULT 0,
                reply_to_guid TEXT,
                thread_originator_guid TEXT,
                destination_caller_id TEXT,
                is_reaction INTEGER NOT NULL DEFAULT 0,
                reaction_type TEXT,
                chat_identifier TEXT NOT NULL DEFAULT '',
                chat_guid TEXT NOT NULL DEFAULT '',
                chat_name TEXT NOT NULL DEFAULT '',
                participants_json TEXT NOT NULL DEFAULT '[]',
                is_group INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_rowid INTEGER NOT NULL,
                position INTEGER NOT NULL,
                filename TEXT NOT NULL DEFAULT '',
                transfer_name TEXT NOT NULL DEFAULT '',
                uti TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                total_bytes INTEGER NOT NULL DEFAULT 0,
                is_sticker INTEGER NOT NULL DEFAULT 0,
                original_path TEXT NOT NULL DEFAULT '',
                local_path TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0,
                archive_error TEXT NOT NULL DEFAULT '',
                missing INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (message_rowid) REFERENCES messages(rowid) ON DELETE CASCADE,
                UNIQUE(message_rowid, position)
            );

            CREATE TABLE IF NOT EXISTS reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_rowid INTEGER NOT NULL,
                position INTEGER NOT NULL,
                reaction_type TEXT NOT NULL DEFAULT '',
                sender TEXT NOT NULL DEFAULT '',
                is_from_me INTEGER NOT NULL DEFAULT 0,
                date TEXT NOT NULL DEFAULT '',
                emoji TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (message_rowid) REFERENCES messages(rowid) ON DELETE CASCADE,
                UNIQUE(message_rowid, position)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages(chat_id, date);
            CREATE INDEX IF NOT EXISTS idx_messages_guid ON messages(guid);
            CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_rowid);

            CREATE TABLE IF NOT EXISTS contacts (
                contact_id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL DEFAULT '',
                given_name TEXT NOT NULL DEFAULT '',
                family_name TEXT NOT NULL DEFAULT '',
                organization_name TEXT NOT NULL DEFAULT '',
                organization_title TEXT NOT NULL DEFAULT '',
                birthday TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                categories_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contact_points (
                contact_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                original_value TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                primary_flag INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (contact_id) REFERENCES contacts(contact_id) ON DELETE CASCADE,
                UNIQUE(contact_id, kind, value)
            );

            CREATE TABLE IF NOT EXISTS chat_contact_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                contact_id TEXT,
                status TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                matched_on TEXT NOT NULL DEFAULT '',
                matched_value TEXT NOT NULL DEFAULT '',
                source_identifier TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                FOREIGN KEY (contact_id) REFERENCES contacts(contact_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_contact_points_value
                ON contact_points(kind, value);
            CREATE INDEX IF NOT EXISTS idx_chat_contact_matches_chat
                ON chat_contact_matches(chat_id);
            """
        )
        self._ensure_column("attachments", "local_path", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("attachments", "archived", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("attachments", "archive_error", "TEXT NOT NULL DEFAULT ''")
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self._db.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self._db.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {str(row["name"]) for row in rows}:
            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            """
            INSERT INTO meta(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def read_cursor(self) -> int:
        value = self.get_meta("cursor", "0")
        try:
            return int(value)
        except ValueError:
            return 0

    def write_cursor(self, rowid: int) -> None:
        self.set_meta("cursor", str(rowid))
        self._db.commit()

    def upsert_chat(self, chat: Chat) -> None:
        now = _fmt_dt(datetime.now(UTC))
        self._db.execute(
            """
            INSERT INTO chats(
                id, identifier, guid, name, service, last_message_at,
                participants_json, is_group, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                identifier = excluded.identifier,
                guid = excluded.guid,
                name = excluded.name,
                service = excluded.service,
                last_message_at = excluded.last_message_at,
                participants_json = excluded.participants_json,
                is_group = excluded.is_group,
                updated_at = excluded.updated_at
            """,
            (
                chat.id,
                chat.identifier,
                chat.guid,
                chat.name,
                chat.service,
                _fmt_dt(chat.last_message_at),
                json.dumps(chat.participants, ensure_ascii=False),
                int(chat.is_group),
                now,
            ),
        )
        self._db.commit()

    def upsert_message(self, message: Message) -> None:
        now = _fmt_dt(datetime.now(UTC))
        self._ensure_chat_for_message(message, now)
        self._db.execute(
            """
            INSERT INTO messages(
                rowid, chat_id, guid, sender, text, date, is_from_me, service,
                has_attachments, reply_to_guid, thread_originator_guid,
                destination_caller_id, is_reaction, reaction_type, chat_identifier,
                chat_guid, chat_name, participants_json, is_group, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rowid) DO UPDATE SET
                chat_id = excluded.chat_id,
                guid = excluded.guid,
                sender = excluded.sender,
                text = excluded.text,
                date = excluded.date,
                is_from_me = excluded.is_from_me,
                service = excluded.service,
                has_attachments = excluded.has_attachments,
                reply_to_guid = excluded.reply_to_guid,
                thread_originator_guid = excluded.thread_originator_guid,
                destination_caller_id = excluded.destination_caller_id,
                is_reaction = excluded.is_reaction,
                reaction_type = excluded.reaction_type,
                chat_identifier = excluded.chat_identifier,
                chat_guid = excluded.chat_guid,
                chat_name = excluded.chat_name,
                participants_json = excluded.participants_json,
                is_group = excluded.is_group,
                updated_at = excluded.updated_at
            """,
            (
                message.rowid,
                message.chat_id,
                message.guid,
                message.sender,
                message.text,
                _fmt_dt(message.date),
                int(message.is_from_me),
                message.service,
                int(message.has_attachments),
                message.reply_to_guid,
                message.thread_originator_guid,
                message.destination_caller_id,
                int(message.is_reaction),
                message.reaction_type,
                message.chat_identifier,
                message.chat_guid,
                message.chat_name,
                json.dumps(message.participants, ensure_ascii=False),
                int(message.is_group),
                now,
            ),
        )
        self._replace_attachments(message.rowid, message.attachments, now)
        self._replace_reactions(message.rowid, message.reactions, now)
        self.set_meta("cursor", str(max(self.read_cursor(), message.rowid)))
        self._db.commit()

    def count_chats(self) -> int:
        row = self._db.execute("SELECT COUNT(*) AS count FROM chats").fetchone()
        return int(row["count"])

    def count_messages(self) -> int:
        row = self._db.execute("SELECT COUNT(*) AS count FROM messages").fetchone()
        return int(row["count"])

    def count_messages_for_chat(self, chat_id: int) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return int(row["count"])

    def oldest_message_for_chat(self, chat_id: int) -> tuple[int, datetime] | None:
        row = self._db.execute(
            """
            SELECT rowid, date
            FROM messages
            WHERE chat_id = ?
            ORDER BY date ASC, rowid ASC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        return int(row["rowid"]), _parse_dt(str(row["date"]))

    def count_attachments(self) -> int:
        row = self._db.execute("SELECT COUNT(*) AS count FROM attachments").fetchone()
        return int(row["count"])

    def count_saved_attachments(self) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS count FROM attachments WHERE archived = 1"
        ).fetchone()
        return int(row["count"])

    def count_contacts(self) -> int:
        row = self._db.execute("SELECT COUNT(*) AS count FROM contacts").fetchone()
        return int(row["count"])

    def count_contact_points(self) -> int:
        row = self._db.execute("SELECT COUNT(*) AS count FROM contact_points").fetchone()
        return int(row["count"])

    def count_chat_contact_matches(self, status: str | None = None) -> int:
        if status is None:
            row = self._db.execute(
                "SELECT COUNT(*) AS count FROM chat_contact_matches"
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT COUNT(*) AS count FROM chat_contact_matches WHERE status = ?",
                (status,),
            ).fetchone()
        return int(row["count"])

    def replace_contacts(self, contacts: list[ContactRecord]) -> ContactsSyncResult:
        now = _fmt_dt(datetime.now(UTC))
        with self._db:
            self._db.execute("DELETE FROM contact_points")
            self._db.execute("DELETE FROM contacts")
            for contact in contacts:
                self._db.execute(
                    """
                    INSERT INTO contacts(
                        contact_id, full_name, given_name, family_name,
                        organization_name, organization_title, birthday, notes,
                        categories_json, metadata_json, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contact.contact_id,
                        contact.full_name,
                        contact.given_name,
                        contact.family_name,
                        contact.organization_name,
                        contact.organization_title,
                        contact.birthday,
                        contact.notes,
                        contact.categories_json,
                        contact.metadata_json,
                        now,
                    ),
                )
                self._db.executemany(
                    """
                    INSERT INTO contact_points(
                        contact_id, kind, value, original_value, label,
                        primary_flag, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(contact_id, kind, value) DO UPDATE SET
                        original_value = excluded.original_value,
                        label = excluded.label,
                        primary_flag = excluded.primary_flag,
                        updated_at = excluded.updated_at
                    """,
                    [
                        (
                            contact.contact_id,
                            point.kind,
                            point.value,
                            point.original_value,
                            point.label,
                            int(point.primary),
                            now,
                        )
                        for point in contact.points
                    ],
                )
            self.set_meta("contacts_synced_at", now)
        return ContactsSyncResult(
            contacts=len(contacts),
            contact_points=sum(len(contact.points) for contact in contacts),
        )

    def enrich_chat_contacts(self, *, default_country: str = "US") -> ContactsEnrichResult:
        now = _fmt_dt(datetime.now(UTC))
        chat_identifiers = self._chat_identifiers()
        matched = 0
        ambiguous = 0
        unresolved = 0
        with self._db:
            self._db.execute("DELETE FROM chat_contact_matches")
            for chat_id, identifiers in chat_identifiers.items():
                seen_normalized: set[tuple[str, str]] = set()
                for source_identifier in sorted(identifiers):
                    normalized = normalize_identifier(source_identifier, default_country)
                    if normalized is None:
                        continue
                    kind, value = normalized
                    if (kind, value) in seen_normalized:
                        continue
                    seen_normalized.add((kind, value))
                    rows = self._db.execute(
                        """
                        SELECT contact_id
                        FROM contact_points
                        WHERE kind = ? AND value = ?
                        ORDER BY contact_id
                        """,
                        (kind, value),
                    ).fetchall()
                    if len(rows) == 1:
                        status = "matched"
                        confidence = 0.95 if kind == "email" else 0.9
                        contact_id = str(rows[0]["contact_id"])
                        matched += 1
                        self._insert_chat_contact_match(
                            chat_id,
                            contact_id,
                            status,
                            confidence,
                            kind,
                            value,
                            source_identifier,
                            now,
                        )
                    elif len(rows) > 1:
                        ambiguous += 1
                        for row in rows:
                            self._insert_chat_contact_match(
                                chat_id,
                                str(row["contact_id"]),
                                "ambiguous",
                                0.5,
                                kind,
                                value,
                                source_identifier,
                                now,
                            )
                    else:
                        unresolved += 1
                        self._insert_chat_contact_match(
                            chat_id,
                            None,
                            "unresolved",
                            0.0,
                            kind,
                            value,
                            source_identifier,
                            now,
                        )
            self.set_meta("contacts_enriched_at", now)
        return ContactsEnrichResult(
            chats=len(chat_identifiers),
            matched=matched,
            ambiguous=ambiguous,
            unresolved=unresolved,
        )

    def _chat_identifiers(self) -> dict[int, set[str]]:
        identifiers: dict[int, set[str]] = {}
        for row in self._db.execute(
            "SELECT id, identifier, participants_json FROM chats"
        ).fetchall():
            chat_id = int(row["id"])
            values = identifiers.setdefault(chat_id, set())
            self._add_identifier(values, str(row["identifier"] or ""))
            self._add_json_identifiers(values, str(row["participants_json"] or "[]"))

        for row in self._db.execute(
            """
            SELECT chat_id, sender, chat_identifier, participants_json
            FROM messages
            """
        ).fetchall():
            chat_id = int(row["chat_id"])
            values = identifiers.setdefault(chat_id, set())
            self._add_identifier(values, str(row["sender"] or ""))
            self._add_identifier(values, str(row["chat_identifier"] or ""))
            self._add_json_identifiers(values, str(row["participants_json"] or "[]"))
        return identifiers

    def _insert_chat_contact_match(
        self,
        chat_id: int,
        contact_id: str | None,
        status: str,
        confidence: float,
        matched_on: str,
        matched_value: str,
        source_identifier: str,
        updated_at: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO chat_contact_matches(
                chat_id, contact_id, status, confidence, matched_on,
                matched_value, source_identifier, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                contact_id,
                status,
                confidence,
                matched_on,
                matched_value,
                source_identifier,
                updated_at,
            ),
        )

    @staticmethod
    def _add_identifier(values: set[str], value: str) -> None:
        if value:
            values.add(value)

    @classmethod
    def _add_json_identifiers(cls, values: set[str], json_text: str) -> None:
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            return
        if not isinstance(parsed, list):
            return
        for item in parsed:
            if isinstance(item, str):
                cls._add_identifier(values, item)

    def _ensure_chat_for_message(self, message: Message, now: str) -> None:
        self._db.execute(
            """
            INSERT INTO chats(
                id, identifier, guid, name, service, last_message_at,
                participants_json, is_group, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                identifier = CASE
                    WHEN excluded.identifier != '' THEN excluded.identifier
                    ELSE chats.identifier
                END,
                guid = CASE WHEN excluded.guid != '' THEN excluded.guid ELSE chats.guid END,
                name = CASE WHEN excluded.name != '' THEN excluded.name ELSE chats.name END,
                last_message_at = excluded.last_message_at,
                participants_json = CASE
                    WHEN excluded.participants_json != '[]' THEN excluded.participants_json
                    ELSE chats.participants_json
                END,
                is_group = CASE
                    WHEN excluded.is_group != 0 THEN excluded.is_group
                    ELSE chats.is_group
                END,
                updated_at = excluded.updated_at
            """,
            (
                message.chat_id,
                message.chat_identifier,
                message.chat_guid,
                message.chat_name,
                message.service,
                _fmt_dt(message.date),
                json.dumps(message.participants, ensure_ascii=False),
                int(message.is_group),
                now,
            ),
        )

    def _replace_attachments(
        self,
        message_rowid: int,
        attachments: list[Attachment],
        updated_at: str,
    ) -> None:
        self._db.execute("DELETE FROM attachments WHERE message_rowid = ?", (message_rowid,))
        self._db.executemany(
            """
            INSERT INTO attachments(
                message_rowid, position, filename, transfer_name, uti, mime_type,
                total_bytes, is_sticker, original_path, local_path, archived,
                archive_error, missing, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    message_rowid,
                    idx,
                    attachment.filename,
                    attachment.transfer_name,
                    attachment.uti,
                    attachment.mime_type,
                    attachment.total_bytes,
                    int(attachment.is_sticker),
                    attachment.original_path,
                    copied.local_path,
                    int(copied.archived),
                    copied.error,
                    int(attachment.missing),
                    updated_at,
                )
                for idx, attachment in enumerate(attachments)
                for copied in [self._copy_attachment(message_rowid, idx, attachment)]
            ],
        )

    def _copy_attachment(
        self,
        message_rowid: int,
        position: int,
        attachment: Attachment,
    ) -> _CopiedAttachment:
        source_text = attachment.original_path or attachment.filename
        if attachment.missing or not source_text:
            return _CopiedAttachment(local_path="", archived=False, error="missing")

        source = Path(source_text).expanduser()
        if not source.exists():
            return _CopiedAttachment(
                local_path="",
                archived=False,
                error=f"source not found: {source}",
            )
        if not source.is_file():
            return _CopiedAttachment(
                local_path="",
                archived=False,
                error=f"source is not a file: {source}",
            )

        name_source = attachment.transfer_name or attachment.filename or source.name
        filename = _safe_filename(name_source, f"attachment-{position}")
        destination_dir = self.path.parent / "attachments" / str(message_rowid)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{position:03d}-{filename}"
        try:
            shutil.copy2(source, destination)
        except OSError as exc:
            return _CopiedAttachment(
                local_path="",
                archived=False,
                error=str(exc),
            )
        return _CopiedAttachment(
            local_path=str(destination),
            archived=True,
            error="",
        )

    def _replace_reactions(
        self,
        message_rowid: int,
        reactions: list[Reaction],
        updated_at: str,
    ) -> None:
        self._db.execute("DELETE FROM reactions WHERE message_rowid = ?", (message_rowid,))
        self._db.executemany(
            """
            INSERT INTO reactions(
                message_rowid, position, reaction_type, sender, is_from_me,
                date, emoji, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    message_rowid,
                    idx,
                    reaction.reaction_type,
                    reaction.sender,
                    int(reaction.is_from_me),
                    _fmt_dt(reaction.date),
                    reaction.emoji,
                    updated_at,
                )
                for idx, reaction in enumerate(reactions)
            ],
        )
