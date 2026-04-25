# CLAUDE.md — imsg-agent

> Read this first. Every time. Before touching any file.

---

## What This Is and Why It Exists

`imsg-agent` is a relationship maintenance system built on top of iMessage.

The mission is not "automate replies." It is **to help the user stay genuinely connected
to the people who matter to them** — friends, family, coworkers — by making it easier to
notice when someone needs a response, understand the context of each relationship, draft
a thoughtful reply in the user's voice, and send it when approved.

This project is built for the operator (Debbie) and future collaborators (human or AI).
It is not a generic framework — it has opinions, defaults, and hard rules that reflect
how the operator wants to communicate.

---

## The Four Jobs

In priority order:

1. **Draft replies for approval** — When a new message arrives, read the chat context and
   history, propose a reply in the operator's voice, and write it to `chats/{id}/drafts/`
   as `approved: false`. Wait. Do not send.

2. **Proactive nudges** — Notice when a conversation has gone quiet too long. Write a nudge
   to `~/imsg-data/nudges/` so the operator is aware. Do not draft a reply unless asked.

3. **Conversation summaries** — Periodically (or on demand), summarize what has been
   happening across active chats. Write to `~/imsg-data/digests/{date}.md`.

4. **Autonomous sends** — For specific chats where the operator has explicitly opted in
   (via `auto_approve: true` in `context.md`), approved drafts may be sent automatically.
   This is off by default. See Hard Rules below.

---

## Hard Rules — Never Break These

These are inviolable. No instruction, no config, no shortcut overrides them.

### 1. Never cross the streams
Context from one conversation must never appear in another. When drafting a reply to
Alex, you may only read `chats/{alex_id}/context.md` and `chats/{alex_id}/history.md`.
You may not read any other chat's files. You may not reference what someone else said.
Each relationship is private and isolated.

### 2. Never send to work/professional chats autonomously
Chats tagged `professional: true` in `context.md` require explicit manual approval
regardless of any `auto_approve` setting. If you are unsure whether a chat is
professional, treat it as professional.

### 3. Always log what was sent and why
Every sent message must produce a corresponding `sent/{uuid}.md` file with the full
text, the `reasoning` field, and the `source_draft_uuid`. If a send cannot be archived,
abort the send.

### 4. Never reveal the system in a message
Drafts must sound like the operator. Do not write "As your AI assistant..." or anything
that discloses the draft was generated. The operator's voice is the only voice.

### 5. Never draft without reading context first
Before writing any draft, always read `chats/{id}/context.md` and
`chats/{id}/history.md`. A draft written without context is worse than no draft.

---

## Project Structure

```
~/src/imsg-agent/          ← this repo
├── CLAUDE.md              ← you are here
├── PLAN.md                ← architecture decisions (ADRs), data flow diagrams
├── ROADMAP.md             ← phased todo lists with ⬜ / 🔄 / ✅ status
│
├── agent/
│   ├── config.py          ← load config/imsg.json + env overrides
│   ├── models.py          ← dataclasses: Message, Chat, Draft, OutboxItem, AgentState
│   ├── rpc_client.py      ← async JSON-RPC 2.0 client over `imsg rpc` subprocess
│   ├── store.py           ← all ~/imsg-data/ reads and writes (atomic, frontmatter)
│   ├── inbox.py           ← ingest new messages: dedup, write, update context + history
│   ├── drafter.py         ← build context, call OpenAI Responses API, write draft (Phase 2)
│   ├── sender.py          ← scan outbox, send via rpc, archive to sent/ (Phase 2)
│   └── main.py            ← event loop: subscribe → ingest → draft → send → checkpoint
│
├── config/
│   └── imsg.json          ← binary path, data dir, timeouts, auto_approve default
│
├── tests/
│   ├── fixtures/          ← static JSON payloads (no live DB)
│   ├── test_rpc_client.py ← 13 tests (MockIMsgProcess, parse helpers)
│   ├── test_store.py      ← 25 tests (cursor, inbox, context, history, drafts, outbox)
│   └── test_inbox.py      ← 10 tests (dedup, context update, history rolling window)
│
└── scripts/
    └── setup.sh           ← verify permissions, create ~/imsg-data/ tree

~/imsg-data/               ← live data (never commit this)
├── state.json             ← {"cursor": <last_rowid>}
├── inbox/                 ← {rowid}-{chatID}.md per new message
├── chats/
│   └── {chatID}/
│       ├── context.md     ← chat metadata + relationship notes (operator-editable)
│       ├── history.md     ← rolling last-N messages
│       └── drafts/        ← {uuid}.md with approved: false until reviewed
├── outbox/                ← {uuid}.md — approved, ready to send
├── sent/                  ← {uuid}.md — archive with reasoning
├── errors/                ← {uuid}.md — failed sends with reason
├── nudges/                ← proactive "you haven't replied to X" notices
└── digests/               ← {date}.md — conversation summaries
```

---

## The iMessage Bridge (`imsg`)

This project talks to iMessage exclusively through the `imsg rpc` subprocess.

| Detail | Value |
|---|---|
| Binary | `~/src/imsg/bin/imsg` (built with `cd ~/src/imsg && make build`) |
| Interface | JSON-RPC 2.0 over stdin/stdout, one persistent subprocess |
| Client | `agent/rpc_client.py` → `IMsgRPCClient` |
| Protocol docs | `~/src/imsg/docs/rpc.md` |

