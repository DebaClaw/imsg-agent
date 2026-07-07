"""
web_app.py - Local operator web GUI for imsg-agent.

The web app is a localhost-only surface over the existing archive and Markdown
approval artifacts. It never sends messages directly; approval queues a draft by
moving it through the same outbox artifact path used by the rest of the system.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NoReturn, cast
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv

from .archive_agent import ArchiveAgentWorker
from .archive_store import ArchiveRow, IMessageArchive
from .config import Config, load_config
from .drafter import Drafter, OpenAIResponsesDraftingClient
from .manage_cli import archive_db_path, service_status
from .models import Draft, Message
from .pending_report import _decorate_pending_row, pending_replies
from .store import MessageStore

JSON = dict[str, Any]
JSONList = list[JSON]


class WebAPIError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class WebService:
    def __init__(self, *, config: Config, data_dir: Path, db_path: Path) -> None:
        self.config = config
        self.data_dir = Path(data_dir).expanduser()
        self.db_path = Path(db_path).expanduser()

    def status(self) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            stats = archive.archive_stats()
        return {
            "data_dir": str(self.data_dir),
            "db_path": str(self.db_path),
            "archive": stats,
            "services": [service_status("monitor"), service_status("worker")],
        }

    def overview(self, *, limit: int = 12) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            store = MessageStore(self.data_dir)
            return {
                "status": self.status(),
                "attention": archive.attention_items(limit=limit),
                "pending": pending_replies(
                    archive,
                    store,
                    limit=limit,
                    max_missing_age_hours=self.config.max_inbox_age_hours,
                ),
                "recent": archive.recent_chats(limit=limit),
            }

    def pending(self, *, limit: int = 20) -> JSONList:
        with IMessageArchive(self.db_path) as archive:
            return pending_replies(
                archive,
                MessageStore(self.data_dir),
                limit=limit,
                max_missing_age_hours=self.config.max_inbox_age_hours,
            )

    def attention(self, *, limit: int = 50) -> JSONList:
        with IMessageArchive(self.db_path) as archive:
            return archive.attention_items(limit=limit)

    def recent(self, *, limit: int = 50) -> JSONList:
        with IMessageArchive(self.db_path) as archive:
            return archive.recent_chats(limit=limit)

    def needs_reply(self, *, limit: int = 50) -> JSONList:
        with IMessageArchive(self.db_path) as archive:
            return archive.needs_reply(limit=limit)

    def issues(self, *, limit: int = 50) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            return {
                "unresolved_contacts": archive.unresolved_contact_chats(limit=limit),
                "attachment_issues": archive.attachment_issues(limit=limit),
            }

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        chat_id: int | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> JSONList:
        if not query.strip():
            return []
        with IMessageArchive(self.db_path) as archive:
            return archive.search_messages(
                query,
                limit=limit,
                chat_id=chat_id,
                since=since,
                until=until,
            )

    def chat(self, chat_id: int, *, limit: int = 80) -> JSON:
        with IMessageArchive(self.db_path) as archive:
            messages = list(reversed(archive.chat_messages(chat_id, limit=limit)))
            seed = archive.chat_context_seed(chat_id)
        context, notes = MessageStore(self.data_dir).read_chat_context_document(chat_id)
        return {
            "chat": seed,
            "context": context,
            "notes": notes,
            "messages": messages,
        }

    def approve_draft(self, uuid: str, *, text: str | None = None) -> JSON:
        store = MessageStore(self.data_dir)
        path = self._draft_path(uuid)
        draft = self._read_draft(store, path)
        if text is not None:
            draft.proposed_text = text.strip()
        draft.approved = True
        store.move_draft_to_outbox(draft)
        context = store.read_chat_context(draft.chat_id)
        return {
            "status": "queued",
            "draft_uuid": draft.uuid,
            "chat_id": draft.chat_id,
            "outbox_path": str(self.data_dir / "outbox" / f"{draft.uuid}.md"),
            "professional": context.get("professional"),
            "sent": False,
        }

    def edit_draft(self, uuid: str, *, text: str) -> JSON:
        if not text.strip():
            raise WebAPIError(HTTPStatus.BAD_REQUEST, "Draft text cannot be empty")
        store = MessageStore(self.data_dir)
        path = self._draft_path(uuid)
        draft = self._read_draft(store, path)
        draft.proposed_text = text.strip()
        draft.approved = False
        store.write_draft(draft)
        return {
            "status": "edited",
            "draft": self._draft_payload(draft, path),
        }

    def discard_draft(self, uuid: str) -> JSON:
        path = self._draft_path(uuid)
        path.unlink()
        return {
            "status": "discarded",
            "draft_uuid": uuid,
        }

    def request_draft(
        self,
        *,
        chat_id: int | None = None,
        message_rowid: int | None = None,
    ) -> JSON:
        if not self.config.openai_api_key:
            raise WebAPIError(HTTPStatus.BAD_REQUEST, "OPENAI_API_KEY is not set")
        if chat_id is None and message_rowid is None:
            raise WebAPIError(HTTPStatus.BAD_REQUEST, "chat_id or message_rowid is required")
        with IMessageArchive(self.db_path) as archive:
            message = self._draft_target_message(
                archive,
                chat_id=chat_id,
                message_rowid=message_rowid,
            )
            if message is None:
                raise WebAPIError(HTTPStatus.NOT_FOUND, "No target message found")
            if chat_id is not None and (message.is_from_me or message.is_reaction):
                raise WebAPIError(
                    HTTPStatus.CONFLICT,
                    "Latest message is not an inbound reply target",
                )
            store = MessageStore(self.data_dir)
            drafter = Drafter(
                store,
                OpenAIResponsesDraftingClient(api_key=self.config.openai_api_key),
                default_model=self.config.draft_model,
                max_inbox_age_hours=0,
                auto_approve_default=self.config.auto_approve,
            )
            worker = ArchiveAgentWorker(
                archive=archive,
                store=store,
                drafter=drafter,
                history_limit=self.config.chat_context_messages,
            )
            draft = asyncio.run(worker.draft_archived_message(message))
            return self._draft_result(store, message=message, draft=draft)

    def _draft_target_message(
        self,
        archive: IMessageArchive,
        *,
        chat_id: int | None,
        message_rowid: int | None,
    ) -> Message | None:
        if message_rowid is not None:
            return archive.message_by_rowid(message_rowid)
        if chat_id is not None:
            return archive.latest_message_for_chat(chat_id)
        return None

    def _draft_result(
        self,
        store: MessageStore,
        *,
        message: Message,
        draft: Draft | None,
    ) -> JSON:
        if draft is not None:
            path = store.data_dir / "chats" / str(draft.chat_id) / "drafts" / f"{draft.uuid}.md"
            return {
                "status": "draft_created",
                "chat_id": message.chat_id,
                "message_rowid": message.rowid,
                "draft": self._draft_payload(draft, path),
            }
        artifact = self._reply_artifact_for_source(
            store,
            chat_id=message.chat_id,
            source_rowid=message.rowid,
        )
        if artifact is not None:
            return {
                "status": str(artifact.get("draft_status") or "handled"),
                "chat_id": message.chat_id,
                "message_rowid": message.rowid,
                "artifact": artifact,
            }
        return {
            "status": "skipped",
            "chat_id": message.chat_id,
            "message_rowid": message.rowid,
        }

    def _reply_artifact_for_source(
        self,
        store: MessageStore,
        *,
        chat_id: int,
        source_rowid: int,
    ) -> ArchiveRow | None:
        decorated = _decorate_pending_row(
            {
                "chat_id": chat_id,
                "message_rowid": source_rowid,
            },
            store,
        )
        if decorated.get("draft_status") == "missing":
            return None
        return decorated

    def _draft_path(self, uuid: str) -> Path:
        if "/" in uuid or "\\" in uuid or uuid in {"", ".", ".."}:
            raise WebAPIError(HTTPStatus.BAD_REQUEST, "Invalid draft uuid")
        matches = sorted((self.data_dir / "chats").glob(f"*/drafts/{uuid}.md"))
        if not matches:
            raise WebAPIError(HTTPStatus.NOT_FOUND, "Draft not found")
        return matches[0]

    def _read_draft(self, store: MessageStore, path: Path) -> Draft:
        draft = store.read_draft(path)
        if draft is None:
            raise WebAPIError(HTTPStatus.UNPROCESSABLE_ENTITY, "Draft could not be parsed")
        return draft

    def _draft_payload(self, draft: Draft, path: Path) -> JSON:
        payload = asdict(draft)
        payload["created_at"] = draft.created_at.isoformat()
        payload["path"] = str(path)
        return payload


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
        if parts == ["api", "pending"]:
            return service.pending(limit=_query_int(query, "limit", 20))
        if parts == ["api", "attention"]:
            return service.attention(limit=_query_int(query, "limit", 50))
        if parts == ["api", "recent"]:
            return service.recent(limit=_query_int(query, "limit", 50))
        if parts == ["api", "needs-reply"]:
            return service.needs_reply(limit=_query_int(query, "limit", 50))
        if parts == ["api", "issues"]:
            return service.issues(limit=_query_int(query, "limit", 50))
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
            return service.chat(int(parts[2]), limit=_query_int(query, "limit", 80))
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
        if parts == ["api", "drafts", "request"]:
            return service.request_draft(
                chat_id=_payload_optional_int(payload, "chat_id"),
                message_rowid=_payload_optional_int(payload, "message_rowid"),
            )
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
