"""
web_app.py - Local operator web GUI for imsg-agent.

The web app is a localhost-only surface over the existing archive and Markdown
approval artifacts. It never sends messages directly; approval queues a draft by
moving it through the same outbox artifact path used by the rest of the system.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NoReturn, cast
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv

from .config import Config, load_config
from .manage_cli import archive_db_path
from .operator_service import JSON, JSONList, OperatorService, OperatorServiceError

WebAPIError = OperatorServiceError
WebService = OperatorService

__all__ = ["WebAPIError", "WebService"]


class IMsgWebServer(ThreadingHTTPServer):
    service: WebService
    static_dir: Path


class IMsgWebHandler(BaseHTTPRequestHandler):
    server_version = "imsg-agent-web/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/"):
                self._send_json(self._route_get(parsed.path, parse_qs(parsed.query)))
                return
            self._serve_static(parsed.path)
        except WebAPIError as exc:
            self._send_error(exc.status, exc.message)
        except Exception as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            self._send_json(self._route_post(parsed.path, payload))
        except WebAPIError as exc:
            self._send_error(exc.status, exc.message)
        except Exception as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _route_get(self, path: str, query: dict[str, list[str]]) -> JSON | JSONList:
        service = cast(IMsgWebServer, self.server).service
        parts = _path_parts(path)
        if parts == ["api", "status"]:
            return service.status()
        if parts == ["api", "overview"]:
            return service.overview(limit=_query_int(query, "limit", 12))
        if parts == ["api", "orbit"]:
            days = _query_optional_int(query, "days")
            return service.orbit(
                limit=_query_int(query, "limit", 16),
                offset=_query_optional_int(query, "offset") or 0,
                direction=_query_str(query, "direction", "incoming"),
                days=7 if days is None else days,
                include_spam=_query_optional_bool(query, "include_spam") or False,
            )
        if parts == ["api", "changes"]:
            return service.changes(since=_query_str(query, "since", ""))
        if parts == ["api", "operator"]:
            return service.operator_profile()
        if parts == ["api", "preferences"]:
            return service.observatory_preferences()
        if parts == ["api", "contacts"]:
            return service.contacts(
                limit=_query_int(query, "limit", 50),
                query=_query_str(query, "q", ""),
            )
        if parts == ["api", "contacts", "page"]:
            return service.contacts_page(
                limit=_query_int(query, "limit", 50),
                offset=_query_optional_int(query, "offset") or 0,
                query=_query_str(query, "q", ""),
            )
        if len(parts) == 3 and parts[:2] == ["api", "contacts"]:
            return service.contact(parts[2])
        if parts == ["api", "pending"]:
            return service.pending(
                limit=_query_int(query, "limit", 20),
                days=_query_optional_int(query, "days"),
                relationships=_query_csv(query, "relationships"),
                include_archived=_query_optional_bool(query, "include_archived"),
            )
        if parts == ["api", "attention"]:
            return service.attention(limit=_query_int(query, "limit", 50))
        if parts == ["api", "recent"]:
            return service.recent(limit=_query_int(query, "limit", 50))
        if parts == ["api", "needs-reply"]:
            return service.needs_reply(limit=_query_int(query, "limit", 50))
        if parts == ["api", "issues"]:
            return service.issues(limit=_query_int(query, "limit", 50))
        if parts == ["api", "views"]:
            return service.saved_views(limit=_query_int(query, "limit", 30))
        if len(parts) == 4 and parts[:2] == ["api", "logs"]:
            return service.logs(
                parts[2],
                errors=parts[3] == "errors",
                lines=_query_int(query, "lines", 80),
            )
        if parts == ["api", "search"]:
            return service.search(
                _query_str(query, "q", ""),
                limit=_query_int(query, "limit", 50),
                chat_id=_query_optional_int(query, "chat_id"),
                since=_query_optional_str(query, "since"),
                until=_query_optional_str(query, "until"),
            )
        if len(parts) == 4 and parts[:3] == ["api", "chats"] and parts[3] == "messages":
            raise WebAPIError(HTTPStatus.BAD_REQUEST, "Missing chat id")
        if len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "messages":
            return service.chat(
                int(parts[2]),
                limit=_query_int(query, "limit", 80),
                before=_query_optional_str(query, "before"),
                before_rowid=_query_optional_int(query, "before_rowid"),
            )
        raise WebAPIError(HTTPStatus.NOT_FOUND, "Not found")

    def _route_post(self, path: str, payload: JSON) -> JSON:
        service = cast(IMsgWebServer, self.server).service
        parts = _path_parts(path)
        if len(parts) == 4 and parts[:2] == ["api", "drafts"]:
            uuid = parts[2]
            action = parts[3]
            if action == "approve":
                return service.approve_draft(uuid, text=_optional_payload_str(payload, "text"))
            if action == "edit":
                return service.edit_draft(uuid, text=_payload_str(payload, "text"))
            if action == "discard":
                return service.discard_draft(uuid)
            if action == "archive":
                return service.archive_draft(
                    uuid,
                    reason=_optional_payload_str(payload, "reason") or "Archived by operator.",
                )
            if action == "reject":
                return service.reject_draft(
                    uuid,
                    reasoning=_optional_payload_str(payload, "reasoning")
                    or "Operator rejected the draft.",
                )
        if parts == ["api", "drafts", "request"]:
            return service.request_draft(
                chat_id=_payload_optional_int(payload, "chat_id"),
                message_rowid=_payload_optional_int(payload, "message_rowid"),
            )
        if parts == ["api", "no-reply"]:
            return service.mark_no_reply(
                chat_id=_payload_int(payload, "chat_id"),
                source_rowid=_payload_int(payload, "source_rowid"),
                reasoning=_payload_str(payload, "reasoning"),
                model=_optional_payload_str(payload, "model"),
            )
        if parts == ["api", "operator"]:
            return service.update_operator_profile(_payload_dict(payload, "fields"))
        if parts == ["api", "preferences"]:
            return service.update_observatory_preferences(_payload_dict(payload, "fields"))
        if parts == ["api", "contacts", "review"]:
            return service.review_contact(
                chat_id=_payload_int(payload, "chat_id"),
                decision=_payload_str(payload, "decision"),
                notes=_optional_payload_str(payload, "notes") or "",
            )
        if parts == ["api", "contacts", "sync"]:
            return service.sync_contacts()
        if parts == ["api", "contacts", "research"]:
            return service.research_business_contact(
                name=_payload_str(payload, "name"),
                location=_payload_str(payload, "location"),
            )
        if parts == ["api", "contacts", "link"]:
            return service.link_contact(
                chat_id=_payload_int(payload, "chat_id"),
                contact_id=_payload_str(payload, "contact_id"),
            )
        if parts == ["api", "contacts", "unlink"]:
            return service.unlink_contact(
                chat_id=_payload_int(payload, "chat_id"),
                contact_id=_payload_str(payload, "contact_id"),
            )
        if parts == ["api", "contacts", "create"]:
            return service.create_contact(_payload_dict(payload, "fields"))
        if len(parts) == 4 and parts[:2] == ["api", "contacts"] and parts[3] == "update":
            return service.update_contact(parts[2], _payload_dict(payload, "fields"))
        if len(parts) == 4 and parts[:2] == ["api", "contacts"] and parts[3] == "importance":
            return service.update_contact_importance(parts[2], _payload_int(payload, "importance"))
        if len(parts) == 4 and parts[:2] == ["api", "contacts"] and parts[3] == "delete":
            return service.delete_contact(parts[2])
        if len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "context":
            return service.update_chat_context(
                int(parts[2]),
                fields=_payload_dict(payload, "fields"),
                notes=_optional_payload_str(payload, "notes"),
            )
        if len(parts) == 4 and parts[:2] == ["api", "services"]:
            return service.service_action(parts[2], parts[3])
        raise WebAPIError(HTTPStatus.NOT_FOUND, "Not found")

    def _serve_static(self, path: str) -> None:
        server = cast(IMsgWebServer, self.server)
        relative = "index.html" if path in {"", "/"} else unquote(path).lstrip("/")
        candidate = (server.static_dir / relative).resolve()
        static_root = server.static_dir.resolve()
        if static_root != candidate and static_root not in candidate.parents:
            self._send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not candidate.exists() or not candidate.is_file():
            candidate = static_root / "index.html"
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        body = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> JSON:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WebAPIError(HTTPStatus.BAD_REQUEST, "Invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise WebAPIError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return cast(JSON, payload)

    def _send_json(self, payload: JSON | JSONList, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message, "status": int(status)}, status=status)


def _path_parts(path: str) -> list[str]:
    return [unquote(part) for part in path.split("/") if part]


def _query_str(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    return values[0] if values else default


def _query_optional_str(query: dict[str, list[str]], key: str) -> str | None:
    value = _query_str(query, key, "")
    return value or None


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _query_str(query, key, str(default))
    try:
        return max(1, min(500, int(raw)))
    except ValueError:
        raise WebAPIError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer") from None


def _query_optional_int(query: dict[str, list[str]], key: str) -> int | None:
    raw = _query_str(query, key, "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise WebAPIError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer") from None


def _query_csv(query: dict[str, list[str]], key: str) -> list[str] | None:
    value = _query_optional_str(query, key)
    return [item.strip() for item in value.split(",") if item.strip()] if value else None


def _query_optional_bool(query: dict[str, list[str]], key: str) -> bool | None:
    value = _query_optional_str(query, key)
    if value is None:
        return None
    if value.lower() in {"1", "true", "yes"}:
        return True
    if value.lower() in {"0", "false", "no"}:
        return False
    raise WebAPIError(HTTPStatus.BAD_REQUEST, f"{key} must be a boolean")


def _payload_str(payload: JSON, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise WebAPIError(HTTPStatus.BAD_REQUEST, f"{key} is required")
    return value


def _optional_payload_str(payload: JSON, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise WebAPIError(HTTPStatus.BAD_REQUEST, f"{key} must be a string")
    return value


def _payload_optional_int(payload: JSON, key: str) -> int | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, int):
        raise WebAPIError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer")
    return value


def _payload_int(payload: JSON, key: str) -> int:
    value = _payload_optional_int(payload, key)
    if value is None:
        raise WebAPIError(HTTPStatus.BAD_REQUEST, f"{key} is required")
    return value


def _payload_dict(payload: JSON, key: str) -> JSON:
    value = payload.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WebAPIError(HTTPStatus.BAD_REQUEST, f"{key} must be an object")
    return cast(JSON, value)


def serve(
    *,
    config: Config,
    host: str,
    port: int,
    data_dir: Path,
    db_path: Path,
    static_dir: Path,
) -> NoReturn:
    server = IMsgWebServer((host, port), IMsgWebHandler)
    server.service = WebService(config=config, data_dir=data_dir, db_path=db_path)
    server.static_dir = static_dir
    print(f"imsg-agent web listening on http://{host}:{port}")
    server.serve_forever()
    raise AssertionError("unreachable")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local imsg-agent web GUI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", help="SQLite DB path. Defaults to ~/imsg-data/imessage.sqlite")
    parser.add_argument("--data-dir", help="Data dir. Defaults to configured ~/imsg-data")
    parser.add_argument(
        "--static-dir",
        help="Static asset directory. Defaults to this repo's web/ directory",
    )
    return parser


def cli() -> None:
    load_dotenv()
    args = _parser().parse_args()
    config = load_config()
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else config.data_dir
    db_path = Path(args.db).expanduser() if args.db else archive_db_path(config)
    static_dir = (
        Path(args.static_dir).expanduser()
        if args.static_dir
        else Path(__file__).parent.parent / "web"
    )
    serve(
        config=config,
        host=str(args.host),
        port=int(args.port),
        data_dir=data_dir,
        db_path=db_path,
        static_dir=static_dir,
    )


if __name__ == "__main__":
    cli()
