# ROADMAP.md — imsg-agent

Phased implementation plan. Each phase is independently useful and shippable.

Status key: ⬜ not started · 🔄 in progress · ✅ done · 🚫 blocked

---

## Phase 1 — Foundation: RPC Client + Data Store

**Goal:** A working agent loop that can receive messages, write them to disk, and surface them
for human inspection. No AI drafting yet. Proves the pipe works end-to-end.

**Exit criteria:**
- `python agent/main.py` starts, connects to `imsg rpc`, and populates `~/imsg-data/inbox/`
- New messages appear as `.md` files within ~1 second of arriving in Messages.app
- Restarting the agent does not duplicate existing inbox files
- `chats/{chatID}/context.md` and `history.md` are kept up to date
- All modules have unit tests passing with no live data

### Tasks

#### Setup & Config
- ⬜ Create `pyproject.toml` with dependencies (anthropic, pyyaml, aiofiles, python-dotenv)
- ⬜ Create `config/imsg.json` with default configuration
- ⬜ Create `.env.example` for API keys
- ⬜ Create `.gitignore` (exclude `~/imsg-data/`, `.env`, `__pycache__`, etc.)
- ⬜ Write `scripts/setup.sh` — check permissions, create `~/imsg-data/` tree, verify imsg binary

#### Core Modules
- ⬜ `agent/models.py` — dataclasses: `Message`, `Chat`, `ChatInfo`, `Draft`, `OutboxItem`
- ⬜ `agent/rpc_client.py` — subprocess manager, JSON-RPC send/receive, async iterator for notifications
- ⬜ `agent/store.py` — all `~/imsg-data/` I/O: read/write inbox, context, history, state.json, outbox, sent, errors
- ⬜ `agent/inbox.py` — consume messages from rpc_client, write to store, dedup by rowid
- ⬜ `agent/main.py` — event loop: init → subscribe → ingest loop → checkpoint → signal handling

#### Tests
- ⬜ `tests/fixtures/` — sample `chats.list` and `watch.subscribe` notification payloads
- ⬜ `tests/test_rpc_client.py` — mock subprocess I/O, test request/response lifecycle
- ⬜ `tests/test_store.py` — temp dir, test all read/write/parse/atomic-write operations
- ⬜ `tests/test_inbox.py` — test dedup, context update, history rolling window

#### Validation
- ⬜ Manual end-to-end test: send message to self, verify inbox file created
- ⬜ Restart agent, verify no duplicate inbox file
- ⬜ Verify `state.json` cursor advances correctly

---

## Phase 2 — Drafting: AI Response Proposals

**Goal:** For each inbox message, the agent reads chat context and proposes a response using
Claude. Drafts are written to `chats/{chatID}/drafts/` and held for approval.

**Exit criteria:**
- Within 5 seconds of a new inbox file, a draft file appears in `chats/{chatID}/drafts/`
- Draft contains sensible proposed response given the chat history
- Setting `approved: true` in a draft causes it to move to `outbox/` on next agent pass
- Sending from outbox works; file moves to `sent/`

### Tasks

#### Drafting
- ⬜ `agent/drafter.py` — build context from history.md + context.md, call Claude API, write draft
- ⬜ System prompt v1 — base prompt for iMessage response drafting
- ⬜ Per-chat prompt overrides — read from `chats/{chatID}/context.md` field `agent_notes`
- ⬜ Draft filename convention: `{timestamp}-{rowid}.md` for natural sort order
- ⬜ `tests/test_drafter.py` — mock API, test context assembly, test draft format

#### Approval & Send
- ⬜ `agent/sender.py` — scan outbox, call rpc_client.send(), archive to sent/ or errors/
- ⬜ Approval watcher: scan drafts/ for `approved: true`, move to outbox/
- ⬜ Attachment path allowlist enforcement in sender.py
- ⬜ `tests/test_sender.py` — mock rpc_client, test success/failure/archive paths

#### Safety & Config
- ⬜ `auto_approve: false` default enforced — drafts never auto-move without explicit config
- ⬜ Per-chat `do_not_draft: true` flag in context.md — skip drafting for that chat
- ⬜ Max inbox age filter — don't draft responses to messages older than N hours

