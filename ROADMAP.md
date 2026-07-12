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
- ✅ Create `pyproject.toml` with dependencies (openai, pyyaml, aiofiles, python-dotenv)
- ✅ Create `config/imsg.json` with default configuration
- ✅ Create `.env.example` for API keys
- ✅ Create `.gitignore` (exclude `~/imsg-data/`, `.env`, `__pycache__`, etc.)
- ✅ Write `scripts/setup.sh` — check permissions, create `~/imsg-data/` tree, verify imsg binary

#### Core Modules
- ✅ `agent/models.py` — dataclasses: `Message`, `Chat`, `Draft`, `OutboxItem`
- ✅ `agent/rpc_client.py` — subprocess manager, JSON-RPC send/receive, async iterator for notifications
- ✅ `agent/store.py` — all `~/imsg-data/` I/O: read/write inbox, context, history, state.json, outbox, sent, errors
- ✅ `agent/inbox.py` — consume messages from rpc_client, write to store, dedup by rowid
- ✅ `agent/main.py` — event loop: init → subscribe → ingest loop → checkpoint → signal handling

#### Tests
- ✅ `tests/fixtures/` — sample `chats.list` and `watch.subscribe` notification payloads
- ✅ `tests/test_rpc_client.py` — mock subprocess I/O, test request/response lifecycle
- ✅ `tests/test_store.py` — temp dir, test all read/write/parse/atomic-write operations
- ✅ `tests/test_inbox.py` — test dedup, context update, history rolling window

#### Validation
- ⬜ Manual end-to-end test: send message to self, verify inbox file created
- ⬜ Restart agent, verify no duplicate inbox file
- ⬜ Verify `state.json` cursor advances correctly

---

## Phase 2 — Drafting: AI Response Proposals

**Goal:** For each inbox message, the agent reads chat context and proposes a response using
the OpenAI Responses API. Drafts are written to `chats/{chatID}/drafts/` and held for approval.

**Exit criteria:**
- Within 5 seconds of a new inbox file, a draft file appears in `chats/{chatID}/drafts/`
- Draft contains sensible proposed response given the chat history
- Setting `approved: true` in a draft causes it to move to `outbox/` on next agent pass
- Sending from outbox works; file moves to `sent/`

### Tasks

#### Drafting
- ✅ `agent/drafter.py` — build context from history.md + context.md, call OpenAI Responses API, write draft
- ✅ System prompt v1 — base prompt for iMessage response drafting
- ✅ Per-chat prompt context — read relationship, tone, and `agent_notes` from `context.md`
- ✅ Draft filename convention: `{timestamp}-{rowid}.md` for natural sort order
- ✅ `tests/test_drafter.py` — mock API, test context assembly, test draft format

#### Approval & Send
- ✅ `agent/sender.py` — scan outbox, call rpc_client.send(), archive to sent/ or errors/
- ✅ Approval watcher: scan drafts/ for `approved: true`, move to outbox/
- ✅ Attachment path allowlist enforcement in sender.py
- ✅ `tests/test_sender.py` — mock rpc_client, test success/failure/archive paths

#### Safety & Config
- ✅ `auto_approve: false` default enforced — drafts never auto-move without explicit config
- ✅ Per-chat `do_not_draft: true` flag in context.md — skip drafting for that chat
- ✅ Max inbox age filter — don't draft responses to messages older than N hours

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
- ✅ `chats/{chatID}/context.md` schema v2 — add `relationship`, `tone`, `agent_notes`, `do_not_draft`
- ✅ Drafter reads and uses relationship context in system prompt
- ⬜ `scripts/import_contacts.py` — seed context.md files from existing chat history

#### Policies
- ⬜ `config/policies.json` — per-chat-id or per-participant rules
- ✅ Auto-approve policy engine for opted-in non-professional 1:1 chats
- ⬜ Rate limiting: max N sends per chat per hour
- ⬜ Quiet hours: do not send between configurable hours

#### Summaries & Proactive Nudges
- ✅ `agent/summarizer.py` — daily summary of conversations
- ✅ `agent/nudger.py` — detect "no reply in N days", write nudge to a special `nudges/` dir
- ⬜ Weekly digest scheduling/output policy

#### Group Chat Handling
- ✅ Detect group chats (`;+;` in identifier or multiple participants) and adjust drafting strategy
- ✅ Default to `do_not_draft: true` for group chats until operator opts in

---

## Phase 4 — Storage: SQLite Archive and Query Layer

**Goal:** Maintain a queryable SQLite archive so the operator and agent can answer questions
like "what did I last say to person X" or "find conversations that need a reply."

