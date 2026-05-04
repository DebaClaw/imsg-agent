"""
mcp_server.py - Read-only MCP server for the local iMessage archive.

This server intentionally exposes archive visibility tools only. It does not send
messages, approve drafts, or mutate the archive.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

from .archive_store import IMessageArchive
from .config import Config, load_config
from .pending_report import pending_replies
from .store import MessageStore

logger = logging.getLogger(__name__)
JSON = dict[str, Any]
SUPPORTED_PROTOCOL_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}
DEFAULT_PROTOCOL_VERSION = "2025-06-18"


def archive_db_path(config: Config) -> Path:
    return config.data_dir / "imessage.sqlite"


def _limit(value: object, default: int, maximum: int = 200) -> int:
    try:
        parsed = int(value) if isinstance(value, int | str | bytes | bytearray) else default
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _text_result(value: object) -> JSON:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, indent=2, sort_keys=True),
            }
        ]
    }


def _error_result(message: str) -> JSON:
    return {
        "isError": True,
        "content": [{"type": "text", "text": message}],
    }


def _tool(
    name: str,
    description: str,
    properties: JSON | None = None,
    required: list[str] | None = None,
) -> JSON:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOLS: list[JSON] = [
    _tool("archive_stats", "Show archive totals and health counts."),
    _tool(
        "recent_chats",
        "List recently active chats with contact names and latest message preview.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}},
    ),
    _tool(
        "attention",
        "Rank inbound chats that likely need attention without using AI.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25}},
    ),
    _tool(
        "needs_reply",
        "List chats where the latest archived message is inbound.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
    ),
    _tool(
        "pending_replies",
        "List latest-inbound chats with matching proposed replies when present.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 5}},
    ),
    _tool(
        "search_messages",
        "Full-text search archived messages.",
        {
            "query": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            "chat_id": {"type": "integer"},
            "since": {"type": "string", "description": "Inclusive ISO date/time lower bound."},
            "until": {"type": "string", "description": "Exclusive ISO date/time upper bound."},
        },
        ["query"],
    ),
    _tool(
        "get_chat_messages",
        "Fetch recent archived messages for one chat.",
        {
            "chat_id": {"type": "integer"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            "before": {"type": "string", "description": "Exclusive ISO date/time upper bound."},
            "after": {"type": "string", "description": "Inclusive ISO date/time lower bound."},
        },
        ["chat_id"],
    ),
    _tool(
        "unresolved_contacts",
        "List archived chat identifiers that did not match Contacts.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
    ),
    _tool(
        "attachment_issues",
        "List archived attachments that are missing or were not copied locally.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
    ),
]


class IMsgMCPServer:
    def __init__(
        self,
        db_path: Path,
        data_dir: Path | None = None,
        max_missing_age_hours: int | None = None,
    ) -> None:
        self._db_path = db_path
        self._data_dir = data_dir or db_path.parent
        self._max_missing_age_hours = max_missing_age_hours

    def handle(self, message: JSON) -> JSON | None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if not method:
            return self._response(request_id, error={"code": -32600, "message": "Missing method"})
        if request_id is None:
            return None
        try:
            result = self._dispatch(method, message.get("params") or {})
        except Exception as exc:
            logger.exception("MCP request failed: %s", method)
            return self._response(
                request_id,
                error={"code": -32603, "message": str(exc) or exc.__class__.__name__},
            )
        return self._response(request_id, result=result)

    def _dispatch(self, method: str, params: object) -> JSON:
        if method == "initialize":
            protocol_version = DEFAULT_PROTOCOL_VERSION
            if isinstance(params, dict):
                requested_version = params.get("protocolVersion")
                if (
                    isinstance(requested_version, str)
                    and requested_version in SUPPORTED_PROTOCOL_VERSIONS
                ):
                    protocol_version = requested_version
            return {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "imsg-agent",
                    "title": "imsg-agent Archive",
                    "version": "0.1.0",
                },
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "resources/list":
            return {"resources": []}
        if method == "prompts/list":
            return {"prompts": []}
        if method == "tools/call":
            if not isinstance(params, dict):
                return _error_result("tools/call params must be an object")
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _error_result("tool arguments must be an object")
            return self._call_tool(name, arguments)
        raise ValueError(f"Unsupported MCP method: {method}")

    def _call_tool(self, name: str, arguments: JSON) -> JSON:
        with IMessageArchive(self._db_path) as archive:
            if name == "archive_stats":
                return _text_result(archive.archive_stats())
            if name == "recent_chats":
                return _text_result(archive.recent_chats(limit=_limit(arguments.get("limit"), 20)))
            if name == "attention":
                return _text_result(
                    archive.attention_items(limit=_limit(arguments.get("limit"), 25))
                )
            if name == "needs_reply":
                return _text_result(archive.needs_reply(limit=_limit(arguments.get("limit"), 50)))
            if name == "pending_replies":
                return _text_result(
                    pending_replies(
                        archive,
                        MessageStore(self._data_dir),
                        limit=_limit(arguments.get("limit"), 5),
                        max_missing_age_hours=self._max_missing_age_hours,
                    )
                )
            if name == "search_messages":
                query = str(arguments.get("query") or "").strip()
                if not query:
                    return _error_result("query is required")
                chat_id_value = arguments.get("chat_id")
                chat_id = int(chat_id_value) if chat_id_value is not None else None
                return _text_result(
                    archive.search_messages(
                        query,
                        limit=_limit(arguments.get("limit"), 25),
                        chat_id=chat_id,
                        since=_optional_string(arguments.get("since")),
                        until=_optional_string(arguments.get("until")),
                    )
                )
            if name == "get_chat_messages":
                chat_id_value = arguments.get("chat_id")
                if chat_id_value is None:
                    return _error_result("chat_id is required")
                return _text_result(
                    archive.chat_messages(
                        int(chat_id_value),
                        limit=_limit(arguments.get("limit"), 50),
                        before=_optional_string(arguments.get("before")),
                        after=_optional_string(arguments.get("after")),
                    )
                )
            if name == "unresolved_contacts":
                return _text_result(
                    archive.unresolved_contact_chats(limit=_limit(arguments.get("limit"), 50))
                )
            if name == "attachment_issues":
                return _text_result(
                    archive.attachment_issues(limit=_limit(arguments.get("limit"), 50))
                )
        return _error_result(f"Unknown tool: {name}")

    @staticmethod
    def _response(
        request_id: object,
        *,
        result: JSON | None = None,
        error: JSON | None = None,
    ) -> JSON:
        response: JSON = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result or {}
        return response


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_message(stdin: Any) -> JSON | None:
    headers: dict[str, str] = {}
    line = stdin.buffer.readline()
    if line == b"":
        return None
    if not line.strip():
        return _read_message(stdin)
    if not line.lower().startswith(b"content-length:"):
        return cast(JSON, json.loads(line.decode("utf-8")))
    key, _, value = line.decode("utf-8").partition(":")
    headers[key.strip().lower()] = value.strip()
    while True:
        line = stdin.buffer.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("utf-8").partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    payload = stdin.buffer.read(length)
    return cast(JSON, json.loads(payload.decode("utf-8")))


def _write_message(stdout: Any, message: JSON) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    stdout.buffer.write(payload + b"\n")
    stdout.buffer.flush()


def serve_stdio(server: IMsgMCPServer) -> None:
    while True:
        message = _read_message(sys.stdin)
        if message is None:
            return
        response = server.handle(message)
        if response is not None:
            _write_message(sys.stdout, response)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the imsg-agent read-only MCP server.")
    parser.add_argument("--db", help="Path to imessage.sqlite. Defaults to configured data dir.")
    return parser


def cli() -> None:
    load_dotenv()
    args = _parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "WARNING").upper(), logging.WARNING)
    )
    config = load_config()
    db_path = Path(args.db or archive_db_path(config)).expanduser()
    serve_stdio(IMsgMCPServer(db_path, config.data_dir, config.max_inbox_age_hours))


if __name__ == "__main__":
    cli()
