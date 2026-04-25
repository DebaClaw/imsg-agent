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
