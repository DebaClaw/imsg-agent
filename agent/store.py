"""
store.py — All reads and writes to ~/imsg-data/.

Single source of truth for filesystem I/O. No network calls, no subprocess calls.
All writes are atomic (write to .tmp file, then rename).

Directory layout managed by this module:
    {data_dir}/state.json
    {data_dir}/inbox/{rowid}-{chat_id}.md
    {data_dir}/chats/{chat_id}/context.md
    {data_dir}/chats/{chat_id}/history.md
    {data_dir}/chats/{chat_id}/drafts/{uuid}.md
    {data_dir}/outbox/{uuid}.md
    {data_dir}/sent/{uuid}.md
    {data_dir}/errors/{uuid}.md
"""
from __future__ import annotations

# TODO (Phase 1): Implement file store.
#
# Interface to implement:
#
#   class MessageStore:
#       def __init__(self, data_dir: Path) -> None
#
#       # State
#       def read_cursor(self) -> int
#       def write_cursor(self, rowid: int) -> None
#
#       # Inbox
#       def inbox_path(self, rowid: int, chat_id: int) -> Path
#       def inbox_exists(self, rowid: int, chat_id: int) -> bool
#       def write_inbox(self, message: Message) -> None
#       def list_unprocessed_inbox(self) -> list[Path]
#       def read_inbox(self, path: Path) -> Message
#
#       # Chat context
#       def read_chat_context(self, chat_id: int) -> dict
#       def write_chat_context(self, chat_id: int, context: dict) -> None
#       def append_chat_history(self, chat_id: int, message: Message,
#                               max_messages: int = 20) -> None
#       def read_chat_history(self, chat_id: int) -> list[Message]
#
#       # Drafts
#       def write_draft(self, draft: Draft) -> None
#       def list_approved_drafts(self) -> list[Path]
#       def read_draft(self, path: Path) -> Draft
#       def move_draft_to_outbox(self, draft: Draft) -> None
#
#       # Outbox / Sent / Errors
#       def list_outbox(self) -> list[Path]
#       def read_outbox_item(self, path: Path) -> OutboxItem
#       def move_to_sent(self, item: OutboxItem) -> None
#       def move_to_errors(self, item: OutboxItem, reason: str) -> None
#
# File format: YAML frontmatter + body (see CLAUDE.md § Data Store)
# Atomic writes: write to path.with_suffix('.tmp'), then Path.rename()