Markdown remains the approval/drafting artifact store. SQLite is the archive/search/visibility
source and the preferred input for the archive-backed AI worker.

**Exit criteria:**
- `archive_store.py` maintains `~/imsg-data/imessage.sqlite`
- Archive monitor updates SQLite incrementally from `imsg rpc`
- Query methods support recent chats, needs-reply, attention, search, chat history,
  contacts, and attachment issue views
- Archive-backed AI worker reads SQLite instead of subscribing to `imsg rpc`
- Markdown files remain the source of truth for drafts, approvals, sent archives, and errors

### Tasks

- ✅ `agent/archive_store.py` — schema for chats, messages, attachments, reactions, cursor
- ✅ Query API for recent chats, needs-reply, attention, search, and chat history
- ✅ Incremental archive update from `imsg-archive monitor`
- ✅ Backfill from `imsg rpc messages.history`
- ✅ `imsg-mcp` read-only MCP surface over archive queries
- ✅ Archive-backed draft worker consumes SQLite with an independent cursor
- ⬜ Query methods: `messages_by_participant()`, `chats_by_last_active()`
- ⬜ Rebuild/repair tooling for archive projections if schema/query projections change

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

## Phase 6 — Operations: Visibility, Management, and Interfaces

**Goal:** Make the no-AI archive operationally useful on its own, then layer management
interfaces and AI workflows on top without making the archive depend on AI.

**Exit criteria:**
- The archive monitor can run persistently under `launchd`
- The operator can answer "what needs attention?" from CLI commands alone
- TUI and web plans preserve the same SQLite/archive source of truth
- AI actions remain explicit higher-level commands that read from the archive and write
  reviewable artifacts instead of mutating messages invisibly

### Tasks

#### No-AI Operations
- ✅ `scripts/install_launchd.sh` — install a user LaunchAgent for `imsg-archive monitor`
- ✅ `imsg-archive stats` — archive totals for chats, messages, contacts, attachments
- ✅ `imsg-archive recent` — recently active chats with last message preview
- ✅ `imsg-archive search messages` — FTS-backed archived message search
- ✅ `imsg-archive attention` — deterministic no-AI ranking for reply awareness
- ✅ `imsg-archive needs-reply` — chats where the latest archived message is inbound
- ✅ `imsg-archive pending` — latest-inbound messages with matching proposed replies
- ✅ `imsg-archive unresolved` — contact match gaps for review
- ✅ `imsg-archive attachment-issues` — attachments that were not copied locally
- ✅ `imsg-mcp` — read-only MCP tools over the SQLite archive
- ⬜ Incremental Contacts enrichment during monitor or scheduled maintenance
- ⬜ Resumable attachment repair state for large attachment recovery jobs

#### Management Interfaces
- ✅ `imsg-agentctl` — installed operator CLI for status, reports, services, logs, and queues
- ⬜ TUI dashboard for communications awareness and triage
- 🔄 Local web interface for archive browsing and communication management
- ✅ Shared read-only service/query layer for CLI, MCP, TUI, and web
- ⬜ Saved views: unanswered, recently active, quiet relationships, attachment issues

#### Relationship Observatory: Identity, Contacts, and Triage
- ✅ **Operator identity and preferences**
  - ✅ Local operator profile with name, vCard/contact reference, aliases, and avatar fallback
  - ✅ Global pending-window, relationship-type filters, grouping, and older-history controls
- 🔄 **Contact review and enrichment**
  - ⬜ Surface matched contact context and synced photo/avatar data in the operator UI
  - ✅ Add a local review flow for unresolved contacts: keep-local, ignore/spam, or prepare a vCard candidate
  - ⬜ Link an unresolved conversation to an existing synced contact
  - ✅ Keep Contacts creation explicit; never promote unknown/spam conversation data automatically
- ✅ **Orbit conversation mode**
  - ✅ Switch from the default Orbit plus draft queue to a selected-conversation workbench
  - ✅ Restore the default operator-and-queue mode only when selection is released
  - ✅ Keep the Orbit visible while the conversation replaces only the draft-queue column
  - ✅ Load a smaller initial transcript and avoid repeated per-message contact lookups
- ✅ **Non-destructive draft lifecycle**
  - ✅ Archive drafts with an operator reason instead of deleting Markdown artifacts
  - ✅ Hide archived drafts by default while preserving an explicit archive view

