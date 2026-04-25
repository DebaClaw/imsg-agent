"""
Tests for rpc_client.py — all subprocess I/O is mocked.
No live imsg binary is required.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.rpc_client import (
    IMsgRPCClient,
    IMsgRPCConnectionError,
    IMsgRPCError,
    _parse_chat,
    _parse_message,
)

# ---------------------------------------------------------------------------
# Sample payloads (match the shape imsg rpc actually sends)
# ---------------------------------------------------------------------------

SAMPLE_MESSAGE = {
    "id": 12345,
    "chat_id": 7,
    "guid": "AAAABBBB-1111-2222-3333-444455556666",
    "sender": "+14155550101",
    "text": "Hey, are we still on for Thursday?",
    "created_at": "2026-04-04T10:30:00Z",
    "is_from_me": False,
    "service": "iMessage",
    "attachments": [],
    "reactions": [],
    "reply_to_guid": None,
    "destination_caller_id": None,
}

SAMPLE_CHAT = {
    "id": 7,
    "identifier": "iMessage;-;+14155550101",
    "guid": "iMessage;-;+14155550101",
    "name": "Alex",
    "service": "iMessage",
    "last_message_at": "2026-04-04T10:30:00Z",
    "participants": ["+14155550101"],
}


# ---------------------------------------------------------------------------
# Pure parser unit tests (no asyncio)
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_basic_fields(self) -> None:
        msg = _parse_message(SAMPLE_MESSAGE)
        assert msg.rowid == 12345
        assert msg.chat_id == 7
        assert msg.guid == "AAAABBBB-1111-2222-3333-444455556666"
        assert msg.sender == "+14155550101"
        assert msg.text == "Hey, are we still on for Thursday?"
        assert msg.is_from_me is False
        assert msg.service == "iMessage"
        assert msg.has_attachments is False

    def test_null_optional_fields_become_none(self) -> None:
        msg = _parse_message(SAMPLE_MESSAGE)
        assert msg.reply_to_guid is None
        assert msg.destination_caller_id is None

    def test_reply_to_guid_populated(self) -> None:
        msg = _parse_message({**SAMPLE_MESSAGE, "reply_to_guid": "PARENT-GUID"})
        assert msg.reply_to_guid == "PARENT-GUID"

    def test_date_is_utc(self) -> None:
        msg = _parse_message(SAMPLE_MESSAGE)
        assert msg.date.tzinfo is not None
        assert msg.date.year == 2026
        assert msg.date.month == 4

    def test_has_attachments_true_when_present(self) -> None:
        data = {
            **SAMPLE_MESSAGE,
            "attachments": [{
                "filename": "~/Library/Messages/Attachments/abc.jpg",
                "transfer_name": "photo.jpg",
                "uti": "public.jpeg",
                "mime_type": "image/jpeg",
                "total_bytes": 204800,
                "is_sticker": False,
                "original_path": "/Users/debbie/Library/Messages/Attachments/abc.jpg",
                "missing": False,
            }],
        }
        msg = _parse_message(data)
        assert msg.has_attachments is True
        assert len(msg.attachments) == 1
        assert msg.attachments[0].mime_type == "image/jpeg"


class TestParseChat:
    def test_basic_fields(self) -> None:
        chat = _parse_chat(SAMPLE_CHAT)
        assert chat.id == 7
        assert chat.name == "Alex"
        assert chat.service == "iMessage"
        assert chat.participants == ["+14155550101"]

    def test_last_message_at_parsed(self) -> None:
        chat = _parse_chat(SAMPLE_CHAT)
        assert chat.last_message_at.year == 2026


# ---------------------------------------------------------------------------
# Async client tests with mocked subprocess
# ---------------------------------------------------------------------------


class MockIMsgProcess:
    """
    A write-triggered test double for the imsg rpc subprocess.

    Unlike a pre-loaded response list, this mock only delivers a response when
    the corresponding request arrives on stdin. That eliminates the race condition
    where the reader consumes a response before the caller registers its future.

    Usage:
        process = MockIMsgProcess()
        process.add_handler(my_handler)   # called on every stdin.write
        client._process = process
    """

    def __init__(self) -> None:
        self.pid = 12345
        self._outbox: asyncio.Queue[bytes] = asyncio.Queue()
        self._handlers: list[Any] = []
        self.stdin = self._Stdin(self)
        self.stdout = self._Stdout(self)

    def add_handler(self, fn: Any) -> None:
        self._handlers.append(fn)

    def send(self, obj: dict[str, Any]) -> None:
        """Enqueue a JSON-RPC object to be returned by the next readline()."""
        self._outbox.put_nowait(json.dumps(obj).encode() + b"\n")

    class _Stdin:
        def __init__(self, proc: MockIMsgProcess) -> None:
            self._proc = proc

        def write(self, data: bytes) -> None:
            try:
                req = json.loads(data.decode().strip())
                for handler in self._proc._handlers:
                    handler(req)
            except Exception:
                pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

    class _Stdout:
        def __init__(self, proc: MockIMsgProcess) -> None:
            self._proc = proc

        async def readline(self) -> bytes:
            return await self._proc._outbox.get()


def _make_client_with_responses(
    *responses: dict[str, Any],
) -> tuple[IMsgRPCClient, MagicMock]:
    """
    Build a client whose mock stdout returns `responses` as JSON lines then EOF.
    Suitable for single-request tests only — for sequential or subscription tests,
    use MockIMsgProcess directly to avoid read-ahead race conditions.
    """
    client = IMsgRPCClient(Path("/fake/imsg"), timeout=2.0)

    lines = [json.dumps(r).encode() + b"\n" for r in responses] + [b""]
    idx = [0]

    async def readline() -> bytes:
        if idx[0] >= len(lines):
            return b""
        line = lines[idx[0]]
        idx[0] += 1
        await asyncio.sleep(0)  # yield so the caller's future can be registered
        return line

    mock_stdout = MagicMock()
    mock_stdout.readline = readline

    mock_stdin = AsyncMock()
    mock_process = MagicMock()
    mock_process.stdin = mock_stdin
    mock_process.stdout = mock_stdout
    mock_process.pid = 9999

    client._process = mock_process
    return client, mock_process


def _simple_client(
    method: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> IMsgRPCClient:
    """Create a client backed by MockIMsgProcess that returns a fixed result/error for `method`."""
    process = MockIMsgProcess()

    def handler(req: dict[str, Any]) -> None:
        if req.get("method") == method:
            if error is not None:
                process.send({"jsonrpc": "2.0", "id": req.get("id"), "error": error})
            else:
                process.send({"jsonrpc": "2.0", "id": req.get("id"), "result": result or {}})

    process.add_handler(handler)
    client = IMsgRPCClient(Path("/fake/imsg"), timeout=2.0)
    client._process = process  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._read_loop())
    return client


@pytest.mark.asyncio
async def test_list_chats_parses_response() -> None:
    client = _simple_client("chats.list", result={"chats": [SAMPLE_CHAT]})
    chats = await client.list_chats(limit=1)
    assert len(chats) == 1
    assert chats[0].id == 7
    assert chats[0].name == "Alex"
    client._reader_task.cancel()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_get_history_parses_messages() -> None:
    client = _simple_client("messages.history", result={"messages": [SAMPLE_MESSAGE]})
    messages = await client.get_history(chat_id=7, limit=1)
    assert len(messages) == 1
    assert messages[0].rowid == 12345
    assert messages[0].text == "Hey, are we still on for Thursday?"
    client._reader_task.cancel()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_raises_imsgrpcerror_on_error_response() -> None:
    client = _simple_client(
        "chats.list", error={"code": -32600, "message": "Invalid request"}
    )
    with pytest.raises(IMsgRPCError) as exc_info:
        await client.list_chats()
    assert exc_info.value.code == -32600
    assert "Invalid request" in exc_info.value.message
    client._reader_task.cancel()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_multiple_sequential_requests() -> None:
    """Two back-to-back requests should each get their own response via distinct IDs."""
    process = MockIMsgProcess()

    def handler(req: dict[str, Any]) -> None:
        method, req_id = req.get("method"), req.get("id")
        if method == "chats.list":
            process.send({"jsonrpc": "2.0", "id": req_id, "result": {"chats": [SAMPLE_CHAT]}})
        elif method == "messages.history":
            process.send(
                {"jsonrpc": "2.0", "id": req_id, "result": {"messages": [SAMPLE_MESSAGE]}}
            )

    process.add_handler(handler)
    client = IMsgRPCClient(Path("/fake/imsg"), timeout=2.0)
    client._process = process  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._read_loop())

    chats = await client.list_chats()
    messages = await client.get_history(chat_id=7)

    assert len(chats) == 1 and chats[0].name == "Alex"
    assert len(messages) == 1 and messages[0].rowid == 12345
    client._reader_task.cancel()


@pytest.mark.asyncio
async def test_subscribe_yields_messages_from_notifications() -> None:
    """subscribe() should yield Messages delivered as push notifications."""
    process = MockIMsgProcess()

    def handler(req: dict[str, Any]) -> None:
        method, req_id = req.get("method"), req.get("id")
        if method == "watch.subscribe":
            process.send({"jsonrpc": "2.0", "id": req_id, "result": {"subscription": 42}})
            # Deliver the notification after sleep(0) so subscribe() can register
            # its queue between processing the subscribe_response and the notification.
            async def _notify() -> None:
                await asyncio.sleep(0)
                process.send(
                    {
                        "jsonrpc": "2.0",
                        "method": "message",
                        "params": {"subscription": 42, "message": SAMPLE_MESSAGE},
                    }
                )

            asyncio.create_task(_notify())
        elif method == "watch.unsubscribe":
            process.send({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})

    process.add_handler(handler)
    client = IMsgRPCClient(Path("/fake/imsg"), timeout=2.0)
    client._process = process  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._read_loop())

    received = []
    async for msg in client.subscribe(since_rowid=0):
        received.append(msg)
        break  # stop after first message

    assert len(received) == 1
    assert received[0].rowid == 12345
    client._reader_task.cancel()


@pytest.mark.asyncio
async def test_connection_error_propagates_to_pending_request() -> None:
    """If the process ends with a pending request, IMsgRPCConnectionError is raised."""
    # MockIMsgProcess that immediately returns EOF on readline
    process = MockIMsgProcess()
    process._outbox.put_nowait(b"")  # immediate EOF
    client = IMsgRPCClient(Path("/fake/imsg"), timeout=2.0)
    client._process = process  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._read_loop())

    with pytest.raises(IMsgRPCConnectionError):
        await client.list_chats()
