# imsg-agent

`imsg-agent` is a file-based relationship maintenance agent for iMessage. It watches
new messages through the `imsg rpc` bridge, keeps per-chat context and history, drafts
thoughtful replies in the operator's voice, and sends only after an approval path says
the message is ready.

The goal is not to automate relationships away. The goal is to help the operator notice
what needs attention, understand the relevant context, and respond with care.

It also includes `imsg-archive`, a separate no-GenAI archive command that backfills chats
and messages into a local SQLite database and then monitors new messages into that same
database.

## Features

- **iMessage ingestion through `imsg rpc`**: no direct reads from
  `~/Library/Messages/chat.db` in this package.
- **Local SQLite archive**: `imsg-archive` stores chats, messages, attachment metadata,
  reactions, and a live cursor in `~/imsg-data/imessage.sqlite`.
- **Archive visibility CLI**: read-only commands show archive totals, recent chats,
  unanswered inbound conversations, unresolved contact matches, and attachment issues.
- **No-GenAI archive mode**: archive backfill and monitoring do not import or call the
  drafting system or any model API.
- **Human-readable data store**: inbox items, chat context, history, drafts, outbox,
  sent archives, errors, nudges, and digests live under `~/imsg-data/` as Markdown with
  YAML frontmatter.
- **Per-chat context isolation**: drafting reads only `chats/{chat_id}/context.md` and
  `chats/{chat_id}/history.md` for the chat being handled.
- **OpenAI drafting**: drafts are created through the OpenAI Responses API, with
  `gpt-5.5` as the default model and per-chat overrides available through `context.md`.
- **Manual and opt-in automatic approval**: drafts default to `approved: false`.
  Per-chat `auto_approve: true` can approve drafts automatically for non-professional
  one-on-one chats.
- **Safety-first sending**: messages are sent only from `outbox/`; every successful send
  is archived to `sent/` with reasoning and source draft metadata, and failures go to
  `errors/`.
- **Group and professional chat safeguards**: professional or unknown-professional chats
  require manual approval, and group chats require explicit drafting opt-in.
- **Proactive nudges and digests**: quiet unanswered conversations can create notices in
  `nudges/`, and daily digests can be written to `digests/`.

## Project Layout

```text
agent/
  config.py       Load config/imsg.json and environment overrides
  rpc_client.py   Async JSON-RPC client for the imsg subprocess
  store.py        All ~/imsg-data file reads/writes
  inbox.py        Deduplicate and ingest new messages
  drafter.py      Build per-chat prompts and call OpenAI
  sender.py       Move approved drafts to outbox and send archived items
  nudger.py       Write proactive follow-up nudges
  summarizer.py   Write daily conversation digests
  archive_store.py SQLite archive schema and writes
  archiver.py     Non-GenAI archive backfill and monitor
  archive_main.py CLI for imsg-archive
  main.py         Runtime event loop

config/imsg.json  Default local configuration
scripts/setup.sh  Environment and data-directory setup
scripts/install_launchd.sh  Install a user LaunchAgent for archive monitoring
tests/            Unit tests with fixtures, no live Messages database required
```

## Getting Started

### 1. Prerequisites

- macOS with Messages configured.
- `uv` for Python dependency management and command execution.
- A working `imsg` executable available on `PATH`. Set `IMSG_BINARY` only if your
  executable is somewhere that the agent process cannot find through `PATH`.
- Full Disk Access granted to the terminal app that runs `imsg-agent`.
- An OpenAI API key for drafting. This is not needed for `imsg-archive`.

Before continuing, verify the configured executable path:

```bash
command -v "${IMSG_BINARY:-imsg}"
```

If that command fails, install or build `imsg` in its own repository first, or set
`IMSG_BINARY` to the executable path. `scripts/setup.sh` performs the same validation
and stops if the binary is missing.

### 2. Install dependencies

Use `uv` for all Python commands:

