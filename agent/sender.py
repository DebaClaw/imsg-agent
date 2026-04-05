"""
sender.py — Send approved outbox items via imsg rpc.

Scans outbox/ for .md files, calls rpc_client.send(), archives results.
This module does NOT decide what to send — it only executes what is in outbox/.
"""
from __future__ import annotations

# TODO (Phase 2): Implement sender.
#
# Interface to implement:
#
#   class Sender:
#       def __init__(self, store: MessageStore, rpc: IMsgRPCClient) -> None
#
#       async def run_pass(self) -> int
#           Process all current outbox items. Returns count of items sent.
#           For each item in store.list_outbox():
#           1. Read OutboxItem from file
#           2. Validate attachment path (must be under data_dir/outbox/attachments/)
#           3. Call rpc.send(chat_id=item.chat_id, text=item.text, ...)
#           4. On success: store.move_to_sent(item)
#           5. On failure: store.move_to_errors(item, reason=str(error))
#
# Security: attachment_path validation
#   resolved = Path(item.attachment_path).resolve()
#   allowed = (data_dir / "outbox" / "attachments").resolve()
#   assert resolved.is_relative_to(allowed), "attachment outside allowed path"
