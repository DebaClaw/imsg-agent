from __future__ import annotations

from agent.archive_main import _parser


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
            "bun /Users/zob/src/contacts-mcp/dist/index.js",
            "--contacts-store",
            "/tmp/contacts",
        ]
    )

    assert args.command == "contacts"
    assert args.contacts_command_name == "sync"
    assert args.contacts_command == "bun /Users/zob/src/contacts-mcp/dist/index.js"
    assert args.contacts_store == "/tmp/contacts"


def test_archive_cli_has_contacts_enrich_command() -> None:
    args = _parser().parse_args(["contacts", "enrich", "--default-country", "US"])

    assert args.command == "contacts"
    assert args.contacts_command_name == "enrich"
    assert args.default_country == "US"


def test_archive_cli_has_visibility_commands() -> None:
    stats = _parser().parse_args(["stats", "--json"])
    recent = _parser().parse_args(["recent", "--limit", "5"])
    needs_reply = _parser().parse_args(["needs-reply", "--limit", "10", "--json"])
    unresolved = _parser().parse_args(["unresolved"])
    attachment_issues = _parser().parse_args(["attachment-issues"])

    assert stats.command == "stats"
    assert stats.json_output is True
    assert recent.command == "recent"
    assert recent.limit == 5
    assert needs_reply.command == "needs-reply"
    assert needs_reply.limit == 10
    assert needs_reply.json_output is True
    assert unresolved.command == "unresolved"
    assert attachment_issues.command == "attachment-issues"