#### Full Contact and Conversation Management
- 🔄 **contacts-mcp integration**
  - ✅ Configure and run an explicit contacts-mcp sync from the web UI
  - ✅ Browse synced contacts, identifiers, notes, and categories
  - ✅ Link or unlink a conversation from a synced contact without waiting for heuristic enrichment
  - ✅ Preserve local review decisions and explicit vCard contact candidates for unknown/spam chats
  - ✅ Create, update, and archive Contacts through the installed contacts-mcp connector
  - ✅ Surface synced contact photos/avatars when the connector exports them
- ⬜ **Conversation workspace**
  - ✅ Edit complete per-chat context: identity, relationship, tone, policy, notes, and drafting model
  - ✅ Browse recent transcript with pagination and load older messages on demand
  - ✅ Present contact identity and conversation policy together without crossing chat context
  - ✅ Add a dedicated contacts view and complete all actions with local API/test coverage

#### Orbit Performance and Live Data
- ✅ **Fast initial Orbit**
  - ✅ Collapse overview reads into one archive session and eliminate repeated queue/context scans
  - ✅ Read the initial Orbit directly from the local archive without requiring a manual refresh
  - ✅ Sort Orbit by Favorites and relationship relevance, then most-recent activity; keep attention score as a secondary signal
- ✅ **Live local updates**
  - ✅ Detect SQLite/data-artifact changes and refresh the active view without user action
  - ✅ Keep Refresh as an explicit reconciliation action rather than initial-load plumbing

#### Selected Orbit Workspace
- ✅ **Context and conversation pane**
  - ✅ Return an empty-space Orbit click to the default operator-and-queue view
  - ✅ Put relationship context and the transcript in one switchable right-hand pane
  - ✅ Move to messages after saving context and retain iMessage-style inbound/outbound alignment
- ✅ **Scrollable selected pane**
  - ✅ Constrain the desktop workbench and scroll context or messages independently
- ✅ **Primary contact card**
  - ✅ Choose a synced contact card as the configurable Orbit center
  - ✅ Use that card's name and photo in Orbit, with the existing vCard fallback
- ✅ **Operator identity and recipients**
  - ✅ Make the operator name and linked contact a first-class local identity configuration
  - ✅ Exclude the configured operator from Context recipient lists and saved participants

#### Contact Safety Controls
- ✅ **Ignore and spam policy**
  - ✅ Turn an Ignore / spam review into a visible, non-engagement context policy
  - ✅ Preserve existing operator context while filling blank policy guidance

#### Settings and Large Lists
- ✅ **Global settings workspace**
  - ✅ Make the persistent Operator control open a full Settings workspace from every view
  - ✅ Consolidate operator identity and review-queue configuration there
- ✅ **Contact selection at scale**
  - ✅ Add substring filtering and pagination to contact lists and identity-card selection
- ✅ **Identity card picker ergonomics**
  - ✅ Put a clearly labeled live filter directly above the paginated identity-card list

#### Orbit Relationship Ranking and Exploration
- ✅ **Relationship-aware Orbit ranking**
  - ✅ Add a local per-contact importance override
  - ✅ Score matched contacts with recent two-way discussion activity alongside inbound recency
  - ✅ Expose ranking factors on Orbit items for review
- ✅ **Orbit navigation**
  - ✅ Add incoming/outgoing/either direction and time-window controls
  - ✅ Page through all matching threads without overcrowding the attention field

#### macOS Administration
- ✅ **Service admin scripts**
  - ✅ Add consistent start, stop, restart, status, and log controls for monitor, worker, and web
  - ✅ Document the macOS launchctl restart path and script entrypoints
- ✅ **LaunchAgent restart reliability**
  - ✅ Restart loaded services with kickstart and allow enough time for macOS teardown

#### AI Action Layer
- ✅ `imsg-agent-worker` — archive-backed AI worker with independent draft cursor
- ✅ `scripts/install_agent_worker_launchd.sh` — install worker as separate LaunchAgent
- ⬜ AI summaries from archive data with per-chat isolation controls
- ✅ AI draft generation from archived context with manual approval by default
- ⬜ Explicit AI triage command for ambiguous/unresolved conversations
- ⬜ "Do the needful" workflow that produces reviewable actions, drafts, and rationale
- ⬜ Policy gates for autonomous action by relationship, chat type, and professional status

---

## Ongoing / Cross-cutting

- ⬜ CI: GitHub Actions running `pytest` on every push (no live DB, fixtures only)
- ✅ Linting: `ruff` + `mypy --strict`
- ⬜ `scripts/health_check.sh` — verify imsg binary, permissions, data dir, rpc connectivity
- ⬜ Changelog maintenance
- ⬜ Prompt versioning: track system prompt versions alongside draft files so old drafts are
     reproducible (`prompt_version` field in draft frontmatter)
