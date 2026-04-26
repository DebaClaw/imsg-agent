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
