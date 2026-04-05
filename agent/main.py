"""
main.py — Agent entrypoint and event loop.

Lifecycle per pass:
    1. WAKE        Read state.json cursor
    2. POLL        Connect to imsg rpc, subscribe since cursor
    3. INGEST      Write new messages to inbox/, update chat context
    4. DRAFT       Propose responses for unprocessed inbox items (Phase 2)
    5. SEND        Process approved outbox items (Phase 2)
    6. CHECKPOINT  Advance cursor in state.json

Run:
    python -m agent.main
    # or via installed entrypoint:
    imsg-agent
"""
from __future__ import annotations

# TODO (Phase 1): Implement main loop.
#
# Rough structure:
#
#   async def run_once(config: Config, store: MessageStore,
#                      rpc: IMsgRPCClient) -> None:
#       cursor = store.read_cursor()
#       max_rowid = cursor
#       async for message in rpc.subscribe(since_rowid=cursor):
#           processed = await inbox.process(message)
#           if processed and message.rowid > max_rowid:
#               max_rowid = message.rowid
#       store.write_cursor(max_rowid)
#
#   async def main() -> None:
#       config = load_config()
#       store = MessageStore(config.data_dir)
#       rpc = IMsgRPCClient(config.imsg_binary)
#       await rpc.start()
#       try:
#           while True:
#               await run_once(config, store, rpc)
#               await asyncio.sleep(config.poll_interval_seconds)
#       finally:
#           await rpc.stop()
#
# Signal handling:
#   SIGTERM / SIGINT: finish current pass, checkpoint, exit cleanly
#   (do not interrupt mid-batch — partial batches leave cursor unchanged)
