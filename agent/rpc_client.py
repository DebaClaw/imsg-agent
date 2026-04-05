"""
rpc_client.py — Async wrapper around the `imsg rpc` subprocess.

Manages subprocess lifetime, sends JSON-RPC 2.0 requests, receives
responses and push notifications, and provides typed async methods.

This module has NO business logic. It is a pure protocol adapter.
"""
from __future__ import annotations

# TODO (Phase 1): Implement subprocess management and JSON-RPC protocol.
#
# Interface to implement:
#
#   class IMsgRPCClient:
#       async def start(self) -> None
#           Launch `imsg rpc` subprocess, open stdin/stdout pipes.
#
#       async def stop(self) -> None
#           Gracefully close subprocess.
#
#       async def list_chats(self, limit: int = 20) -> list[Chat]
#           → chats.list RPC call
#
#       async def get_history(self, chat_id: int, limit: int = 50,
#                             participants: list[str] | None = None,
#                             start: str | None = None,
#                             end: str | None = None) -> list[Message]
#           → messages.history RPC call
#
#       async def subscribe(self, chat_id: int | None = None,
#                           since_rowid: int | None = None,
#                           include_reactions: bool = False) -> AsyncIterator[Message]
#           → watch.subscribe RPC call; yields Message on each notification
#
#       async def unsubscribe(self, subscription_id: int) -> None
#           → watch.unsubscribe RPC call
#
#       async def send(self, chat_id: int | None = None,
#                      to: str | None = None,
#                      text: str = "",
#                      file: str | None = None,
#                      service: str = "auto") -> None
#           → send RPC call
#
# Error handling:
#   - Raise IMsgRPCError for JSON-RPC error responses
#   - Raise IMsgRPCConnectionError if subprocess dies unexpectedly
#   - Reconnect automatically up to 3 times before raising
#
# See: ~/src/imsg/docs/rpc.md for full protocol reference
