"""
drafter.py — Propose responses using Claude API.

Reads unprocessed inbox files, builds chat context, calls Claude,
and writes draft files to chats/{chatID}/drafts/.

This is the ONLY module that calls an external AI API.
"""
from __future__ import annotations

# TODO (Phase 2): Implement AI drafting.
#
# Interface to implement:
#
#   class Drafter:
#       def __init__(self, store: MessageStore, api_key: str,
#                    model: str = "claude-opus-4-5") -> None
#
#       async def process_inbox(self, inbox_path: Path) -> Draft | None
#           Returns None if chat has do_not_draft: true or message too old.
#           Steps:
#           1. Read message from inbox_path
#           2. Read chats/{chat_id}/context.md → check do_not_draft
#           3. Read chats/{chat_id}/history.md → last N messages
#           4. Build messages array for Claude API
#           5. Call Claude API
#           6. Parse response → proposed_text, reasoning
#           7. store.write_draft(Draft(..., approved=False))
#           8. Return draft
#
# System prompt lives in agent/prompts/draft_v1.txt
# prompt_version field in draft frontmatter references the prompt file version.
#
# Context assembly:
#   system = base_prompt + chat_context (name, relationship, agent_notes)
#   user   = history (last N messages as transcript) + "New message:\n{text}"
#
# Never include raw message text in the system prompt — only in user turn.
# (Mitigates prompt injection from received message content.)
