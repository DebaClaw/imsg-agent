# PLAN.md — imsg-agent Architecture & Design

Last updated: 2026-07-12

---

## Goal

Enable AI assistants (and human operators) to fully participate in a user's iMessage
communications: staying aware of conversations, understanding context and relationships,
drafting appropriate responses, and sending them — with the user always in control.

The system must be:
- **Safe by default** — no message is ever sent without an explicit approval step
- **Transparent** — every approval action is a file that can be read, edited, or deleted by hand
- **Recoverable** — restarts never lose messages; the cursor + inbox pattern is idempotent
- **Auditable** — the `sent/` archive is the complete record of what was sent and why
- **Queryable** — the no-AI archive is stored in SQLite for local search, attention ranking,
  and management surfaces
- **Evolvable** — the approval workflow can migrate to a DB+queue without changing the agent logic

---

## System Overview

```
┌─────────────────────────────────────────────────────────┐
│                    macOS Messages.app                    │
│                  ~/Library/Messages/chat.db              │
└────────────────────────┬────────────────────────────────┘
                         │ read-only SQLite
                         ▼
┌─────────────────────────────────────────────────────────┐
│                    imsg binary                           │
│          (`imsg` from PATH, or IMSG_BINARY)             │
│                                                         │
│  imsg rpc  ←── JSON-RPC 2.0 over stdin/stdout ──►       │
└────────────────────────┬────────────────────────────────┘
                         │ subprocess pipe
                         ▼
┌─────────────────────────────────────────────────────────┐
│                  imsg-agent services                     │
│              (this project, Python)                     │
│                                                         │
│  archiver.py     →  archive_store.py (SQLite archive)   │
│  archive_agent.py → drafter.py → store.py (drafts)      │
│  sender.py       →  imsg rpc send                       │
│  mcp_server.py   →  archive_store.py (read-only tools)  │
└────────────────────────┬────────────────────────────────┘
                         │ reads/writes markdown files + SQLite archive
                         ▼
┌─────────────────────────────────────────────────────────┐
│                   ~/imsg-data/                          │
│   state.json  inbox/  chats/  outbox/  sent/  errors/  │
│   imessage.sqlite                                      │
└─────────────────────────────────────────────────────────┘
                         ▲
                         │ human edits (approve drafts, etc.)
                    Operator / AI agent
```

---

## Architecture Decision Records (ADRs)

### ADR-001: Language — Python

**Decision:** Python, not Swift.

**Rationale:**
- `imsg`/`IMsgCore` are Swift because they must use macOS-specific APIs (SQLite WAL watching,
  AppleScript, ScriptingBridge). This project has no such requirement.
- Python is the dominant language for AI agent tooling (LangChain, Claude SDK, OpenAI SDK, etc.)
- Async/await in Python 3.11+ is mature and fits the event-driven nature of message watching.
- Easier to iterate drafting logic and prompts without a compile step.
- `python3` ships with macOS; minimal setup for a new operator.

**Rejected alternatives:**
- Swift: adds compile step, less AI tooling ecosystem, no benefit since we don't call macOS APIs
- Node.js: viable but fewer AI SDK options and async patterns are noisier
- Shell scripts: not appropriate for stateful orchestration

---

### ADR-002: Interface — `imsg rpc` subprocess only

**Decision:** All iMessage access goes through a single long-running `imsg rpc` subprocess.

**Rationale:**
- One process, one connection, no per-message overhead.
- `imsg rpc` is a stable, versioned protocol boundary. If `imsg` changes internals, the JSON-RPC
  interface absorbs the change.
- Keeps this project dependency-free from Swift/macOS build tooling.
- A subprocess pipe is easy to mock/fake in tests.

**Rejected alternatives:**
- Import `IMsgCore` as Swift Package: requires Swift toolchain, tight coupling, breaks on upstream changes
- Call `imsg history/watch` as separate processes per query: high overhead, harder to manage cursors
- Direct SQLite access to `chat.db`: violates the layering, duplicates logic already in `imsg`

---

### ADR-003: Approval Store — Markdown files with YAML frontmatter

**Decision:** Use `~/imsg-data/` as a directory-based store with `.md` files for the human
approval workflow: inbox, chat context, rolling history, drafts, outbox, sent archives,
errors, nudges, and digests.

**Rationale:**
- Human-readable and human-editable without any tooling.
- `approved: true` in a draft file is something a human can set in any text editor.
- Easy to inspect the full state of the system with `ls` and `cat`.
- Git-diffable if the operator wants to version their data.
- Maps cleanly to a relational schema for future DB migration (see PLAN § Migration).
- Keeps the safety-critical approval path legible even while the archive/query layer uses SQLite.

