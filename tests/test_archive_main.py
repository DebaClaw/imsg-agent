from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pytest

from agent.archive_main import _parser, run_monitor
from agent.config import Config


def test_archive_cli_accepts_options_after_subcommand() -> None:
    args = _parser().parse_args(
        ["backfill", "--debug", "--no-attachments", "--history-page-size", "50"]
    )

    assert args.command == "backfill"
    assert args.debug is True
    assert args.no_attachments is True
    assert args.history_page_size == 50


def test_archive_cli_accepts_options_before_subcommand() -> None:
    args = _parser().parse_args(["--debug", "--history-page-size", "50", "backfill"])

    assert args.command == "backfill"
    assert args.debug is True
    assert args.history_page_size == 50


def test_archive_cli_has_attachments_command() -> None:
    args = _parser().parse_args(["attachments", "--history-page-size", "25"])

    assert args.command == "attachments"
    assert args.history_page_size == 25


def test_archive_cli_has_contacts_sync_command() -> None:
    args = _parser().parse_args(
        [
            "contacts",
            "sync",
            "--contacts-command",
            "bun ${HOME}/src/contacts-mcp/dist/index.js",
            "--contacts-store",
            "/tmp/contacts",
        ]
    )

    assert args.command == "contacts"
    assert args.contacts_command_name == "sync"
    assert args.contacts_command == "bun ${HOME}/src/contacts-mcp/dist/index.js"
    assert args.contacts_store == "/tmp/contacts"


def test_archive_cli_has_contacts_enrich_command() -> None:
    args = _parser().parse_args(["contacts", "enrich", "--default-country", "US"])

    assert args.command == "contacts"
    assert args.contacts_command_name == "enrich"
    assert args.default_country == "US"


def test_archive_cli_has_visibility_commands() -> None:
    stats = _parser().parse_args(["stats", "--json"])
    recent = _parser().parse_args(["recent", "--limit", "5"])
    attention = _parser().parse_args(["attention", "--limit", "7", "--json"])
    needs_reply = _parser().parse_args(["needs-reply", "--limit", "10", "--json"])
    pending = _parser().parse_args(["pending", "--limit", "5", "--json"])
    unresolved = _parser().parse_args(["unresolved"])
    attachment_issues = _parser().parse_args(["attachment-issues"])
    search = _parser().parse_args(
        ["search", "messages", "coffee", "--chat-id", "7", "--since", "2026-01-01"]
    )

    assert stats.command == "stats"
    assert stats.json_output is True
    assert recent.command == "recent"
    assert recent.limit == 5
    assert attention.command == "attention"
    assert attention.limit == 7
    assert attention.json_output is True
    assert needs_reply.command == "needs-reply"
    assert needs_reply.limit == 10
    assert needs_reply.json_output is True
    assert pending.command == "pending"
    assert pending.limit == 5
    assert pending.json_output is True
    assert unresolved.command == "unresolved"
    assert attachment_issues.command == "attachment-issues"
    assert search.command == "search"
    assert search.search_command_name == "messages"
    assert search.query == "coffee"
    assert search.chat_id == 7
    assert search.since == "2026-01-01"


def test_archive_monitor_exits_when_subscription_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = Config(
        imsg_binary=Path("imsg"),
        data_dir=tmp_path,
        rpc_timeout_seconds=30,
        rpc_read_limit_bytes=1024,
        watch_debounce_ms=250,
        history_limit=50,
        chat_context_messages=20,
        auto_approve=False,
        default_service="auto",
        max_inbox_age_hours=48,
        openai_api_key=None,
        draft_model="gpt-5.5",
        maintenance_interval_seconds=5.0,
        nudge_after_hours=72,
        contacts_command="contacts-mcp",
        contacts_store=None,
    )
    events: list[str] = []

    class FakeArchive:
        def __init__(self, _path: Path) -> None:
            pass

        def close(self) -> None:
            events.append("archive.close")

    class FakeRPC:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def start(self) -> None:
            events.append("rpc.start")

        async def stop(self) -> None:
            events.append("rpc.stop")

    class FailingArchiver:
        def __init__(self, _archive: FakeArchive, _rpc: FakeRPC) -> None:
            pass

        async def monitor(self, **_kwargs: object) -> None:
            raise RuntimeError("subscription lost")

    monkeypatch.setattr("agent.archive_main.load_config", lambda: config)
    monkeypatch.setattr("agent.archive_main.IMessageArchive", FakeArchive)
    monkeypatch.setattr("agent.archive_main.IMsgRPCClient", FakeRPC)
    monkeypatch.setattr("agent.archive_main.IMessageArchiver", FailingArchiver)
    args = argparse.Namespace(db=None, since_rowid=None, no_attachments=False)

    with pytest.raises(RuntimeError, match="subscription lost"):
        asyncio.run(run_monitor(args))

    assert events == ["rpc.start", "rpc.stop", "archive.close"]
