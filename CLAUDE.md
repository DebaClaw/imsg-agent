# CLAUDE.md — imsg-agent

Context guide for AI assistants working in this repository. Read this before touching any file.

---

## What This Project Is

`imsg-agent` is an orchestration layer that sits **on top of** the `imsg` tool. It enables AI
assistants (and human operators) to fully manage iMessage communications: checking for new
messages, maintaining per-chat context, drafting responses, approving them, and sending.

It does NOT read the Messages database directly. It does NOT use `IMsgCore` as a Swift library
dependency. All iMessage access goes through the **`imsg rpc` subprocess** (JSON-RPC 2.0 over
stdin/stdout). That is the one and only seam between this project and `imsg`.

---

## Sibling Project: `imsg`

| Path | `~/src/imsg` |
|---|---|
| Repo | https://github.com/steipete/imsg |
| Binary | `~/src/imsg/bin/imsg` (after `make build`) |
| Role | Low-level iMessage access: read DB, send via AppleScript, expose RPC |
| Interface used here | `imsg rpc` — persistent JSON-RPC 2.0 server over stdin/stdout |

**Never modify `imsg` from within this project.** If a capability is missing in `imsg`, open a PR
upstream. The `imsg` binary path is configured in `config/imsg.json`.

### Building imsg

```bash
cd ~/src/imsg && make build   # produces ~/src/imsg/bin/imsg
```

### Key imsg RPC methods used here

```
chats.list          → { limit }              → { chats: [...] }
messages.history    → { chat_id, limit, ... } → { messages: [...] }
watch.subscribe     → { chat_id, since_rowid } → { subscription: N }  + notifications
watch.unsubscribe   → { subscription: N }
send                → { to|chat_id, text, file, service }
```

Full RPC protocol: `~/src/imsg/docs/rpc.md`

---

## Project Layout

```
imsg-agent/
├── CLAUDE.md               ← you are here
├── PLAN.md                 ← architecture, design decisions, ADRs
├── ROADMAP.md              ← phased todo lists and milestone tracking
│
├── config/
│   └── imsg.json           ← imsg binary path, RPC timeout, defaults
│
├── agent/                  ← core agent runtime (Python)
│   ├── __init__.py
│   ├── main.py             ← entrypoint: wake → poll → process → sleep
│   ├── rpc_client.py       ← thin async wrapper around `imsg rpc` stdin/stdout
│   ├── store.py            ← read/write ~/imsg-data/ directory store
│   ├── inbox.py            ← consume new messages, write inbox/ files
│   ├── drafter.py          ← propose responses (calls AI model)
│   ├── sender.py           ← read outbox/, call rpc send, archive to sent/
│   └── models.py           ← dataclasses: Message, Chat, Draft, OutboxItem
│
├── data/                   ← symlink or config pointer to ~/imsg-data/
│   └── (see Data Store section below)
│
├── scripts/
│   ├── setup.sh            ← install deps, check permissions, create ~/imsg-data/
│   └── run.sh              ← launch the agent loop
│
└── tests/
    ├── fixtures/           ← static JSON fixtures (no live DB)
    ├── test_rpc_client.py
    ├── test_store.py
    ├── test_inbox.py
    └── test_drafter.py
```

---

## Data Store — `~/imsg-data/`

The live data directory lives at `~/imsg-data/` (outside the repo). Never commit data files.

```
~/imsg-data/
├── state.json              ← { "cursor": 12345 }  last processed rowid
│
├── inbox/                  ← unprocessed inbound messages (one file per message)
│   └── {rowid}-{chatID}.md
│
├── chats/
│   └── {chatID}/
│       ├── context.md      ← chat name, participants, service, last-seen rowid
│       ├── history.md      ← rolling last-N messages (human-readable)
│       └── drafts/         ← agent-proposed responses pending approval
│           └── {uuid}.md
│
├── outbox/                 ← approved items ready to send
│   └── {uuid}.md
│
├── sent/                   ← archive of sent messages
│   └── {uuid}.md
│
└── errors/                 ← failed sends, parse failures
    └── {uuid}.md
```

### File Format (frontmatter + body)

Every markdown file uses YAML frontmatter:

