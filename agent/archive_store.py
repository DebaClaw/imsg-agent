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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from .contact_enrichment import (
    ContactRecord,
    ContactsEnrichResult,
    ContactsSyncResult,
    normalize_identifier,
)
from .models import Attachment, Chat, Message, Reaction

SCHEMA_VERSION = 6
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
ArchiveRow = dict[str, object]


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


def _fts_query(value: str) -> str:
    tokens = [token for token in re.split(r"\s+", value.strip()) if token]
    if not tokens:
        return '""'
    return " ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _hours_since(value: str, *, now: datetime) -> float:
    try:
        parsed = _parse_dt(value)
    except ValueError:
        return 0.0
    return max(0.0, (now - parsed).total_seconds() / 3600)


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

    def __enter__(self) -> IMessageArchive:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        previous_schema = self._read_schema_version()
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

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(text, content='messages', content_rowid='rowid');

            CREATE TRIGGER IF NOT EXISTS messages_fts_after_insert
            AFTER INSERT ON messages
            BEGIN
                INSERT INTO messages_fts(rowid, text)
                VALUES (new.rowid, new.text);
            END;

            CREATE TRIGGER IF NOT EXISTS messages_fts_after_delete
            AFTER DELETE ON messages
            BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, text)
                VALUES('delete', old.rowid, old.text);
            END;

            CREATE TRIGGER IF NOT EXISTS messages_fts_after_text_update
            AFTER UPDATE OF text ON messages
            BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, text)
                VALUES('delete', old.rowid, old.text);
                INSERT INTO messages_fts(rowid, text)
                VALUES (new.rowid, new.text);
            END;

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
                photo_data_uri TEXT NOT NULL DEFAULT '',
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

            CREATE TABLE IF NOT EXISTS manual_contact_links (
                chat_id INTEGER NOT NULL,
                contact_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, contact_id),
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                FOREIGN KEY (contact_id) REFERENCES contacts(contact_id) ON DELETE CASCADE
            );
            """
        )
        self._ensure_column("attachments", "local_path", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("attachments", "archived", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("attachments", "archive_error", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("contacts", "photo_data_uri", "TEXT NOT NULL DEFAULT ''")
        if previous_schema < 4:
            self._db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self._db.commit()

    def _read_schema_version(self) -> int:
        row = self._db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

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

    def read_agent_cursor(self) -> int:
        value = self.get_meta("agent_draft_cursor", "0")
        try:
            return int(value)
        except ValueError:
            return 0

    def write_agent_cursor(self, rowid: int) -> None:
        self.set_meta("agent_draft_cursor", str(rowid))
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

    def archive_stats(self) -> dict[str, int]:
        row = self._db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM chats) AS chats,
                (SELECT COUNT(*) FROM messages) AS messages,
                (SELECT COUNT(*) FROM messages_fts) AS search_indexed_messages,
                (SELECT COUNT(*) FROM attachments) AS attachments,
                (SELECT COUNT(*) FROM attachments WHERE archived = 1) AS saved_attachments,
                (SELECT COUNT(*) FROM attachments WHERE missing = 1) AS missing_attachments,
                (
                    SELECT COUNT(*)
                    FROM attachments
                    WHERE archive_error != ''
                ) AS attachment_errors,
                (SELECT COUNT(*) FROM reactions) AS reactions,
                (SELECT COUNT(*) FROM contacts) AS contacts,
                (SELECT COUNT(*) FROM contact_points) AS contact_points,
                (
                    SELECT COUNT(*)
                    FROM chat_contact_matches
                    WHERE status = 'matched'
                ) AS matched_chats,
                (
                    SELECT COUNT(DISTINCT chat_id)
                    FROM chat_contact_matches
                    WHERE status = 'ambiguous'
                ) AS ambiguous_chats,
                (
                    SELECT COUNT(*)
                    FROM chat_contact_matches
                    WHERE status = 'unresolved'
                ) AS unresolved_chats
            """
        ).fetchone()
        keys = row.keys()
        return {key: int(row[key]) for key in keys}

    def search_messages(
        self,
        query: str,
        *,
        limit: int = 50,
        chat_id: int | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[ArchiveRow]:
        where = ["messages_fts MATCH ?"]
        params: list[object] = [_fts_query(query)]
        if chat_id is not None:
            where.append("m.chat_id = ?")
            params.append(chat_id)
        if since:
            where.append("m.date >= ?")
            params.append(since)
        if until:
            where.append("m.date < ?")
            params.append(until)
        params.append(limit)
        rows = self._db.execute(
            f"""
            SELECT
                m.rowid AS message_rowid,
                m.chat_id AS chat_id,
                c.name AS chat_name,
                m.sender AS sender,
                (
                    SELECT contacts.full_name
                    FROM chat_contact_matches matches
                    JOIN contacts ON contacts.contact_id = matches.contact_id
                    WHERE matches.chat_id = m.chat_id
                        AND matches.source_identifier = m.sender
                        AND matches.status = 'matched'
                        AND contacts.full_name != ''
                    ORDER BY matches.confidence DESC, contacts.full_name
                    LIMIT 1
                ) AS sender_name,
                m.date AS message_at,
                m.is_from_me AS is_from_me,
                m.text AS text,
                m.has_attachments AS has_attachments,
                bm25(messages_fts) AS rank,
                (
                    SELECT GROUP_CONCAT(full_name, ', ')
                    FROM (
                        SELECT DISTINCT contacts.full_name AS full_name
                        FROM chat_contact_matches matches
                        JOIN contacts ON contacts.contact_id = matches.contact_id
                        WHERE matches.chat_id = m.chat_id
                            AND matches.status = 'matched'
                            AND contacts.full_name != ''
                        ORDER BY contacts.full_name
                    )
                ) AS contacts
            FROM messages_fts
            JOIN messages m ON m.rowid = messages_fts.rowid
            JOIN chats c ON c.id = m.chat_id
            WHERE {" AND ".join(where)}
            ORDER BY rank ASC, m.date DESC, m.rowid DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def inbound_messages_after(self, rowid: int, *, limit: int = 50) -> list[Message]:
        rows = self._db.execute(
            """
            SELECT
                m.rowid, m.chat_id, m.guid, m.sender, m.text, m.date, m.is_from_me,
                m.service, m.has_attachments, m.reply_to_guid,
                m.thread_originator_guid, m.destination_caller_id, m.is_reaction,
                m.reaction_type, m.chat_identifier, m.chat_guid, m.chat_name,
                m.participants_json, m.is_group
            FROM messages m
            WHERE m.rowid > ?
                AND m.is_from_me = 0
                AND m.is_reaction = 0
            ORDER BY m.rowid ASC
            LIMIT ?
            """,
            (rowid, limit),
        ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def message_by_rowid(self, rowid: int) -> Message | None:
        row = self._db.execute(
            """
            SELECT
                m.rowid, m.chat_id, m.guid, m.sender, m.text, m.date, m.is_from_me,
                m.service, m.has_attachments, m.reply_to_guid,
                m.thread_originator_guid, m.destination_caller_id, m.is_reaction,
                m.reaction_type, m.chat_identifier, m.chat_guid, m.chat_name,
                m.participants_json, m.is_group
            FROM messages m
            WHERE m.rowid = ?
            """,
            (rowid,),
        ).fetchone()
        return self._message_from_row(row) if row is not None else None

    def latest_message_for_chat(self, chat_id: int) -> Message | None:
        row = self._db.execute(
            """
            SELECT
                m.rowid, m.chat_id, m.guid, m.sender, m.text, m.date, m.is_from_me,
                m.service, m.has_attachments, m.reply_to_guid,
                m.thread_originator_guid, m.destination_caller_id, m.is_reaction,
                m.reaction_type, m.chat_identifier, m.chat_guid, m.chat_name,
                m.participants_json, m.is_group
            FROM messages m
            WHERE m.chat_id = ?
            ORDER BY m.date DESC, m.rowid DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        return self._message_from_row(row) if row is not None else None

    def chat_context_seed(self, chat_id: int) -> ArchiveRow:
        row = self._db.execute(
            """
            SELECT id, identifier, guid, name, service, last_message_at,
                participants_json, is_group
            FROM chats
            WHERE id = ?
            """,
            (chat_id,),
        ).fetchone()
        if row is None:
            return {}
        data = self._row_to_dict(row)
        data["participants"] = self._loads_json_list(str(data.pop("participants_json", "[]")))
        return data

    def chat_history_markdown(
        self,
        chat_id: int,
        *,
        through_rowid: int,
        limit: int = 20,
    ) -> str:
        rows = self._db.execute(
            """
            SELECT rowid, sender, text, date, is_from_me
            FROM messages
            WHERE chat_id = ?
                AND rowid <= ?
            ORDER BY date DESC, rowid DESC
            LIMIT ?
            """,
            (chat_id, through_rowid, limit),
        ).fetchall()
        entries: list[str] = []
        for row in reversed(rows):
            sender = "me" if int(row["is_from_me"]) else str(row["sender"] or "")
            direction = "→ me" if int(row["is_from_me"]) else f"← {sender}"
            body_text = str(row["text"] or "") or "_(no text)_"
            entries.append(
                f"<!-- rowid:{int(row['rowid'])} -->\n"
                f"**{direction}** _{row['date']}_\n\n"
                f"{body_text}\n"
            )
        return "\n".join(entries)

    def chat_messages(
        self,
        chat_id: int,
        *,
        limit: int = 50,
        before: str | None = None,
        before_rowid: int | None = None,
        after: str | None = None,
    ) -> list[ArchiveRow]:
        where = ["m.chat_id = ?"]
        params: list[object] = [chat_id]
        if before:
            where.append("m.date < ?")
            params.append(before)
        if before_rowid is not None:
            where.append("m.rowid < ?")
            params.append(before_rowid)
        if after:
            where.append("m.date >= ?")
            params.append(after)
        params.append(limit)
        rows = self._db.execute(
            f"""
            WITH matched_contacts AS (
                SELECT matches.source_identifier, contacts.full_name
                FROM chat_contact_matches matches
                JOIN contacts ON contacts.contact_id = matches.contact_id
                WHERE matches.chat_id = ?
                    AND matches.status = 'matched'
                    AND contacts.full_name != ''
            ),
            sender_contacts AS (
                SELECT source_identifier, MIN(full_name) AS sender_name
                FROM matched_contacts
                GROUP BY source_identifier
            ),
            chat_contacts AS (
                SELECT GROUP_CONCAT(DISTINCT full_name) AS contacts
                FROM matched_contacts
            )
            SELECT
                m.rowid AS message_rowid,
                m.chat_id AS chat_id,
                c.name AS chat_name,
                m.sender AS sender,
                sender_contacts.sender_name AS sender_name,
                m.date AS message_at,
                m.is_from_me AS is_from_me,
                m.text AS text,
                m.has_attachments AS has_attachments,
                chat_contacts.contacts AS contacts
            FROM messages m
            JOIN chats c ON c.id = m.chat_id
            LEFT JOIN sender_contacts ON sender_contacts.source_identifier = m.sender
            CROSS JOIN chat_contacts
            WHERE {" AND ".join(where)}
            ORDER BY m.date DESC, m.rowid DESC
            LIMIT ?
            """,
            [chat_id, *params],
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def recent_chats(self, *, limit: int = 20) -> list[ArchiveRow]:
        rows = self._db.execute(
            """
            SELECT
                c.id AS chat_id,
                c.name AS name,
                c.identifier AS identifier,
                c.service AS service,
                COUNT(m.rowid) AS messages,
                MAX(m.date) AS last_message_at,
                (
                    SELECT sender
                    FROM messages latest
                    WHERE latest.chat_id = c.id
                    ORDER BY latest.date DESC, latest.rowid DESC
                    LIMIT 1
                ) AS last_sender,
                (
                    SELECT text
                    FROM messages latest
                    WHERE latest.chat_id = c.id
                    ORDER BY latest.date DESC, latest.rowid DESC
                    LIMIT 1
                ) AS last_text,
                (
                    SELECT GROUP_CONCAT(full_name, ', ')
                    FROM (
                        SELECT DISTINCT contacts.full_name AS full_name
                        FROM chat_contact_matches matches
                        JOIN contacts ON contacts.contact_id = matches.contact_id
                        WHERE matches.chat_id = c.id
                            AND matches.status = 'matched'
                            AND contacts.full_name != ''
                        ORDER BY contacts.full_name
                    )
                ) AS contacts
            FROM chats c
            LEFT JOIN messages m ON m.chat_id = c.id
            GROUP BY c.id
            ORDER BY COALESCE(MAX(m.date), c.last_message_at) DESC, c.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def attention_items(self, *, limit: int = 50) -> list[ArchiveRow]:
        rows = self._db.execute(
            """
            SELECT
                c.id AS chat_id,
                c.name AS name,
                c.identifier AS identifier,
                c.is_group AS is_group,
                latest.rowid AS message_rowid,
                latest.sender AS sender,
                latest.date AS last_message_at,
                latest.text AS last_text,
                latest.has_attachments AS has_attachments,
                (
                    SELECT COUNT(*)
                    FROM messages count_messages
                    WHERE count_messages.chat_id = c.id
                ) AS messages,
                (
                    SELECT GROUP_CONCAT(full_name, ', ')
                    FROM (
                        SELECT DISTINCT contacts.full_name AS full_name
                        FROM chat_contact_matches matches
                        JOIN contacts ON contacts.contact_id = matches.contact_id
                        WHERE matches.chat_id = c.id
                            AND matches.status = 'matched'
                            AND contacts.full_name != ''
                        ORDER BY contacts.full_name
                    )
                ) AS contacts
            FROM chats c
            JOIN messages latest ON latest.rowid = (
                SELECT rowid
                FROM messages
                WHERE chat_id = c.id
                ORDER BY date DESC, rowid DESC
                LIMIT 1
            )
            WHERE latest.is_from_me = 0
                AND latest.is_reaction = 0
            ORDER BY latest.date DESC, latest.rowid DESC
            LIMIT ?
            """,
            (limit * 4,),
        ).fetchall()
        now = datetime.now(UTC)
        items = [self._score_attention_item(self._row_to_dict(row), now=now) for row in rows]
        items.sort(
            key=lambda row: (
                -int(str(row["score"])),
                -float(str(row["hours_waiting"])),
                str(row["last_message_at"]),
            )
        )
        return items[:limit]

    def orbit_threads(
        self,
        *,
        limit: int = 16,
        offset: int = 0,
        direction: str = "incoming",
        since: str | None = None,
        exclude_chat_ids: set[int] | None = None,
    ) -> ArchiveRow:
        if direction not in {"incoming", "outgoing", "either"}:
            raise ValueError("Unknown Orbit direction")
        where = ["latest.rank = 1", "latest.is_reaction = 0"]
        params: list[object] = []
        if direction == "incoming":
            where.append("latest.is_from_me = 0")
        elif direction == "outgoing":
            where.append("latest.is_from_me = 1")
        if exclude_chat_ids:
            placeholders = ", ".join("?" for _ in exclude_chat_ids)
            where.append(f"c.id NOT IN ({placeholders})")
            params.extend(sorted(exclude_chat_ids))
        if since:
            where.append("latest.date >= ?")
            params.append(since)
        base = "FROM latest JOIN chats c ON c.id = latest.chat_id WHERE " + " AND ".join(where)
        latest_cte = """
            WITH latest AS (
                SELECT m.*, ROW_NUMBER() OVER (
                    PARTITION BY m.chat_id ORDER BY m.date DESC, m.rowid DESC
                ) AS rank
                FROM messages m
            )
        """
        total = int(self._db.execute(f"{latest_cte} SELECT COUNT(*) {base}", params).fetchone()[0])
        cutoff = _fmt_dt(datetime.now(UTC) - timedelta(days=90))
        rows = self._db.execute(
            latest_cte
            + """
            SELECT
                c.id AS chat_id,
                c.name AS name,
                c.identifier AS identifier,
                c.is_group AS is_group,
                latest.rowid AS message_rowid,
                latest.sender AS sender,
                latest.date AS last_message_at,
                latest.text AS last_text,
                latest.is_from_me AS latest_is_from_me,
                latest.has_attachments AS has_attachments,
                (SELECT COUNT(*) FROM messages counted WHERE counted.chat_id = c.id) AS messages,
                (SELECT COUNT(*) FROM messages inbound WHERE inbound.chat_id = c.id
                    AND inbound.date >= ? AND inbound.is_from_me = 0) AS inbound_90d,
                (SELECT COUNT(*) FROM messages outbound WHERE outbound.chat_id = c.id
                    AND outbound.date >= ? AND outbound.is_from_me = 1) AS outbound_90d,
                (
                    SELECT GROUP_CONCAT(full_name, ', ') FROM (
                        SELECT contacts.full_name
                        FROM contacts
                        WHERE contacts.contact_id IN (
                            SELECT contact_id FROM chat_contact_matches
                            WHERE chat_id = c.id AND status = 'matched'
                            UNION
                            SELECT contact_id FROM manual_contact_links
                            WHERE chat_id = c.id
                        ) AND contacts.full_name != ''
                        ORDER BY contacts.full_name
                    )
                ) AS contacts,
                (
                    SELECT GROUP_CONCAT(contact_id, ',') FROM (
                        SELECT contact_id FROM chat_contact_matches
                        WHERE chat_id = c.id AND status = 'matched'
                        UNION
                        SELECT contact_id FROM manual_contact_links
                        WHERE chat_id = c.id
                        ORDER BY contact_id
                    )
                ) AS contact_ids
            """
            + base
            + " ORDER BY latest.date DESC, latest.rowid DESC LIMIT ? OFFSET ?",
            [cutoff, cutoff, *params, limit, offset],
        ).fetchall()
        items: list[ArchiveRow] = []
        now = datetime.now(UTC)
        for source in rows:
            row = self._row_to_dict(source)
            if bool(row.get("latest_is_from_me")):
                row["score"] = 0
                row["hours_waiting"] = round(_hours_since(str(row["last_message_at"]), now=now), 2)
                row["reason"] = "latest message is outbound"
            else:
                row = self._score_attention_item(row, now=now)
            items.append(row)
        next_offset = offset + len(items)
        return {
            "items": items,
            "limit": limit,
            "offset": offset,
            "total": total,
            "next_offset": next_offset if next_offset < total else None,
        }

    def needs_reply(self, *, limit: int = 50) -> list[ArchiveRow]:
        rows = self._db.execute(
            """
            WITH latest_messages AS (
                SELECT
                    m.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY m.chat_id
                        ORDER BY m.date DESC, m.rowid DESC
                    ) AS rank
                FROM messages m
            )
            SELECT
                c.id AS chat_id,
                c.name AS name,
                c.identifier AS identifier,
                latest_messages.sender AS sender,
                latest_messages.date AS last_message_at,
                latest_messages.text AS last_text,
                (
                    SELECT GROUP_CONCAT(full_name, ', ')
                    FROM (
                        SELECT DISTINCT contacts.full_name AS full_name
                        FROM chat_contact_matches matches
                        JOIN contacts ON contacts.contact_id = matches.contact_id
                        WHERE matches.chat_id = c.id
                            AND matches.status = 'matched'
                            AND contacts.full_name != ''
                        ORDER BY contacts.full_name
                    )
                ) AS contacts
            FROM latest_messages
            JOIN chats c ON c.id = latest_messages.chat_id
            WHERE latest_messages.rank = 1
                AND latest_messages.is_from_me = 0
                AND latest_messages.is_reaction = 0
            ORDER BY latest_messages.date DESC, latest_messages.rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def unresolved_contact_chats(self, *, limit: int = 50) -> list[ArchiveRow]:
        rows = self._db.execute(
            """
            SELECT
                c.id AS chat_id,
                c.name AS name,
                c.identifier AS identifier,
                matches.source_identifier AS source_identifier,
                matches.matched_value AS normalized_value,
                matches.updated_at AS updated_at
            FROM chat_contact_matches matches
            JOIN chats c ON c.id = matches.chat_id
            WHERE matches.status = 'unresolved'
            ORDER BY c.last_message_at DESC, c.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def contacts(self, *, limit: int = 50, query: str = "") -> list[ArchiveRow]:
        page = self.contacts_page(limit=limit, offset=0, query=query)
        return cast(list[ArchiveRow], page["items"])

    def contacts_page(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        query: str = "",
    ) -> ArchiveRow:
        pattern = f"%{query.strip()}%"
        total = int(
            self._db.execute(
                """
                SELECT COUNT(*) FROM contacts
                WHERE full_name LIKE ? OR organization_name LIKE ?
                """,
                (pattern, pattern),
            ).fetchone()[0]
        )
        rows = self._db.execute(
            """
            SELECT
                contacts.contact_id,
                contacts.full_name,
                contacts.organization_name,
                contacts.organization_title,
                contacts.notes,
                contacts.categories_json,
                contacts.photo_data_uri,
                COUNT(contact_points.value) AS contact_points
            FROM contacts
            LEFT JOIN contact_points ON contact_points.contact_id = contacts.contact_id
            WHERE contacts.full_name LIKE ?
                OR contacts.organization_name LIKE ?
            GROUP BY contacts.contact_id
            ORDER BY contacts.full_name COLLATE NOCASE, contacts.contact_id
            LIMIT ? OFFSET ?
            """,
            (pattern, pattern, limit, offset),
        ).fetchall()
        items = [self._row_to_dict(row) for row in rows]
        next_offset = offset + len(items)
        return {
            "items": items,
            "limit": limit,
            "offset": offset,
            "total": total,
            "next_offset": next_offset if next_offset < total else None,
        }

    def contact(self, contact_id: str) -> ArchiveRow:
        row = self._db.execute(
            """
            SELECT contact_id, full_name, given_name, family_name, organization_name,
                organization_title, birthday, notes, categories_json, metadata_json,
                photo_data_uri, updated_at
            FROM contacts WHERE contact_id = ?
            """,
            (contact_id,),
        ).fetchone()
        if row is None:
            return {}
        detail = self._row_to_dict(row)
        points = self._db.execute(
            """
            SELECT kind, value, original_value, label, primary_flag
            FROM contact_points WHERE contact_id = ?
            ORDER BY primary_flag DESC, kind, value
            """,
            (contact_id,),
        ).fetchall()
        detail["points"] = [self._row_to_dict(point) for point in points]
        return detail

    def chat_contacts(self, chat_id: int) -> list[ArchiveRow]:
        rows = self._db.execute(
            """
            SELECT DISTINCT contacts.contact_id, contacts.full_name, contacts.organization_name,
                contacts.organization_title, contacts.notes, contacts.categories_json,
                contacts.photo_data_uri,
                CASE WHEN manual.contact_id IS NULL THEN 0 ELSE 1 END AS manual
            FROM contacts
            LEFT JOIN chat_contact_matches matches
                ON matches.contact_id = contacts.contact_id
                AND matches.chat_id = ?
                AND matches.status IN ('matched', 'ambiguous')
            LEFT JOIN manual_contact_links manual
                ON manual.contact_id = contacts.contact_id AND manual.chat_id = ?
            WHERE matches.contact_id IS NOT NULL OR manual.contact_id IS NOT NULL
            ORDER BY manual DESC, contacts.full_name COLLATE NOCASE
            """,
            (chat_id, chat_id),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def link_chat_contact(self, chat_id: int, contact_id: str) -> None:
        if self._db.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is None:
            raise ValueError("Chat not found")
        contact = self._db.execute(
            "SELECT 1 FROM contacts WHERE contact_id = ?", (contact_id,)
        ).fetchone()
        if contact is None:
            raise ValueError("Contact not found")
        with self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO manual_contact_links(chat_id, contact_id, created_at)
                VALUES(?, ?, ?)
                """,
                (chat_id, contact_id, _fmt_dt(datetime.now(UTC))),
            )

    def unlink_chat_contact(self, chat_id: int, contact_id: str) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM manual_contact_links WHERE chat_id = ? AND contact_id = ?",
                (chat_id, contact_id),
            )

    def attachment_issues(self, *, limit: int = 50) -> list[ArchiveRow]:
        rows = self._db.execute(
            """
            SELECT
                attachments.message_rowid AS message_rowid,
                messages.chat_id AS chat_id,
                chats.name AS chat_name,
                messages.date AS message_at,
                attachments.position AS position,
                attachments.transfer_name AS transfer_name,
                attachments.original_path AS original_path,
                attachments.missing AS missing,
                attachments.archived AS archived,
                attachments.archive_error AS archive_error
            FROM attachments
            JOIN messages ON messages.rowid = attachments.message_rowid
            JOIN chats ON chats.id = messages.chat_id
            WHERE attachments.archived = 0
                OR attachments.missing = 1
                OR attachments.archive_error != ''
            ORDER BY messages.date DESC, attachments.message_rowid DESC, attachments.position
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

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
                        categories_json, metadata_json, photo_data_uri, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        contact.photo_data_uri,
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

    @staticmethod
    def _loads_json_list(value: str) -> list[str]:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed]

    @classmethod
    def _message_from_row(cls, row: sqlite3.Row) -> Message:
        return Message(
            rowid=int(row["rowid"]),
            chat_id=int(row["chat_id"]),
            guid=str(row["guid"] or ""),
            sender=str(row["sender"] or ""),
            text=str(row["text"] or ""),
            date=_parse_dt(str(row["date"])),
            is_from_me=bool(row["is_from_me"]),
            service=str(row["service"] or ""),
            has_attachments=bool(row["has_attachments"]),
            reply_to_guid=row["reply_to_guid"] or None,
            thread_originator_guid=row["thread_originator_guid"] or None,
            destination_caller_id=row["destination_caller_id"] or None,
            is_reaction=bool(row["is_reaction"]),
            reaction_type=row["reaction_type"] or None,
            chat_identifier=str(row["chat_identifier"] or ""),
            chat_guid=str(row["chat_guid"] or ""),
            chat_name=str(row["chat_name"] or ""),
            participants=cls._loads_json_list(str(row["participants_json"] or "[]")),
            is_group=bool(row["is_group"]),
        )

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

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> ArchiveRow:
        keys = row.keys()
        return {key: row[key] for key in keys}

    @staticmethod
    def _score_attention_item(row: ArchiveRow, *, now: datetime) -> ArchiveRow:
        hours_waiting = _hours_since(str(row["last_message_at"]), now=now)
        text = str(row.get("last_text") or "")
        contacts = str(row.get("contacts") or "")
        score = 40
        reasons = ["latest message is inbound"]
        if "?" in text:
            score += 12
            reasons.append("asks a question")
        if hours_waiting >= 24:
            boost = min(24, int(hours_waiting // 24) * 6)
            score += boost
            reasons.append(f"waiting {int(hours_waiting // 24)}d")
        elif hours_waiting >= 4:
            score += 4
            reasons.append("waiting several hours")
        if contacts:
            score += 5
            reasons.append("matched contact")
        if int(str(row.get("has_attachments") or 0)):
            score += 3
            reasons.append("has attachment")
        if int(str(row.get("is_group") or 0)):
            score -= 12
            reasons.append("group chat")
        row["score"] = max(0, min(100, score))
        row["hours_waiting"] = round(hours_waiting, 2)
        row["reason"] = "; ".join(reasons)
        return row

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