#### Validation
- ⬜ Manual end-to-end: receive message → draft appears → set approved → message sent → archived
- ⬜ Verify rejected draft (deleted from drafts/) does not end up in outbox

---

## Phase 3 — Intelligence: Context, Relationships & Policies

**Goal:** The agent understands *who* people are, maintains richer context over time, and applies
configurable policies (auto-approve for certain chats, different tones per contact, etc.)

**Exit criteria:**
- `context.md` includes operator-editable relationship notes that influence draft tone
- Auto-approve policy works for configured chats
- Agent correctly identifies and handles group chats vs 1:1
- Agent can surface "you haven't replied to X in N days" summaries

### Tasks

#### Relationship Context
- ⬜ `chats/{chatID}/context.md` schema v2 — add `relationship`, `tone`, `agent_notes`, `do_not_draft`
- ⬜ Drafter reads and uses relationship context in system prompt
- ⬜ `scripts/import_contacts.py` — seed context.md files from existing chat history

#### Policies
- ⬜ `config/policies.json` — per-chat-id or per-participant rules
- ⬜ Auto-approve policy engine in `sender.py`
- ⬜ Rate limiting: max N sends per chat per hour
- ⬜ Quiet hours: do not send between configurable hours

#### Summaries & Proactive Nudges
- ⬜ `agent/summarizer.py` — daily summary of conversations, unanswered messages
- ⬜ `agent/nudger.py` — detect "no reply in N days", write nudge to a special `nudges/` dir
- ⬜ Weekly digest output to `~/imsg-data/digests/{date}.md`

#### Group Chat Handling
- ⬜ Detect group chats (`;+;` in identifier) and adjust drafting strategy
- ⬜ Default to `do_not_draft: true` for group chats until operator opts in

---

## Phase 4 — Storage: SQLite Index

**Goal:** Add a queryable index over the markdown data so the agent can answer questions like
"what did I last say to person X" or "find all unanswered messages."

**Exit criteria:**
- `store_index.py` maintains a SQLite DB in `~/imsg-data/index.db`
- Index is rebuilt from markdown files in <5 seconds for 10k messages
- All queries in drafter/summarizer use the index, not filesystem traversal
- Markdown files remain the source of truth; index is a projection

### Tasks

- ⬜ `agent/store_index.py` — schema, build, and query API
- ⬜ `scripts/rebuild_index.py` — full rebuild from markdown files
- ⬜ Incremental index update on each store write
- ⬜ Migration: existing `~/imsg-data/` → index.db
- ⬜ Query methods: `unanswered_messages()`, `messages_by_participant()`, `chats_by_last_active()`

---

## Phase 5 — Production: Queue, DB, Multi-agent

**Goal:** Replace the markdown store with a proper DB + message queue for high-volume use,
concurrent agents, and reliability guarantees.

**Exit criteria:**
- `store.py` replaced by `store_db.py` backed by PostgreSQL (or SQLite WAL for single-machine)
- `outbox/` replaced by a proper queue (Redis Streams or pg queue)
- Multiple agent workers can run concurrently without conflicts
- Historical data migrated from markdown files

### Tasks

- ⬜ Design DB schema (messages, chats, drafts, outbox, sent, errors, state tables)
- ⬜ `agent/store_db.py` — same interface as `store.py`, DB-backed
- ⬜ Queue integration for outbox
- ⬜ Migration script: `~/imsg-data/` markdown → DB
- ⬜ Concurrency: advisory locks or queue ownership for multi-worker safety
- ⬜ Observability: structured logging, metrics (messages/hour, drafts/hour, send latency)

---

## Ongoing / Cross-cutting

- ⬜ CI: GitHub Actions running `pytest` on every push (no live DB, fixtures only)
- ⬜ Linting: `ruff` + `mypy --strict`
- ⬜ `scripts/health_check.sh` — verify imsg binary, permissions, data dir, rpc connectivity
- ⬜ Changelog maintenance
- ⬜ Prompt versioning: track system prompt versions alongside draft files so old drafts are
     reproducible (`prompt_version` field in draft frontmatter)