```bash
cd ~/src/imsg-agent
uv sync
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set:

```bash
OPENAI_API_KEY=sk-...
```

Optional overrides:

```bash
IMSG_DATA_DIR=~/imsg-data
IMSG_BINARY=/opt/homebrew/bin/imsg
IMSG_DRAFT_MODEL=gpt-5.5
LOG_LEVEL=INFO
```

### 4. Initialize local data

```bash
uv run bash scripts/setup.sh
```

This creates the `~/imsg-data/` directory tree and verifies the `imsg` binary can read
Messages through its own supported interface.

### 5. Run tests

```bash
uv run pytest tests/ -v
uv run ruff check .
uv run mypy agent tests
```

The unit tests do not touch live iMessage data.

### 6. Run the agent

```bash
uv run python -m agent.main
```

The agent runs until `SIGINT` or `SIGTERM`. It finishes the current message, checkpoints
the cursor, and exits cleanly.

## Persistent iMessage Archive Monitor

Use this path when you want a local database of iMessage data with no GenAI involved.
The archive stores what `imsg rpc` exposes: chats, messages, reactions, and attachment
metadata including filenames, MIME/UTI, sizes, missing flags, original attachment paths,
and local archive paths. Attachment bytes are copied to `~/imsg-data/attachments/`; the
SQLite database stores metadata and paths, not file blobs.

The default database path is:

```text
~/imsg-data/imessage.sqlite
```

Backfill all chats and historical messages that `imsg` can find:

```bash
uv run imsg-archive backfill
```

Backfill once, then keep monitoring forever:

```bash
uv run imsg-archive run
```

Only monitor new messages using the saved cursor:

```bash
uv run imsg-archive monitor
```

Install a persistent user LaunchAgent for monitoring:

```bash
cd ~/src/imsg-agent
bash scripts/install_launchd.sh
```

That installs `com.imsg-agent.archive-monitor` and runs `uv run imsg-archive monitor`
with attachments enabled. Inspect it with:

```bash
launchctl print gui/$(id -u)/com.imsg-agent.archive-monitor
tail -f ~/imsg-data/logs/imsg-archive-monitor.log
tail -f ~/imsg-data/logs/imsg-archive-monitor.err.log
```

Stop it with:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.imsg-agent.archive-monitor.plist
```

Fetch attachment metadata and copy available attachment files for archived messages:

```bash
uv run imsg-archive attachments --debug --history-page-size 250
```

Sync Contacts data from `contacts-mcp`, then match archived chats by normalized
phone/email identifiers:

```bash
cd ~/src/contacts-mcp
bun dist/index.js sync-provider --provider apple --direction pull

cd ~/src/imsg-agent
uv run imsg-archive contacts sync \
  --contacts-command "bun /Users/zob/src/contacts-mcp/dist/index.js"
uv run imsg-archive contacts enrich
```

Useful options:

```bash
uv run imsg-archive backfill --chat-limit 10000 --history-limit 100000
uv run imsg-archive backfill --history-page-size 100
uv run imsg-archive backfill --debug --history-page-size 50
uv run imsg-archive backfill --debug --no-attachments --history-page-size 50
uv run imsg-archive attachments --debug --history-page-size 250
uv run imsg-archive contacts sync --contacts-command "bun /Users/zob/src/contacts-mcp/dist/index.js"
uv run imsg-archive contacts enrich --default-country US
uv run imsg-archive monitor --db ~/imsg-data/imessage.sqlite
uv run imsg-archive monitor --since-rowid 12345
uv run imsg-archive stats
uv run imsg-archive recent --limit 25
uv run imsg-archive needs-reply --limit 50
uv run imsg-archive unresolved --limit 50
uv run imsg-archive attachment-issues --limit 50
uv run imsg-archive needs-reply --json
```

