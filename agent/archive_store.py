"""
archive_store.py - SQLite archive for iMessage chats, messages, attachments, and reactions.

Markdown remains the approval/drafting store. This database is a local searchable archive
of iMessage data received through `imsg rpc`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import Attachment, Chat, Message, Reaction

SCHEMA_VERSION = 1


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
            """
        )
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self._db.commit()

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
                total_bytes, is_sticker, original_path, missing, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    int(attachment.missing),
                    updated_at,
                )
                for idx, attachment in enumerate(attachments)
            ],
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