```markdown
---
rowid: 12345
chat_id: 7
sender: "+14155551212"
date: "2026-04-04T10:30:00Z"
service: iMessage
has_attachments: false
thread: null            # guid of thread root, if reply
---
Hey, are we still on for Thursday?
```

Draft/outbox files add:

```markdown
---
uuid: "f47ac10b-58cc-..."
chat_id: 7
target_identifier: "iMessage;+;+14155551212"
created_at: "2026-04-04T10:31:00Z"
reasoning: "User asked about Thursday meeting — confirming."
approved: false         # set to true to move to outbox
---
Yes! See you at 2pm.
```

---

## Agent Lifecycle (one pass)

```
1. WAKE        Read state.json → get cursor (last rowid)
2. POLL        imsg rpc: watch.subscribe {since_rowid: cursor}
               OR imsg rpc: messages.history for each active chat
3. INGEST      For each new message:
               - write inbox/{rowid}-{chatID}.md
               - update chats/{chatID}/context.md
               - append to chats/{chatID}/history.md
4. DRAFT       For each inbox item not yet drafted:
               - read chats/{chatID}/context.md + history.md for context
               - call AI model with context + new message
               - write chats/{chatID}/drafts/{uuid}.md (approved: false)
5. APPROVE     (manual or policy-based)
               - human edits draft, sets approved: true
               - OR auto-approve policy moves it
               - approved drafts move to outbox/{uuid}.md
6. SEND        For each outbox/{uuid}.md:
               - imsg rpc: send {chat_id, text}
               - on success: move to sent/{uuid}.md, remove from outbox
               - on failure: move to errors/{uuid}.md
7. CHECKPOINT  Update state.json cursor to max seen rowid
```

---

## Key Design Decisions (see PLAN.md for full ADRs)

- **Language: Python** — natural for AI agent code, rich async ecosystem, easy to iterate
- **No direct DB access** — all reads/writes via `imsg rpc`; this project never touches `chat.db`
- **Markdown+frontmatter files** — human-readable, git-diffable, easy to inspect/edit manually
- **Outbox pattern** — responses are never sent without an explicit approved file in `outbox/`
- **Cursor-based polling** — `state.json` cursor prevents duplicate processing on restart
- **imsg rpc over CLI invocations** — one persistent subprocess, not per-message process forks
- **No framework** — plain Python async, minimal dependencies, easy to audit

---

## Configuration — `config/imsg.json`

```json
{
  "imsg_binary": "~/src/imsg/bin/imsg",
  "data_dir": "~/imsg-data",
  "rpc_timeout_seconds": 30,
  "watch_debounce_ms": 250,
  "history_limit": 50,
  "chat_context_messages": 20,
  "auto_approve": false,
  "default_service": "auto"
}
```

---

## macOS Permissions Required

These are required by `imsg`, not this project — but agent operators must have them:

| Permission | Why | Where |
|---|---|---|
| Full Disk Access | Read `~/Library/Messages/chat.db` | System Settings → Privacy → Full Disk Access |
| Automation → Messages | Send via AppleScript | System Settings → Privacy → Automation |

Run `scripts/setup.sh` to verify permissions before first use.

---

## What NOT To Do

- Do NOT read `~/Library/Messages/chat.db` directly
- Do NOT import or link against `IMsgCore` (the Swift library)
- Do NOT modify files in `~/src/imsg/`
- Do NOT commit anything from `~/imsg-data/` (add to .gitignore)
- Do NOT send a message without an `outbox/{uuid}.md` file as the source of truth
- Do NOT hardcode phone numbers, chat IDs, or personal data in source files
- Do NOT bypass the cursor — always read `state.json` on startup

---

## Future: Migration to DB + Queue

The markdown file store is intentionally structured to map 1:1 to a relational schema:

| Directory/File | Future equivalent |
|---|---|
| `state.json` | `agent_state` table |
| `inbox/{rowid}-{chatID}.md` | `messages` table + `inbox_queue` |
| `chats/{chatID}/context.md` | `chats` table |
| `chats/{chatID}/history.md` | `messages` table (indexed by chat) |
| `chats/{chatID}/drafts/` | `drafts` table |
| `outbox/` | `outbox_queue` (message queue topic) |
| `sent/` | `messages` table (status=sent) |

When migrating: keep the same agent lifecycle, replace `store.py` internals only.
