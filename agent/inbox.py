"""
inbox.py — Consume new messages from rpc_client and write to store.

Owns the ingest half of the agent lifecycle:
    poll → deduplicate → write inbox file → update chat context → update history
"""
from __future__ import annotations

# TODO (Phase 1): Implement inbox ingestion.
#
# Interface to implement:
#
#   class InboxProcessor:
#       def __init__(self, store: MessageStore) -> None
#
#       async def process(self, message: Message) -> bool
#           Returns True if written (new), False if skipped (duplicate).
#           Steps:
#           1. Check store.inbox_exists(message.rowid, message.chat_id) → skip if True
#           2. store.write_inbox(message)
#           3. store.append_chat_history(message.chat_id, message)
#           4. store.write_chat_context(message.chat_id, ...) — update last_seen
#           5. Return True
#
# Deduplication guarantee:
#   Even if the cursor regresses (restart without checkpoint), the same rowid
#   will not produce a second inbox file.