**Rejected alternatives:**
- SQLite for approval state: useful for scale, but worse for early human review/edit/approval.
  The project now uses SQLite where it is strongest: the append-heavy local archive and query
  surface.
- JSON files: less readable for message bodies; no separation of metadata from content
- Plain text: no structured metadata without parsing

**Known limitations:**
- Not suitable for high-volume chats (thousands of messages/day). File per message becomes unwieldy.
- No atomic transactions: a crash mid-write can leave partial files (mitigated by write-then-rename).
- No broad query capability over approval files. Conversation search and attention ranking use the
  SQLite archive instead.

These are acceptable for the approval workflow. A future DB+queue can replace this layer when
concurrent workers or higher volume require it.

---

### ADR-003B: Archive Store — SQLite

**Decision:** Maintain `~/imsg-data/imessage.sqlite` as a no-GenAI archive of chats, messages,
attachment metadata, reactions, contact snapshots, and contact matches.

**Rationale:**
- Historical message search and "what needs attention?" queries need indexed reads.
- Backfill and monitoring are append/update-heavy and idempotent by natural iMessage keys:
  chat id and message rowid.
- Archive operations should not depend on drafting, approval, or any model API.
- SQLite is the right local-first store for a single-machine operational archive.
- TUI, local web, saved views, and AI action layers can all read from the same queryable source.

**Boundaries:**
- The archive never reads `~/Library/Messages/chat.db` directly; it only stores data returned by
  `imsg rpc`.
- The archive is not the source of truth for approved sends. `outbox/`, `sent/`, and `errors/`
  remain the approval/send audit path.
- SQLite stores attachment metadata and local archive paths, not file blobs.

---

### ADR-004: Outbox Pattern — No Direct Sends

**Decision:** The agent never calls `imsg rpc: send` directly from the drafter. All sends go
through the outbox file pattern: draft → approve → outbox → send → archive.

**Rationale:**
- Safety. An AI drafter making mistakes is expected; those mistakes staying in `drafts/` until
  reviewed is the feature, not a limitation.
- Decouples drafting from sending: the approval step can be manual, a simple policy rule,
  or a second AI review pass — the sender doesn't care which.
- The `outbox/` directory is the audit-ready queue of "things we have committed to send."
- Makes it trivial to cancel a pending send: delete the `outbox/{uuid}.md` file.

**Approved flows:**
1. Manual: human edits draft, sets `approved: true`, agent's next pass sends it
2. Policy auto-approve: a rule (e.g., "auto-approve replies to family chats") sets approved flag
3. Second-pass AI review: a separate review agent approves or rejects drafts

---

### ADR-005: Cursor-Based Polling

**Decision:** Use a single `state.json` cursor (last seen rowid) for all message ingestion.

**Rationale:**
- `imsg` messages are append-only with monotonically increasing rowids in SQLite.
- A single cursor is the simplest possible resumable state.
- On crash/restart, the agent re-reads from the last checkpointed rowid — no messages lost,
  possible brief duplicates (handled by checking if `inbox/{rowid}-*.md` already exists).
- The cursor is only written to disk after the batch is fully ingested, not per-message.

**Cursor advance rules:**
1. Read cursor from `state.json` on startup (default: 0 = "start from current")
2. After each batch: advance cursor to max rowid in batch
3. Write cursor to `state.json` only after all inbox files for the batch are written
4. If a write fails mid-batch: the cursor is not advanced; the batch is retried on next wake

---

### ADR-006: Chat Context Window

**Decision:** Each chat maintains a `context.md` (metadata) and `history.md` (rolling last-N
messages). The AI drafter receives both as context.

**Rationale:**
- iMessage context is critical for good responses. Without history, responses are incoherent.
- `context.md` carries stable facts: who is in the chat, what service, what relationship.
- `history.md` is a rolling window (configurable, default 20 messages) — not the full archive.
- The full archive is reconstructable from `inbox/` files and `messages.history` RPC calls.

**What goes in `context.md`:**
- Chat name, identifier, service (iMessage vs SMS)
- Participants and their handles
- Relationship notes (manually editable by operator)
- Last active timestamp
- Agent behavior notes (e.g., "do not auto-draft", "respond in Spanish")

---

### ADR-007: No Framework

**Decision:** Plain Python async with no agent framework (no LangChain, no CrewAI, etc.).

**Rationale:**
- The agent lifecycle is simple and well-defined: poll → ingest → draft → approve → send.
  A framework adds abstraction over a problem that doesn't need it yet.
- Frameworks add transitive dependencies that change frequently and introduce security surface.
- The OpenAI Responses API (or any LLM API) is called through `drafter.py` — one HTTP call per draft.
- If orchestration complexity grows (multi-agent, tool use, etc.), this is the point to evaluate
  frameworks — not before.

