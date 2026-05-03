from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast

from agent.archive_store import IMessageArchive
from agent.mcp_server import IMsgMCPServer, _read_message, _write_message
from agent.models import Chat, Draft, Message
from agent.store import MessageStore

NOW = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)


def _chat() -> Chat:
    return Chat(
        id=7,
        identifier="+18016022838",
        name="Alex",
        service="iMessage",
        last_message_at=NOW,
        participants=["+18016022838"],
    )


def _message(rowid: int = 100) -> Message:
    return Message(
        rowid=rowid,
        chat_id=7,
        guid=f"GUID-{rowid}",
        sender="+18016022838",
        text="Coffee this afternoon?",
        date=NOW,
        is_from_me=False,
        service="iMessage",
        has_attachments=False,
    )


def _tool_text(response: dict[str, object]) -> list[dict[str, Any]]:
    result = response["result"]
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list)
    first = content[0]
    assert isinstance(first, dict)
    return cast(list[dict[str, Any]], json.loads(str(first["text"])))


class _FakePipe:
    def __init__(self, initial: bytes = b"") -> None:
        self.buffer = BytesIO(initial)


def test_mcp_initialize_and_lists_tools(tmp_path: Path) -> None:
    server = IMsgMCPServer(tmp_path / "imessage.sqlite")

    init = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert init is not None
    init_result = cast(dict[str, Any], init["result"])
    assert init_result["serverInfo"]["name"] == "imsg-agent"
    assert init_result["protocolVersion"] == "2025-06-18"
    assert tools is not None
    tools_result = cast(dict[str, Any], tools["result"])
    names = {tool["name"] for tool in tools_result["tools"]}
    assert "recent_chats" in names
    assert "pending_replies" in names
    assert "search_messages" in names
    assert "get_chat_messages" in names


def test_mcp_empty_resources_and_prompts_lists(tmp_path: Path) -> None:
    server = IMsgMCPServer(tmp_path / "imessage.sqlite")

    resources = server.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    prompts = server.handle({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})

    assert resources is not None
    assert resources["result"] == {"resources": []}
    assert prompts is not None
    assert prompts["result"] == {"prompts": []}


def test_mcp_recent_and_search_tools_read_archive(tmp_path: Path) -> None:
    db_path = tmp_path / "imessage.sqlite"
    archive = IMessageArchive(db_path)
    archive.upsert_chat(_chat())
    archive.upsert_message(_message())
    archive.close()
    server = IMsgMCPServer(db_path)

    recent = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "recent_chats", "arguments": {"limit": 1}},
        }
    )
    search = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "search_messages", "arguments": {"query": "coffee"}},
        }
    )
    messages = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_chat_messages", "arguments": {"chat_id": 7}},
        }
    )

    assert recent is not None
    assert _tool_text(recent)[0]["chat_id"] == 7
    assert search is not None
    assert _tool_text(search)[0]["message_rowid"] == 100
    assert messages is not None
    assert _tool_text(messages)[0]["text"] == "Coffee this afternoon?"


def test_mcp_pending_replies_reads_archive_and_drafts(tmp_path: Path) -> None:
    db_path = tmp_path / "imessage.sqlite"
    archive = IMessageArchive(db_path)
    archive.upsert_chat(_chat())
    archive.upsert_message(_message())
    archive.close()
    store = MessageStore(tmp_path)
    store.write_draft(
        Draft(
            uuid="draft-1",
            chat_id=7,
            target_identifier="iMessage;-;+14155550101",
            created_at=NOW,
            proposed_text="Sounds good, coffee works.",
            reasoning="They suggested coffee.",
            prompt_version="v1",
            source_rowid=100,
        )
    )
    server = IMsgMCPServer(db_path, tmp_path)

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "pending_replies", "arguments": {"limit": 1}},
        }
    )

    assert response is not None
    rows = _tool_text(response)
    assert rows[0]["message_rowid"] == 100
    assert rows[0]["draft_status"] == "draft_unapproved"
    assert rows[0]["proposed_text"] == "Sounds good, coffee works."


def test_mcp_tool_errors_are_tool_results(tmp_path: Path) -> None:
    server = IMsgMCPServer(tmp_path / "imessage.sqlite")

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_messages", "arguments": {}},
        }
    )

    assert response is not None
    result = cast(dict[str, Any], response["result"])
    assert result["isError"] is True


def test_mcp_stdio_uses_json_lines() -> None:
    incoming = _FakePipe(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    outgoing = _FakePipe()

    message = _read_message(incoming)
    _write_message(outgoing, {"jsonrpc": "2.0", "id": 1, "result": {}})

    assert message == {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert outgoing.buffer.getvalue() == b'{"jsonrpc":"2.0","id":1,"result":{}}\n'