Backfill pages each chat's history using `--history-page-size` so large chats do not need
to return every message in one RPC response. If a page still times out, lower
`--history-page-size` or increase `rpc_timeout_seconds` in `config/imsg.json`.
If you rerun backfill after a partial archive, it resumes each chat below the oldest
message already stored in SQLite instead of starting over from the newest page.
Backfill also retries timed-out pages with progressively smaller page sizes down to one
message before skipping that page and continuing.
If a one-message page still times out with attachment metadata enabled, it retries that
one-message page without attachment metadata so the message row is still archived.
Use `--debug` to print each chat/page boundary, elapsed request time, rowid/date range,
attachment count, and retry decisions.
Use `--no-attachments` as a diagnostic/degraded mode when `messages.history` responds
quickly without attachment metadata but stalls while expanding attachments or reactions.
That mode still archives chats and messages, but `attachments` and `reactions` tables
will not be populated for those fetched messages.
For live monitoring, the difference is the same: with attachments enabled, new messages
are enriched with attachment/reaction metadata and available files are copied immediately;
with `--no-attachments`, the monitor writes message rows faster but leaves attachment and
reaction detail for a later `imsg-archive attachments` pass.
Run `imsg-archive attachments` after a fast `--no-attachments` backfill to enrich the
archive with attachment metadata and copy files into `~/imsg-data/attachments/`. The
attachment pass uses the same timeout backoff behavior as message backfill.
Run `imsg-archive contacts sync` after syncing/exporting Contacts through
`contacts-mcp`; then run `imsg-archive contacts enrich` to populate deterministic
chat/contact matches. Exact normalized phone and email matches are linked automatically.
Ambiguous and unresolved identifiers are recorded in `chat_contact_matches` for review.

For a scriptable archive dashboard, use the read-only commands:

```bash
uv run imsg-archive stats
uv run imsg-archive recent --limit 25
uv run imsg-archive needs-reply --limit 50
uv run imsg-archive unresolved --limit 50
uv run imsg-archive attachment-issues --limit 50
```

Add `--json` to use the output from other scripts.

You can also inspect counts directly with SQLite:

```bash
sqlite3 ~/imsg-data/imessage.sqlite \
  'select (select count(*) from chats) as chats,
          (select count(*) from messages) as messages,
          (select count(*) from attachments) as attachments,
          (select count(*) from attachments where archived = 1) as saved_attachments,
          (select count(*) from contacts) as contacts,
          (select count(*) from chat_contact_matches where status = "matched") as matched_contact_points;'
```

## Approval Workflow

1. A new inbound message is written to `~/imsg-data/inbox/`.
2. The drafter reads only that chat's `context.md` and `history.md`.
3. A draft appears in `~/imsg-data/chats/{chat_id}/drafts/{uuid}.md`.
4. The operator reviews or edits the draft.
5. Setting `approved: true` moves the draft to `outbox/` on the next maintenance pass.
6. The sender archives the message to `sent/` before calling `imsg rpc send`.
7. If the send fails, the archive is moved to `errors/` with the failure reason.

For automatic approval, set this in a chat's `context.md`:

```yaml
professional: false
auto_approve: true
do_not_draft: false
```

Automatic approval is ignored for professional chats, unknown-professional chats, and
group chats.

## Chat Context

Each chat has a context file at:

```text
~/imsg-data/chats/{chat_id}/context.md
```

Useful frontmatter fields:

```yaml
chat_id: 7
name: "Alex"
service: iMessage
participants: ["+14155550101"]
relationship: "close friend"
tone: "casual, warm"
professional: false
auto_approve: false
do_not_draft: false
agent_notes: "Usually texts in the evening."
model: null
```

The body below the frontmatter is freeform operator notes and is included in draft
context for that chat only.

## Safety Rules

- Do not read Messages data directly from SQLite in this package; use `imsg rpc`.
- Do not let context from one chat influence another.
- Do not send anything unless it exists as an outbox item.
- Do not auto-approve professional or unknown-professional chats.
- Do not auto-approve group chats.
- Do not send attachments outside `~/imsg-data/outbox/attachments/`.
- Do not commit `~/imsg-data/` or `.env`.

## Development

Common commands:

```bash
uv sync
uv run pytest tests/ -v
uv run ruff check .
uv run mypy agent tests
uv run imsg-archive --help
```

When a work item is complete and validated, commit and push the change:

```bash
git status --short
git add <changed files>
git commit -m "Describe the completed work"
git push origin main
```