**Dependencies (target minimal set):**
- `openai` — OpenAI API SDK for drafting
- `pyyaml` — frontmatter parsing
- `aiofiles` — async file I/O
- `python-dotenv` — config from `.env`

---

### ADR-008: Conversation Workbench — Channel-Aware, Approval-Safe

**Decision:** Operator views such as Recent and Attention select a conversation and open one
shared workbench. A summary row identifies the channel, direction, latest communication, and
reply-artifact state; the workbench joins that summary to the isolated conversation context,
transcript, and approval actions.

The current adapter is iMessage/SMS through `imsg rpc`. Future email, Slack, news/info, and other
digital inputs should map into the same operator concepts without pretending every channel can be
sent through the iMessage sender.

**Operator use cases:**

| Intent | Workbench behavior | Safety boundary |
|---|---|---|
| View | Open the latest item and transcript from Recent or Attention | Read only the selected conversation |
| Draft | Request an AI proposal when none exists, including an explicit follow-up after an outbound message | Use only that conversation's context and history; leave the result unapproved |
| Edit | Change proposed text and save it for later review | Saving never approves or sends |
| Approve | Commit reviewed text to delivery | Queue the existing Markdown outbox artifact; never call a provider directly from the UI |
| Deny | Record that no response should be sent | Preserve the rejected draft and no-reply rationale |
| Archive | Remove a stale proposal from the active queue | Preserve a non-destructive archive artifact |
| Prevent | Pause AI drafting for a conversation | Set `do_not_draft: true` and disable auto-approval |
| Block | Mark an unwanted sender as ignored/spam | Preserve the local decision and prevent engagement; provider-level blocking is a future adapter capability |
| Configure | Edit identity, relationship, tone, professional status, model, recipients, and approval policy | Professional and unknown-professional conversations always require manual approval |
| Send/manage | Observe queued, sent, and failed states | Provider delivery remains downstream of outbox and must archive before send |

**Extension boundary:** A future communication adapter should provide normalized conversation and
item reads plus explicit capabilities such as `can_reply`, `can_block`, or `can_archive`. Provider
actions belong behind their adapter; conversation context, AI assistance, policy evaluation, and
approval artifacts remain channel-independent. Unsupported actions must be visibly unavailable,
not silently simulated.

---

## Module Responsibilities

### `rpc_client.py`
- Manages the `imsg rpc` subprocess lifetime
- Sends JSON-RPC requests, receives responses and notifications
- Handles reconnection if the subprocess dies
- Provides typed async methods: `list_chats()`, `get_history()`, `subscribe()`, `send()`
- **No business logic** — pure protocol adapter

### `store.py`
- All reads and writes to `~/imsg-data/`
- Parses and serializes markdown+frontmatter files
- Manages atomic writes (write to `.tmp`, rename)
- Reads/writes `state.json` cursor
- **No network or subprocess calls**

### `inbox.py`
- Consumes raw `Message` objects from `rpc_client`
- Calls `store.write_inbox()` and `store.update_chat_context()`
- Deduplicates (skip if `inbox/{rowid}-*.md` already exists)
- **Owns the ingest half of the lifecycle**

### `drafter.py`
- Reads unprocessed inbox files in the legacy runtime, or archived `Message` objects in the
  archive-backed worker
- Builds context from the target chat's `context.md` plus either `history.md` or SQLite history
- Calls the OpenAI Responses API with a system prompt + context + new message
- Writes `chats/{chatID}/drafts/{uuid}.md` with `approved: false`
- **The only module that calls an external AI API**

### `sender.py`
- Scans `outbox/` for `.md` files
- Reads target and text from each file
- Calls `rpc_client.send()`
- On success: moves file to `sent/`
- On failure: moves file to `errors/`, logs reason
- **Owns the send half of the lifecycle**

### `main.py`
- Initializes all modules
- Runs the legacy file-ingest agent loop: wake → poll → ingest → draft → send → checkpoint → sleep
- Handles OS signals (SIGTERM: finish current batch, checkpoint, exit cleanly)
- Configures logging

### `archive_agent.py`
- Runs the preferred archive-backed AI worker
- Reads new inbound messages from `archive_store.py` using a separate `agent_draft_cursor`
- Uses SQLite history plus only the target chat's `context.md` to propose drafts
- Does not subscribe to `imsg rpc`; it depends on `imsg-archive monitor` for ingestion
- Optionally executes already-approved outbox items through `sender.py`

### `archive_store.py`
- Maintains `~/imsg-data/imessage.sqlite`
- Stores chats, messages, attachment metadata, reactions, and archive cursor
- Uses idempotent upserts keyed by chat id and message rowid
- **No GenAI and no direct Messages database reads**

