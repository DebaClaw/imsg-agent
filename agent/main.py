"""
main.py — Agent entrypoint and event loop.

Lifecycle:
    1. WAKE        Read state.json cursor
    2. POLL        Subscribe to imsg rpc since that cursor
    3. INGEST      Write new messages to inbox/, update chat context + history
    4. CHECKPOINT  Advance cursor after each message (written to state.json)
    5. SHUTDOWN    On SIGTERM/SIGINT: finish current message, checkpoint, exit cleanly

Run:
    python -m agent.main
    # or via installed script:
    imsg-agent
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress

from dotenv import load_dotenv

from .config import Config, load_config
from .drafter import Drafter, OpenAIResponsesDraftingClient
from .inbox import InboxProcessor
from .nudger import Nudger
from .rpc_client import IMsgRPCClient
from .sender import ApprovalScanner, Sender
from .store import MessageStore

logger = logging.getLogger(__name__)


async def run(config: Config) -> None:
    """Main agent loop — runs until a stop signal is received."""
    store = MessageStore(config.data_dir)
    rpc = IMsgRPCClient(
        config.imsg_binary,
        timeout=float(config.rpc_timeout_seconds),
        read_limit=config.rpc_read_limit_bytes,
    )
    inbox = InboxProcessor(store, max_history=config.chat_context_messages)
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
        logger.warning("OPENAI_API_KEY is not set; drafting is disabled")
    approval = ApprovalScanner(store)
    sender = Sender(store, rpc, service=config.default_service)
    nudger = Nudger(store, quiet_after_hours=config.nudge_after_hours)
    maintenance_lock = asyncio.Lock()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        logger.info("Shutdown signal received — stopping after current message")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    async def _maintenance_pass() -> None:
        async with maintenance_lock:
            if drafter is not None:
                await drafter.run_pass()
            approval.run_pass()
            await sender.run_pass()
            nudger.run_pass()

    async def _maintenance_loop() -> None:
        while not stop_event.is_set():
            try:
                await _maintenance_pass()
            except Exception:
                logger.exception("Maintenance pass failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=config.maintenance_interval_seconds,
                )

    await rpc.start()
    maintenance_task = asyncio.create_task(_maintenance_loop(), name="imsg-maintenance")
    try:
        cursor = store.read_cursor()
        logger.info("Agent starting — cursor=%d data_dir=%s", cursor, config.data_dir)

        async for message in rpc.subscribe(
            since_rowid=cursor if cursor > 0 else None
        ):
            if stop_event.is_set():
                logger.info("Stop event set — exiting watch loop")
                break

            processed = inbox.process(message)

            # Advance cursor immediately after each successful ingest.
            # If we crash here, the next start will re-deliver this message
            # (inbox_exists() deduplication prevents double-writing it).
            if processed and message.rowid > cursor:
                cursor = message.rowid
                store.write_cursor(cursor)
                await _maintenance_pass()

        logger.info("Agent stopped cleanly — final cursor=%d", cursor)

    finally:
        stop_event.set()
        maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance_task
        await rpc.stop()


def cli() -> None:
    """Entrypoint for the `imsg-agent` command."""
    load_dotenv()
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    config = load_config()
    asyncio.run(run(config))


if __name__ == "__main__":
    cli()
