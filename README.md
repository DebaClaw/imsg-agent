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
metadata including filenames, MIME/UTI, sizes, missing flags, and original attachment
paths. It does not copy attachment file bytes into SQLite.

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

Useful options:

```bash
uv run imsg-archive backfill --chat-limit 10000 --history-limit 100000
uv run imsg-archive monitor --db ~/imsg-data/imessage.sqlite
uv run imsg-archive monitor --since-rowid 12345
```

Large archives can produce large single-line JSON-RPC responses. The default RPC read
limit is 256 MiB and can be changed in `config/imsg.json` with
`rpc_read_limit_bytes`.

For a persistent macOS process, run the monitor under your preferred supervisor
(`launchd`, `tmux`, `screen`, or a terminal session you keep open). Example:

```bash
cd ~/src/imsg-agent
uv run imsg-archive run
```

Inspect counts with SQLite:

```bash
sqlite3 ~/imsg-data/imessage.sqlite \
  'select (select count(*) from chats) as chats,
          (select count(*) from messages) as messages,
          (select count(*) from attachments) as attachments;'
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
