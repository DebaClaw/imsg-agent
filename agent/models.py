"""
Core data models for imsg-agent.

These mirror the shapes returned by `imsg rpc` JSON-RPC responses, plus
the additional fields tracked by the agent's data store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Chat:
    id: int
    identifier: str
    name: str
    service: str
    last_message_at: datetime
    guid: str = ""
    participants: list[str] = field(default_factory=list)


@dataclass
class Attachment:
    filename: str
    transfer_name: str
    uti: str
    mime_type: str
    total_bytes: int
    is_sticker: bool
    original_path: str
    missing: bool


@dataclass
class Reaction:
    reaction_type: str  # love, like, dislike, laugh, emphasis, question, custom
    sender: str
    is_from_me: bool
    date: datetime
    emoji: str


@dataclass
class Message:
    rowid: int
    chat_id: int
    guid: str
    sender: str
    text: str
    date: datetime
    is_from_me: bool
    service: str
    has_attachments: bool
    attachments: list[Attachment] = field(default_factory=list)
    reactions: list[Reaction] = field(default_factory=list)
    reply_to_guid: str | None = None
    thread_originator_guid: str | None = None
    destination_caller_id: str | None = None
    is_reaction: bool = False
    reaction_type: str | None = None


@dataclass
class Draft:
    uuid: str
    chat_id: int
    target_identifier: str
    created_at: datetime
    proposed_text: str
    reasoning: str
    prompt_version: str
    approved: bool = False
    # rowid of the inbox message that triggered this draft
    source_rowid: int | None = None
    model: str | None = None
    auto_approved: bool = False


@dataclass
class OutboxItem:
    uuid: str
    chat_id: int
    target_identifier: str
    text: str
    attachment_path: str | None  # must be under ~/imsg-data/outbox/attachments/ only
    created_at: datetime
    source_draft_uuid: str | None = None
    source_rowid: int | None = None
    reasoning: str = ""
    model: str | None = None
    auto_approved: bool = False


@dataclass
class SentItem:
    uuid: str
    chat_id: int
    text: str
    sent_at: datetime
    source_draft_uuid: str | None = None
    source_rowid: int | None = None
    reasoning: str = ""


@dataclass
class AgentState:
    cursor: int  # last processed message rowid
