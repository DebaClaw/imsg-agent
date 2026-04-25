"""
rpc_client.py — Async wrapper around the `imsg rpc` subprocess.

Manages subprocess lifetime, sends JSON-RPC 2.0 requests, receives
responses and push notifications, and provides typed async methods.

No business logic lives here — this is a pure protocol adapter.

Protocol reference: ~/src/imsg/docs/rpc.md
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Attachment, Chat, Message, Reaction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IMsgRPCError(Exception):
    """Raised when the RPC server returns a JSON-RPC error object."""

    def __init__(self, error: dict[str, Any]) -> None:
        self.code: int = int(error.get("code", -1))
        self.message: str = str(error.get("message", "Unknown RPC error"))
        self.data: Any = error.get("data")
        super().__init__(f"RPC error {self.code}: {self.message}")


class IMsgRPCConnectionError(Exception):
    """Raised when the subprocess dies or the connection is lost."""


# ---------------------------------------------------------------------------
# Parsers — convert raw JSON dicts to typed models
# ---------------------------------------------------------------------------


def _dt(s: str | None) -> datetime:
    if not s:
        return datetime.now(UTC)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _parse_attachment(a: dict[str, Any]) -> Attachment:
    return Attachment(
        filename=a.get("filename") or "",
        transfer_name=a.get("transfer_name") or "",
        uti=a.get("uti") or "",
        mime_type=a.get("mime_type") or "",
        total_bytes=int(a.get("total_bytes") or 0),
        is_sticker=bool(a.get("is_sticker")),
        original_path=a.get("original_path") or "",
        missing=bool(a.get("missing")),
    )


def _parse_reaction(r: dict[str, Any]) -> Reaction:
    return Reaction(
        reaction_type=r.get("reaction_type") or r.get("type") or "",
        sender=r.get("sender") or "",
        is_from_me=bool(r.get("is_from_me")),
        date=_dt(r.get("date") or r.get("created_at")),
        emoji=r.get("emoji") or "",
    )


def _parse_message(data: dict[str, Any]) -> Message:
    attachments = [_parse_attachment(a) for a in (data.get("attachments") or [])]
    reactions = [_parse_reaction(r) for r in (data.get("reactions") or [])]
    return Message(
        rowid=int(data["id"]),
        chat_id=int(data["chat_id"]),
        guid=data.get("guid") or "",
        sender=data.get("sender") or "",
        text=data.get("text") or "",
        date=_dt(data.get("created_at")),
        is_from_me=bool(data.get("is_from_me")),
        service=data.get("service") or "",
        has_attachments=len(attachments) > 0,
        attachments=attachments,
        reactions=reactions,
        reply_to_guid=data.get("reply_to_guid") or None,
        thread_originator_guid=data.get("thread_originator_guid") or None,
        destination_caller_id=data.get("destination_caller_id") or None,
        is_reaction=bool(data.get("is_reaction")),
        reaction_type=data.get("reaction_type") or None,
        chat_identifier=data.get("chat_identifier") or "",
        chat_guid=data.get("chat_guid") or "",
        chat_name=data.get("chat_name") or "",
        participants=list(data.get("participants") or []),
        is_group=bool(data.get("is_group")),
    )


def _parse_chat(data: dict[str, Any]) -> Chat:
    return Chat(
        id=int(data["id"]),
        identifier=data.get("identifier") or "",
        name=data.get("name") or "",
        service=data.get("service") or "",
        last_message_at=_dt(data.get("last_message_at")),
        guid=data.get("guid") or "",
        participants=list(data.get("participants") or []),
        is_group=bool(data.get("is_group")),
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class IMsgRPCClient:
    """
    Async JSON-RPC 2.0 client over a persistent `imsg rpc` subprocess.

    Usage:
        client = IMsgRPCClient(Path("~/src/imsg/bin/imsg"))
        await client.start()
        try:
            chats = await client.list_chats()
            async for msg in client.subscribe(since_rowid=cursor):
                ...
        finally:
            await client.stop()
    """

    def __init__(
        self,
        binary: Path,
        timeout: float = 30.0,
        read_limit: int = 256 * 1024 * 1024,
    ) -> None:
        self._binary = binary
        self._timeout = timeout
        self._read_limit = read_limit
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        # Protects _next_id and the write-then-register sequence in _request
        self._id_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._subscriptions: dict[int, asyncio.Queue[Any]] = {}

    async def start(self) -> None:
        """Launch the imsg rpc subprocess and start the reader task."""
        self._process = await asyncio.create_subprocess_exec(
            str(self._binary),
            "rpc",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=self._read_limit,
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="imsg-rpc-reader"
        )
        logger.info("imsg rpc started (pid=%d)", self._process.pid)

    async def stop(self) -> None:
        """Gracefully stop the subprocess and reader task."""
        if self._process and self._process.stdin:
            with suppress(Exception):
                self._process.stdin.close()
        if self._process:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
        logger.info("imsg rpc stopped")

    # ------------------------------------------------------------------
    # Internal: reader loop and dispatcher
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    # EOF — process ended
                    self._fail_all(IMsgRPCConnectionError("imsg rpc process ended unexpectedly"))
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    obj: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning("Ignoring unparseable RPC line: %s (%s)", line[:200], exc)
                    continue
                self._dispatch(obj)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("RPC reader crashed: %s", exc, exc_info=True)
            self._fail_all(exc)

    def _dispatch(self, obj: dict[str, Any]) -> None:
        """Route an incoming JSON object to the right future or subscription queue."""
        req_id = obj.get("id")
        if req_id is not None:
            # Response to a request we sent
            future = self._pending.pop(req_id, None)
            if future and not future.done():
                if "error" in obj:
                    future.set_exception(IMsgRPCError(obj["error"]))
                else:
                    future.set_result(obj.get("result") or {})
        elif "method" in obj:
            # Push notification (watch subscription)
            params = obj.get("params") or {}
            sub_id = params.get("subscription")
            if sub_id is not None:
                queue = self._subscriptions.get(sub_id)
                if queue is not None:
                    method = obj["method"]
                    if method == "message":
                        queue.put_nowait(params.get("message"))
                    elif method == "error":
                        queue.put_nowait(IMsgRPCError(params.get("error") or {}))

    def _fail_all(self, exc: Exception) -> None:
        """Propagate a fatal error to all pending requests and subscriptions."""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        for queue in list(self._subscriptions.values()):
            queue.put_nowait(exc)

    # ------------------------------------------------------------------
    # Internal: send a request and await its response
    # ------------------------------------------------------------------

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._process or not self._process.stdin:
            raise IMsgRPCConnectionError("RPC client not started — call start() first")

        async with self._id_lock:
            req_id = self._next_id
            self._next_id += 1
            loop = asyncio.get_event_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[req_id] = future

        payload = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        self._process.stdin.write((payload + "\n").encode())
        await self._process.stdin.drain()

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout)
        except TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise IMsgRPCConnectionError(
                f"Request timed out after {self._timeout}s: {method}"
            ) from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_chats(self, limit: int = 20) -> list[Chat]:
        """Return the most recent chats, ordered by last message time."""
        result = await self._request("chats.list", {"limit": limit})
        return [_parse_chat(c) for c in result.get("chats") or []]

    async def get_history(
        self,
        chat_id: int,
        limit: int = 50,
        participants: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        include_attachments: bool = False,
    ) -> list[Message]:
        """Return message history for a chat, newest last."""
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "limit": limit,
            "attachments": include_attachments,
        }
        if participants:
            params["participants"] = participants
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        result = await self._request("messages.history", params)
        return [_parse_message(m) for m in result.get("messages") or []]

    async def subscribe(
        self,
        chat_id: int | None = None,
        since_rowid: int | None = None,
        include_reactions: bool = False,
        include_attachments: bool = False,
    ) -> AsyncGenerator[Message, None]:
        """
        Stream new messages as an async generator.

        Sends watch.subscribe, then yields each arriving message notification.
        Automatically unsubscribes when the generator is closed (break / exception / GC).

        Args:
            chat_id: If set, only stream messages from this chat.
            since_rowid: Resume from this rowid — messages after it are delivered first,
                         then new arrivals stream live.
            include_reactions: Whether to include tapback reaction events.
            include_attachments: Whether to include attachment metadata.
        """
        params: dict[str, Any] = {
            "include_reactions": include_reactions,
            "attachments": include_attachments,
        }
        if chat_id is not None:
            params["chat_id"] = chat_id
        if since_rowid is not None:
            params["since_rowid"] = since_rowid

        result = await self._request("watch.subscribe", params)
        sub_id: int = int(result["subscription"])
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._subscriptions[sub_id] = queue
        logger.debug("Subscribed (id=%d, since_rowid=%s)", sub_id, since_rowid)

        try:
            while True:
                item = await queue.get()
                if isinstance(item, Exception):
                    raise item
                if item is None:
                    return
                yield _parse_message(item)
        finally:
            self._subscriptions.pop(sub_id, None)
            try:
                await self._request("watch.unsubscribe", {"subscription": sub_id})
                logger.debug("Unsubscribed (id=%d)", sub_id)
            except Exception:
                pass  # Best-effort cleanup

    async def send(
        self,
        text: str = "",
        file: str | None = None,
        service: str = "auto",
        chat_id: int | None = None,
        to: str | None = None,
        chat_identifier: str | None = None,
        chat_guid: str | None = None,
    ) -> None:
        """Send a message or attachment."""
        params: dict[str, Any] = {"text": text, "service": service}
        if file:
            params["file"] = file
        if chat_id is not None:
            params["chat_id"] = chat_id
        if to:
            params["to"] = to
        if chat_identifier:
            params["chat_identifier"] = chat_identifier
        if chat_guid:
            params["chat_guid"] = chat_guid
        await self._request("send", params)
