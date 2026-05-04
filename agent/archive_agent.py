"""
archive_agent.py - AI worker that consumes the SQLite archive.

The archive monitor owns iMessage ingestion. This worker does not subscribe to
`imsg rpc`; it reads archived messages from SQLite, proposes reviewable drafts,
and optionally sends already-approved outbox items.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from contextlib import suppress
from pathlib import Path

from dotenv import load_dotenv

from .archive_store import IMessageArchive
from .config import Config, load_config
from .drafter import Drafter, DraftingRateLimitError, OpenAIResponsesDraftingClient
from .rpc_client import IMsgRPCClient
from .sender import ApprovalScanner, Sender
from .store import MessageStore

logger = logging.getLogger(__name__)


def archive_db_path(config: Config) -> Path:
    return config.data_dir / "imessage.sqlite"


class ArchiveAgentWorker:
    def __init__(
        self,
        *,
        archive: IMessageArchive,
        store: MessageStore,
        drafter: Drafter | None,
        history_limit: int,
    ) -> None:
        self._archive = archive
        self._store = store
        self._drafter = drafter
        self._history_limit = history_limit

    async def run_once(self, *, limit: int = 50) -> int:
        if self._drafter is None:
            return 0
        processed = 0
        cursor = self._archive.read_agent_cursor()
        for message in self._archive.inbound_messages_after(cursor, limit=limit):
            self._ensure_chat_context(message.chat_id, source_rowid=message.rowid)
            history = self._archive.chat_history_markdown(
                message.chat_id,
                through_rowid=message.rowid,
                limit=self._history_limit,
            )
            await self._drafter.process_message(message, history_override=history)
            self._archive.write_agent_cursor(message.rowid)
            processed += 1
            logger.info(
                "Processed archived message rowid=%d chat_id=%d",
                message.rowid,
                message.chat_id,
            )
        return processed

    def _ensure_chat_context(self, chat_id: int, *, source_rowid: int) -> None:
        context, body = self._store.read_chat_context_document(chat_id)
        seed = self._archive.chat_context_seed(chat_id)
        if not seed:
            return

        updated = dict(context)
        updated.setdefault("chat_id", chat_id)
        updated.setdefault("name", seed.get("name") or f"chat {chat_id}")
        updated.setdefault("identifier", seed.get("identifier") or "")
        updated.setdefault("service", seed.get("service") or "")
        updated.setdefault("participants", seed.get("participants") or [])
        updated["last_active"] = seed.get("last_message_at") or updated.get("last_active")
        updated["last_seen_rowid"] = max(
            int(updated.get("last_seen_rowid") or 0),
            source_rowid,
        )
        if bool(seed.get("is_group")) and "do_not_draft" not in updated:
            updated["do_not_draft"] = True
        if body and "notes" not in updated:
            updated["notes"] = body
        self._store.write_chat_context(chat_id, updated)


async def run(config: Config, *, db_path: Path | None = None, no_send: bool = False) -> None:
    archive = IMessageArchive(db_path or archive_db_path(config))
    store = MessageStore(config.data_dir)
    drafter: Drafter | None = None
    if config.openai_api_key:
        drafter = Drafter(
            store,
            OpenAIResponsesDraftingClient(api_key=config.openai_api_key),
            default_model=config.draft_model,
            max_inbox_age_hours=config.max_inbox_age_hours,
            auto_approve_default=config.auto_approve,
        )
    else:
        logger.warning("OPENAI_API_KEY is not set; archive-backed drafting is disabled")

    worker = ArchiveAgentWorker(
        archive=archive,
        store=store,
        drafter=drafter,
        history_limit=config.chat_context_messages,
    )
    approval = ApprovalScanner(store)
    rpc: IMsgRPCClient | None = None
    sender: Sender | None = None
    if not no_send:
        rpc = IMsgRPCClient(
            config.imsg_binary,
            timeout=float(config.rpc_timeout_seconds),
            read_limit=config.rpc_read_limit_bytes,
        )
        sender = Sender(store, rpc, service=config.default_service)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        logger.info("Shutdown signal received; stopping archive-backed agent worker")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    if rpc is not None:
        await rpc.start()
    try:
        logger.info(
            "Archive-backed agent worker starting db=%s data_dir=%s no_send=%s",
            archive.path,
            config.data_dir,
            no_send,
        )
        while not stop_event.is_set():
            try:
                await worker.run_once()
                approval.run_pass()
                if sender is not None:
                    await sender.run_pass()
            except DraftingRateLimitError as exc:
                retry_after = max(exc.retry_after_seconds, config.maintenance_interval_seconds)
                logger.warning(
                    "Drafting rate limited; pausing worker for %.1fs before retrying",
                    retry_after,
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=retry_after)
                continue
            except Exception:
                logger.exception("Archive-backed agent worker pass failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=config.maintenance_interval_seconds,
                )
    finally:
        if rpc is not None:
            await rpc.stop()
        archive.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the archive-backed imsg-agent worker."
    )
    parser.add_argument("--db", help="Path to imessage.sqlite. Defaults to configured data dir.")
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="Draft only; do not send approved outbox items.",
    )
    return parser


def cli() -> None:
    load_dotenv()
    args = _parser().parse_args()
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    config = load_config()
    asyncio.run(
        run(
            config,
            db_path=Path(args.db).expanduser() if args.db else None,
            no_send=bool(args.no_send),
        )
    )


if __name__ == "__main__":
    cli()
