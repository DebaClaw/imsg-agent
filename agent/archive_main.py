"""
archive_main.py - CLI for local iMessage SQLite archive.

This path does not import or call any GenAI drafting code.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from dotenv import load_dotenv

from .archive_store import IMessageArchive
from .archiver import IMessageArchiver
from .config import Config, load_config
from .rpc_client import IMsgRPCClient

logger = logging.getLogger(__name__)


def archive_db_path(config: Config) -> Path:
    return config.data_dir / "imessage.sqlite"


async def run_backfill(args: argparse.Namespace) -> None:
    config = load_config()
    archive = IMessageArchive(Path(args.db or archive_db_path(config)))
    rpc = IMsgRPCClient(
        config.imsg_binary,
        timeout=float(config.rpc_timeout_seconds),
        read_limit=config.rpc_read_limit_bytes,
    )
    await rpc.start()
    try:
        chats, messages = await IMessageArchiver(archive, rpc).backfill(
            chat_limit=args.chat_limit,
            history_limit=args.history_limit,
            history_page_size=args.history_page_size,
            include_attachments=not args.no_attachments,
            debug=args.debug,
        )
        logger.info(
            "Backfill complete chats=%d messages=%d total_chats=%d "
            "total_messages=%d attachments=%d",
            chats,
            messages,
            archive.count_chats(),
            archive.count_messages(),
            archive.count_attachments(),
        )
    finally:
        await rpc.stop()
        archive.close()


async def run_monitor(args: argparse.Namespace) -> None:
    config = load_config()
    archive = IMessageArchive(Path(args.db or archive_db_path(config)))
    rpc = IMsgRPCClient(
        config.imsg_binary,
        timeout=float(config.rpc_timeout_seconds),
        read_limit=config.rpc_read_limit_bytes,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        logger.info("Shutdown signal received; stopping archive monitor")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    await rpc.start()
    monitor_task = asyncio.create_task(
        IMessageArchiver(archive, rpc).monitor(
            since_rowid=args.since_rowid,
            include_attachments=not args.no_attachments,
        )
    )
    try:
        await stop_event.wait()
    finally:
        monitor_task.cancel()
        await rpc.stop()
        archive.close()


async def run_forever(args: argparse.Namespace) -> None:
    await run_backfill(args)
    await run_monitor(args)


def _add_options(
    parser: argparse.ArgumentParser,
    *,
    defaults: bool,
) -> None:
    default = None if defaults else argparse.SUPPRESS
    parser.add_argument(
        "--db",
        default=default,
        help="SQLite DB path. Defaults to ~/imsg-data/imessage.sqlite",
    )
    parser.add_argument("--chat-limit", type=int, default=10_000 if defaults else default)
    parser.add_argument(
        "--history-limit",
        type=int,
        default=100_000 if defaults else default,
    )
    parser.add_argument(
        "--history-page-size",
        type=int,
        default=100 if defaults else default,
    )
    parser.add_argument("--since-rowid", type=int, default=default)
    parser.add_argument(
        "--no-attachments",
        action="store_true",
        default=False if defaults else default,
        help=(
            "Do not request attachment/reaction metadata from imsg. This is a "
            "diagnostic/degraded mode; message rows are still archived."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False if defaults else default,
        help="Enable verbose archive progress logs",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive iMessage chats and messages to local SQLite without GenAI."
    )
    _add_options(parser, defaults=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backfill = subparsers.add_parser(
        "backfill",
        help="Fetch chats and historical messages, then exit",
    )
    monitor = subparsers.add_parser(
        "monitor",
        help="Watch new messages and append them to SQLite",
    )
    run = subparsers.add_parser("run", help="Backfill once, then monitor")
    for subparser in (backfill, monitor, run):
        _add_options(subparser, defaults=False)
    return parser


def cli() -> None:
    load_dotenv()
    parser = _parser()
    args = parser.parse_args()
    log_level = "DEBUG" if args.debug else os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    if args.command == "backfill":
        asyncio.run(run_backfill(args))
    elif args.command == "monitor":
        asyncio.run(run_monitor(args))
    elif args.command == "run":
        asyncio.run(run_forever(args))


if __name__ == "__main__":
    cli()