### `archiver.py` / `archive_main.py`
- Backfills all chats returned by `imsg rpc` with attachment metadata enabled
- Monitors new messages with attachment metadata enabled
- Writes only to the SQLite archive
- Does not import the drafter or call any model API

### `mcp_server.py`
- Exposes read-only MCP tools over `archive_store.py`
- Provides archive stats, recent chats, attention, needs-reply, message search, chat history,
  unresolved contact matches, and attachment issues
- Does not send messages, approve drafts, mutate the archive, or call any model API

---

## Data Flow Diagram

```
imsg rpc
  │
  │ JSON notifications (new messages)
  ▼
archiver.py
  │ idempotent upserts
  ▼
archive_store.py ─────► imessage.sqlite
  │                       search / recent / attention / MCP
  │ new inbound rows
  ▼
archive_agent.py
  │ read target context.md + SQLite chat history
  │ call OpenAI Responses API
  │
  ▼
store.py ──────► chats/{chatID}/drafts/{uuid}.md  (approved: false)
                                │
                         [human/policy approval]
                                │
                                ▼
                         outbox/{uuid}.md  (approved: true)
                                │
                         sender.py
                                │
                         imsg rpc: send
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
              sent/{uuid}.md         errors/{uuid}.md
```

---

## Security Model

### Threat: Agent sends messages without authorization
**Mitigation:** Outbox pattern (ADR-004). The only way a message gets sent is if a file exists
in `outbox/`. Files only reach `outbox/` via explicit approval. The `sender.py` module does not
decide *what* to send — it only executes what's already been approved.

### Threat: Agent exfiltrates files via send attachment
**Mitigation:** `sender.py` restricts attachment paths to `~/imsg-data/outbox/attachments/` only.
Files outside this directory are rejected with a log entry to `errors/`.

### Threat: Prompt injection via received message content
**Mitigation:** Received message text is passed to the model as user-turn content, clearly
separated from the system prompt. The system prompt instructs the model that message content is
untrusted user input. Draft output is reviewed before send. (Phase 2: add explicit injection
resistance patterns.)

### Threat: Runaway drafting costs (LLM API spend)
**Mitigation:** `config/imsg.json` has `max_drafts_per_hour` and `max_daily_api_spend` (Phase 2).
Phase 1: operator monitors manually.

### Threat: Stale cursor causes re-processing old messages
**Mitigation:** Inbox deduplication checks for existing `inbox/{rowid}-*.md` before writing.
Even if cursor is wrong, the same message won't generate a second inbox file or draft.

---

## Testing Strategy

### Unit tests (no live data)
- `tests/fixtures/` — static JSON payloads from `imsg rpc` (real shape, fake content)
- `test_rpc_client.py` — mock subprocess, test request/response parsing
- `test_store.py` — temp directory, test all read/write/parse operations
- `test_inbox.py` — mock rpc_client and store, test dedup and context update logic
- `test_drafter.py` — mock drafting client, test context assembly and draft file format

### Integration tests (requires imsg binary + Full Disk Access)
- `tests/integration/` — marked with `@pytest.mark.integration`
- Only run in CI with explicit `IMSG_INTEGRATION=1` env var
- Use a dedicated test chat (manually created) to avoid touching real conversations

---

## Storage Migration Path

### Current: Markdown approval store + SQLite archive
- `store.py` reads/writes human-editable approval workflow files under `~/imsg-data/`.
- `archive_store.py` maintains `~/imsg-data/imessage.sqlite` for historical messages,
  attachments, reactions, contacts, search, and deterministic attention ranking.
- `archiver.py` backfills and monitors the archive through `imsg rpc`.
- `archive_agent.py` consumes the archive through its own `agent_draft_cursor` and writes
  reviewable drafts.
- Markdown remains the source of truth for draft approval, outbox, sent archives, and errors.
- SQLite is the source of truth for archive/search/visibility surfaces.

### Near-term: Shared query layer
- Keep building reusable read-only query methods on top of `archive_store.py`.
- CLI, TUI, local web, saved views, and AI action commands should use the same archive queries.
- AI summaries and draft suggestions may read archive data, but must preserve per-chat isolation
  and write reviewable artifacts rather than mutating messages invisibly.

### Later: Full approval DB + Queue
- Replace `store.py` with a DB-backed implementation only when volume/concurrency requires it.
- Replace `outbox/` with a proper message queue (SQLite queue, Redis, PostgreSQL, or similar).
- The agent lifecycle (`main.py`) and modules above `store.py` should stay mostly unchanged.
- Migration script: ingest existing approval `.md` files into the DB-backed approval store.

The key design principle: **the archive and approval workflow are related but separate.**
SQLite is already correct for historical conversation visibility; Markdown remains correct for
human-controlled approval until there is a concrete need for a queue-backed approval store.