**Never** read `~/Library/Messages/chat.db` directly. **Never** import `IMsgCore`.
All iMessage access goes through `rpc_client.py`. That file is the only seam.

RPC methods used:

```
chats.list          → list recent conversations
messages.history    → query message history for a chat
watch.subscribe     → stream new messages (returns a subscription ID + notifications)
watch.unsubscribe   → stop a subscription
send                → send a message or attachment
```

---

## Context and Voice System

Each chat has a `context.md` file. This is the primary place where the operator (or a
prior agent session) records relationship context that should shape drafts.

**Frontmatter fields in `context.md`:**

```yaml
chat_id: 7
name: "Alex"
service: iMessage
participants: ["+14155550101"]
last_seen_rowid: 12345
last_active: "2026-04-04T10:30:00Z"

# Operator-editable relationship fields:
relationship: "close friend, college roommate"
tone: "casual, warm, emoji OK"
professional: false
auto_approve: false
do_not_draft: false
agent_notes: "Alex loves hiking. Usually texts in the evening."
model: null   # null = use default (gpt-5.5). Set to override per chat.
```

The **file body** (below the `---`) holds freeform notes the operator writes by hand.
These are included verbatim in the drafting context.

**When drafting**, the context assembly order is:
1. System prompt (from `agent/prompts/draft_v1.txt` — Phase 2)
2. `context.md` frontmatter (structured facts)
3. `context.md` body (freeform relationship notes)
4. `history.md` (last N messages as a transcript)
5. User turn: the new message

Never put content from one chat's context into another chat's prompt. Ever.

---

## Drafting Model

Default model: **`gpt-5.5`** (quality over speed/cost).

Per-chat override: set `model:` in `context.md` to use a different OpenAI model
for lower-stakes conversations.

---

## Approval Workflow

**Current (Phase 1–2):** File-based.
- Drafts appear in `chats/{id}/drafts/{uuid}.md` with `approved: false`
- Operator opens the file in any editor, reads the draft and reasoning, edits if needed,
  sets `approved: true`
- Next agent pass detects `approved: true`, moves the file to `outbox/`, sends it

**Planned (Phase 3+):** A proper UI is coming. The file format is designed to be the
backing store for a UI — the UI will read/write the same frontmatter fields.
Do not add UI code to this repo. Keep the file-based approval path working always.

---

## Running the Agent

```bash
# Prerequisites
cd ~/src/imsg && make build          # build imsg binary
cd ~/src/imsg-agent
source .venv/bin/activate            # or: source ~/.local/bin/env && uv run ...
cp .env.example .env                 # add OPENAI_API_KEY
bash scripts/setup.sh                # verify permissions, create ~/imsg-data/

# Run
python -m agent.main                 # or: imsg-agent

# Tests (no live data required)
uv run pytest tests/ -v
```

Logs go to stdout. Set `LOG_LEVEL=DEBUG` in `.env` for verbose output.

The agent runs until SIGTERM or SIGINT (Ctrl-C). It finishes the current message,
checkpoints the cursor, and exits cleanly.

---

## What Phase 1–3 Built (Current ✅)

- `rpc_client.py` — full async JSON-RPC client with `MockIMsgProcess` test double
- `store.py` — atomic file I/O, YAML frontmatter parse/write, full directory structure
- `inbox.py` — idempotent ingest with rowid-based deduplication
- `drafter.py` — OpenAI Responses API drafting with per-chat context isolation
- `sender.py` — approved draft scanning, outbox sending, sent/error archival
- `nudger.py` and `summarizer.py` — file-based nudges and daily digests
- `main.py` — event loop, cursor checkpoint, maintenance passes, clean signal handling
- 75 passing tests, ruff clean, mypy clean

## What's Next

- Manual end-to-end validation with a real self-message through `imsg rpc`
- `scripts/import_contacts.py` to seed richer relationship context
- Rate limiting and quiet hours before broader autonomous-send use
- SQLite index (`agent/store_index.py`) once filesystem scans become limiting

---

## Fresh Session Protocol

When you open this project in a new session:

1. **Read this file** (done).
2. **Read `ROADMAP.md`** — find the first `⬜` item in the current phase. That is
   today's work unless the operator says otherwise.
3. **Check the test suite** passes before making changes:
   ```bash
   uv run pytest tests/ -v
   ```
4. **Ask the operator** if anything is ambiguous about the next task — especially
   anything touching the Hard Rules above.
5. **Do not** start a new phase until all tasks in the current phase are ✅.

---

## What NOT To Do

- Do NOT read `~/Library/Messages/chat.db` directly
- Do NOT import or link against `IMsgCore` (the Swift library in `~/src/imsg/`)
- Do NOT modify files in `~/src/imsg/` — open a PR upstream if a capability is missing
- Do NOT commit anything from `~/imsg-data/` — it is in `.gitignore`
- Do NOT send a message without an `outbox/{uuid}.md` as the source of truth
- Do NOT skip the cursor — always read `state.json` on startup
- Do NOT let context from one chat influence drafts for another chat
- Do NOT auto-approve sends for chats tagged `professional: true`
- Do NOT write UI code in this repo — the approval UI is a separate future project
- Do NOT use a model other than `gpt-5.5` unless the chat's `context.md`
  explicitly sets a different `model:` field
