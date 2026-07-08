---
# ── Identity ──────────────────────────────────────────────────────────────────
chat_id: 0                        # filled by imsg-agent on first ingest
name: ""                          # display name for this chat
service: iMessage                 # iMessage | SMS
participants:
  - "+1XXXXXXXXXX"                # one entry per participant handle

# written by imsg-agent — do not edit by hand
last_seen_rowid: 0
last_active: null

# ── Relationship ──────────────────────────────────────────────────────────────
relationship: ""
# Short description of who this person is and how Jon knows them.
# e.g., "college roommate, close friend since 2003"
# e.g., "Blake's girlfriend, met at Thanksgiving 2025"

relationship_tier: ""
# One of: family | close-friend | friend | colleague | professional | acquaintance
# Controls default drafting register and Rue's escalation threshold.
# Omit or leave blank to default to "friend" (measured, no slang).

tone: ""
# Freeform tone description that overrides tier defaults.
# e.g., "casual, warm, emoji fine, uses lowercase"
# e.g., "professional but friendly, full sentences"
# e.g., "dry humor, sarcasm is OK"

professional: false
# true = requires Jon's manual approval regardless of any auto_approve setting.
# Set true for: coworkers, clients, vendors, anyone where a bad draft has real consequences.

# ── Drafting controls ─────────────────────────────────────────────────────────
auto_approve: false
# ⚠️ true = the DRAFTER marks drafts approved at creation time, with NO review at all.
# The sender then sends within seconds. This bypasses the reviewer entirely.
# For gated automation use reviewer_delegate below instead. Leave this false.

reviewer_delegate: false
# true = the automated draft reviewer (agent/reviewer.py, cron every 5 min) may approve
# drafts in this chat — but only when professional: false is set explicitly AND every
# safety gate passes (no profanity/reveal/sensitive topics/commitments, fresh, rate-limited).
# This is the intended per-chat opt-in for safe full automation.

allow_profanity: false
# true = waive the reviewer's profanity gate for this chat (casual chats where swearing
# is part of the normal register).

group_delegated: false
# true = allow the reviewer to approve in this GROUP chat. Group chats are otherwise
# always escalated. Only set for a group explicitly delegated by Jon.

do_not_draft: false
# true = skip AI drafting entirely for this chat.
# Use for: group chats Jon wants to handle personally, sensitive ongoing situations, etc.

model: null
# null = use the system default (gpt-5.5).
# Set to a model id to use a different model for this specific chat.
# e.g., "gpt-5.4-mini" for lightweight chats where quality matters less.

# ── Agent guidance ────────────────────────────────────────────────────────────
agent_notes: ""
# Stable facts the drafter should always know.
# e.g., "Usually texts in the evening. Just had a baby. Works at Boeing."
# e.g., "Has been struggling with anxiety lately — keep drafts warm and low-pressure."
# e.g., "Jon owes him a call. Drafts should acknowledge the lag."

topics: []
# Recurring topics or shared interests that naturally come up.
# e.g., ["hiking", "Seattle sports", "her rescue dog Mango"]

alert_on: ""
# Keywords/phrases where Rue and the reviewer escalate to Jon instead of approving.
# Comma-separated string or YAML list — matched word-for-word against outgoing drafts,
# so use concrete keywords, not prose.
# e.g., "health, money, family conflict"  or  [health, money, family conflict]
# e.g., "Messages that seem distressed or need a real decision"
# e.g., "Anything that requires Jon to show up somewhere"
---

<!-- Freeform relationship notes — write whatever helps the drafter.
     This section is included verbatim in the draft context.
     Good things to capture:
       - How Jon and this person met, what their history is
       - Current state of the relationship (close, drifted, reconnecting, strained)
       - Recurring topics, inside references, things that matter to this person
       - Things to never say or commit to in a draft
       - Anything that would make a bad draft obvious
-->
