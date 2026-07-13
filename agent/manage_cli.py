"""
manage_cli.py - Operator CLI for imsg-agent.

This command is the human-facing control surface over the lower-level archive, worker,
and MCP commands. It does not send messages or approve drafts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .archive_agent import ArchiveAgentWorker
from .archive_store import ArchiveRow, IMessageArchive
from .config import Config, load_config
from .drafter import Drafter, OpenAIResponsesDraftingClient
from .models import Draft, Message
from .pending_report import _decorate_pending_row, pending_replies
from .store import MessageStore

JSON = dict[str, Any]
Column = tuple[str, str]
ARCHIVE_LABEL = "com.imsg-agent.archive-monitor"
WORKER_LABEL = "com.imsg-agent.worker"


def archive_db_path(config: Config) -> Path:
    return config.data_dir / "imessage.sqlite"


def run_status(args: argparse.Namespace) -> None:
    config = load_config()
    db_path = _db_path(args, config)
    data_dir = _data_dir(args, config)
    with IMessageArchive(db_path) as archive:
        payload: JSON = {
            "data_dir": str(data_dir),
            "db_path": str(db_path),
            "archive": archive.archive_stats(),
            "services": [service_status("monitor"), service_status("worker")],
        }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"data_dir: {payload['data_dir']}")
    print(f"db_path: {payload['db_path']}")
    print()
    print("archive")
    for key, value in payload["archive"].items():
        print(f"  {key}: {value}")
    print()
    print("services")
    _print_rows(
        payload["services"],
        [
            ("service", "service"),
            ("loaded", "loaded"),
            ("running", "running"),
            ("plist_exists", "plist"),
            ("detail", "detail"),
        ],
        json_output=False,
    )


def run_report(args: argparse.Namespace) -> None:
    config = load_config()
    db_path = _db_path(args, config)
    data_dir = _data_dir(args, config)
    with IMessageArchive(db_path) as archive:
        store = MessageStore(data_dir)
        payload: JSON = {
            "archive": archive.archive_stats(),
            "services": [service_status("monitor"), service_status("worker")],
            "pending": pending_replies(
                archive,
                store,
                limit=args.limit,
                max_missing_age_hours=config.max_inbox_age_hours,
            ),
            "unresolved_contacts": archive.unresolved_contact_chats(limit=args.issue_limit),
            "attachment_issues": archive.attachment_issues(limit=args.issue_limit),
        }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print("summary")
    for key, value in payload["archive"].items():
        print(f"  {key}: {value}")
    print()
    print("services")
    _print_rows(
        payload["services"],
        [
            ("service", "service"),
            ("loaded", "loaded"),
            ("running", "running"),
            ("detail", "detail"),
        ],
        json_output=False,
    )
    print()
    print("pending replies")
    _print_pending(payload["pending"])
    print()
    print(f"unresolved_contacts: {len(payload['unresolved_contacts'])}")
    print(f"attachment_issues: {len(payload['attachment_issues'])}")


def run_pending(args: argparse.Namespace) -> None:
    config = load_config()
    with IMessageArchive(_db_path(args, config)) as archive:
        rows = pending_replies(
            archive,
            MessageStore(_data_dir(args, config)),
            limit=args.limit,
            max_missing_age_hours=config.max_inbox_age_hours,
        )
    if args.json_output:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    _print_pending(rows)


def run_recent(args: argparse.Namespace) -> None:
    config = load_config()
    with IMessageArchive(_db_path(args, config)) as archive:
        rows = archive.recent_chats(limit=args.limit)
    _print_rows(
        rows,
        [
            ("chat_id", "chat"),
            ("last_message_at", "last_message_at"),
            ("messages", "messages"),
            ("name", "name"),
            ("contacts", "contacts"),
            ("last_text", "last_text"),
        ],
        json_output=args.json_output,
    )


def run_attention(args: argparse.Namespace) -> None:
    config = load_config()
    with IMessageArchive(_db_path(args, config)) as archive:
        rows = archive.attention_items(limit=args.limit)
    _print_rows(
        rows,
        [
            ("score", "score"),
            ("chat_id", "chat"),
            ("last_message_at", "last_message_at"),
            ("name", "name"),
            ("contacts", "contacts"),
            ("last_text", "last_text"),
            ("reason", "reason"),
        ],
        json_output=args.json_output,
    )


def run_needs_reply(args: argparse.Namespace) -> None:
    config = load_config()
    with IMessageArchive(_db_path(args, config)) as archive:
        rows = archive.needs_reply(limit=args.limit)
    _print_rows(
        rows,
        [
            ("chat_id", "chat"),
            ("last_message_at", "last_message_at"),
            ("name", "name"),
            ("contacts", "contacts"),
            ("last_text", "last_text"),
        ],
        json_output=args.json_output,
    )


def run_search(args: argparse.Namespace) -> None:
    config = load_config()
    with IMessageArchive(_db_path(args, config)) as archive:
        rows = archive.search_messages(
            args.query,
            limit=args.limit,
            chat_id=args.chat_id,
            since=args.since,
            until=args.until,
        )
    _print_rows(
        rows,
        [
            ("message_rowid", "message"),
            ("chat_id", "chat"),
            ("message_at", "message_at"),
            ("chat_name", "chat_name"),
            ("contacts", "contacts"),
            ("sender", "sender"),
            ("text", "text"),
        ],
        json_output=args.json_output,
    )


def run_unresolved(args: argparse.Namespace) -> None:
    config = load_config()
    with IMessageArchive(_db_path(args, config)) as archive:
        rows = archive.unresolved_contact_chats(limit=args.limit)
    _print_rows(
        rows,
        [
            ("chat_id", "chat"),
            ("name", "name"),
            ("source_identifier", "source_identifier"),
            ("normalized_value", "normalized_value"),
            ("updated_at", "updated_at"),
        ],
        json_output=args.json_output,
    )


def run_attachment_issues(args: argparse.Namespace) -> None:
    config = load_config()
    with IMessageArchive(_db_path(args, config)) as archive:
        rows = archive.attachment_issues(limit=args.limit)
    _print_rows(
        rows,
        [
            ("message_rowid", "message"),
            ("chat_id", "chat"),
            ("message_at", "message_at"),
            ("chat_name", "chat_name"),
            ("transfer_name", "transfer_name"),
            ("archive_error", "archive_error"),
        ],
        json_output=args.json_output,
    )


def run_draft(args: argparse.Namespace) -> None:
    config = load_config()
    if not config.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is not set; cannot draft")
    db_path = _db_path(args, config)
    data_dir = _data_dir(args, config)
    with IMessageArchive(db_path) as archive:
        message = _draft_target_message(archive, args)
        if message is None:
            raise SystemExit("No target message found")
        if args.chat_id is not None and (message.is_from_me or message.is_reaction):
            print(
                "Latest message is not an inbound reply target; "
                f"rowid={message.rowid} is_from_me={message.is_from_me}"
            )
            return
        store = MessageStore(data_dir)
        drafter = Drafter(
            store,
            OpenAIResponsesDraftingClient(api_key=config.openai_api_key),
            default_model=config.draft_model,
            max_inbox_age_hours=config.max_inbox_age_hours if args.respect_age else 0,
            auto_approve_default=config.auto_approve,
        )
        worker = ArchiveAgentWorker(
            archive=archive,
            store=store,
            drafter=drafter,
            history_limit=config.chat_context_messages,
        )
        draft = asyncio.run(worker.draft_archived_message(message))
        payload = _draft_result_payload(
            store,
            message=message,
            draft=draft,
        )
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"chat_id: {payload['chat_id']}")
    print(f"message_rowid: {payload['message_rowid']}")
    print(f"status: {payload['status']}")
    if payload.get("draft_path"):
        print(f"draft_path: {payload['draft_path']}")
    if payload.get("proposed_text"):
        print()
        print(str(payload["proposed_text"]))
    if payload.get("reasoning"):
        print()
        print(f"reasoning: {payload['reasoning']}")


def run_service(args: argparse.Namespace) -> None:
    if args.service_command_name == "status":
        services = [args.service] if args.service else ["monitor", "worker"]
        rows = [service_status(service) for service in services]
        _print_rows(
            rows,
            [
                ("service", "service"),
                ("loaded", "loaded"),
                ("running", "running"),
                ("plist_exists", "plist"),
                ("detail", "detail"),
            ],
            json_output=args.json_output,
        )
        return
    if args.service is None:
        raise SystemExit("service is required")
    if args.service_command_name == "start":
        service_start(args.service)
    elif args.service_command_name == "stop":
        service_stop(args.service)
    elif args.service_command_name == "restart":
        service_restart(args.service)


def run_logs(args: argparse.Namespace) -> None:
    config = load_config()
    path = log_path(args.service, data_dir=_data_dir(args, config), errors=args.errors)
    if args.json_output:
        print(
            json.dumps(
                {
                    "path": str(path),
                    "exists": path.exists(),
                    "lines": _tail_lines(path, args.lines),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    print(str(path))
    for line in _tail_lines(path, args.lines):
        print(line)


def _relationship_field_values(values: list[str]) -> JSON:
    fields: JSON = {}
    for item in values:
        if "=" not in item:
            raise SystemExit(f"relationship field must be key=value: {item}")
        key, raw = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit("relationship field name is required")
        try:
            value: object = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        fields[key] = value
    return fields


def run_relationship(args: argparse.Namespace) -> None:
    from .operator_service import OperatorService

    config = load_config()
    service = OperatorService(
        config=config,
        data_dir=_data_dir(args, config),
        db_path=_db_path(args, config),
    )
    fields = _relationship_field_values(args.fields)
    notes = getattr(args, "notes", None)
    if args.relationship_scope == "operator":
        payload = (
            service.update_operator_relationship(fields)
            if fields
            else service.operator_relationship()
        )
    elif args.relationship_scope == "contact":
        payload = (
            service.update_contact_relationship(
                args.contact_id,
                fields=fields,
                notes=notes,
            )
            if fields or notes is not None
            else service.contact_relationship(args.contact_id)
        )
    elif args.relationship_scope == "group":
        payload = (
            service.update_group_relationship(
                args.chat_id,
                fields=fields,
                notes=notes,
            )
            if fields or notes is not None
            else service.group_relationship(args.chat_id)
        )
    else:
        payload = service.relationship_context(args.chat_id)
    print(json.dumps(payload, indent=2, sort_keys=True))


def service_status(service: str) -> JSON:
    label = service_label(service)
    plist = plist_path(label)
    result = _run_launchctl(["print", f"gui/{os.getuid()}/{label}"])
    loaded = result.returncode == 0
    output = result.stdout or result.stderr
    return {
        "service": service,
        "label": label,
        "loaded": loaded,
        "running": loaded and "state = running" in output,
        "plist_exists": plist.exists(),
        "plist_path": str(plist),
        "detail": _service_detail(result),
    }


def service_start(service: str) -> None:
    label = service_label(service)
    plist = plist_path(label)
    if not plist.exists():
        raise SystemExit(f"plist not found: {plist}")
    domain = f"gui/{os.getuid()}"
    _launchctl_or_raise(["bootstrap", domain, str(plist)])
    _launchctl_or_raise(["enable", f"{domain}/{label}"])
    _launchctl_or_raise(["kickstart", "-k", f"{domain}/{label}"])
    print(f"started {service} ({label})")


def service_stop(service: str) -> None:
    label = service_label(service)
    plist = plist_path(label)
    if not plist.exists():
        raise SystemExit(f"plist not found: {plist}")
    _launchctl_or_raise(["bootout", f"gui/{os.getuid()}", str(plist)])
    print(f"stopped {service} ({label})")


def service_restart(service: str) -> None:
    label = service_label(service)
    plist = plist_path(label)
    if not plist.exists():
        raise SystemExit(f"plist not found: {plist}")
    domain = f"gui/{os.getuid()}"
    if not bool(service_status(service)["loaded"]):
        service_start(service)
        return
    _launchctl_or_raise(["kickstart", "-k", f"{domain}/{label}"], timeout=30)
    print(f"restarted {service} ({label})")


def service_label(service: str) -> str:
    if service == "monitor":
        return ARCHIVE_LABEL
    if service == "worker":
        return WORKER_LABEL
    raise SystemExit(f"unknown service: {service}")


def plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def log_path(service: str, *, data_dir: Path, errors: bool) -> Path:
    if service == "monitor":
        name = "imsg-archive-monitor.err.log" if errors else "imsg-archive-monitor.log"
    elif service == "worker":
        name = "imsg-agent-worker.err.log" if errors else "imsg-agent-worker.log"
    else:
        raise SystemExit(f"unknown service: {service}")
    return data_dir / "logs" / name


def _draft_target_message(archive: IMessageArchive, args: argparse.Namespace) -> Message | None:
    if args.message_rowid is not None:
        return archive.message_by_rowid(int(args.message_rowid))
    if args.chat_id is not None:
        return archive.latest_message_for_chat(int(args.chat_id))
    return None


def _draft_result_payload(
    store: MessageStore,
    *,
    message: Message,
    draft: Draft | None,
) -> JSON:
    if draft is not None:
        return {
            "status": "draft_created",
            "chat_id": message.chat_id,
            "message_rowid": message.rowid,
            "draft_uuid": draft.uuid,
            "draft_path": str(
                store.data_dir / "chats" / str(draft.chat_id) / "drafts" / f"{draft.uuid}.md"
            ),
            "proposed_text": draft.proposed_text,
            "reasoning": draft.reasoning,
            "approved": draft.approved,
        }
    artifact = _reply_artifact_for_source(
        store,
        chat_id=message.chat_id,
        source_rowid=message.rowid,
    )
    if artifact is not None:
        return {
            "status": str(artifact.get("draft_status") or "handled"),
            "chat_id": message.chat_id,
            "message_rowid": message.rowid,
            "draft_uuid": str(artifact.get("draft_uuid") or ""),
            "draft_path": str(artifact.get("draft_path") or ""),
            "proposed_text": str(artifact.get("proposed_text") or ""),
            "reasoning": str(artifact.get("reasoning") or ""),
        }
    return {
        "status": "skipped",
        "chat_id": message.chat_id,
        "message_rowid": message.rowid,
        "draft_uuid": "",
        "draft_path": "",
        "proposed_text": "",
        "reasoning": "",
    }


def _reply_artifact_for_source(
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
    if decorated.get("draft_status") != "missing":
        return decorated
    return None


def _run_launchctl(args: list[str], *, timeout: float = 5) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["launchctl", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["launchctl", *args], 1, "", str(exc))


def _launchctl_or_raise(args: list[str], *, timeout: float = 5) -> None:
    result = _run_launchctl(args, timeout=timeout)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "launchctl failed").strip()
        raise SystemExit(message)


def _service_detail(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode != 0:
        return "not loaded"
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("state ="):
            return line.removeprefix("state =").strip()
    return "loaded"


def _tail_lines(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return [f"(missing: {path})"]
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]


def _data_dir(args: argparse.Namespace, config: Config) -> Path:
    value = getattr(args, "data_dir", None)
    return Path(value).expanduser() if value else config.data_dir


def _db_path(args: argparse.Namespace, config: Config) -> Path:
    value = getattr(args, "db", None)
    return Path(value).expanduser() if value else archive_db_path(config)


def _stringify(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", " ").strip()
    if len(text) > 110:
        return f"{text[:107]}..."
    return text


def _print_pending(rows: list[ArchiveRow]) -> None:
    _print_rows(
        rows,
        [
            ("score", "score"),
            ("chat_id", "chat"),
            ("message_rowid", "message"),
            ("last_message_at", "last_message_at"),
            ("name", "name"),
            ("contacts", "contacts"),
            ("last_text", "last_text"),
            ("draft_status", "draft_status"),
            ("proposed_text", "proposed_reply"),
        ],
        json_output=False,
    )


def _print_rows(
    rows: list[dict[str, object]],
    columns: list[Column],
    *,
    json_output: bool,
) -> None:
    if json_output:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    if not rows:
        print("(none)")
        return
    widths = {
        key: max(len(label), *(len(_stringify(row.get(key))) for row in rows))
        for key, label in columns
    }
    print("  ".join(label.ljust(widths[key]) for key, label in columns))
    print("  ".join("-" * widths[key] for key, _label in columns))
    for row in rows:
        print("  ".join(_stringify(row.get(key)).ljust(widths[key]) for key, _ in columns))


def _add_data_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", help="SQLite DB path. Defaults to ~/imsg-data/imessage.sqlite")
    parser.add_argument("--data-dir", help="Data dir. Defaults to configured ~/imsg-data")
    parser.add_argument("--json", action="store_true", dest="json_output")


def _add_limited_options(parser: argparse.ArgumentParser, *, default: int) -> None:
    _add_data_options(parser)
    parser.add_argument("--limit", type=int, default=default)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage, view, and report on the local imsg-agent system."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Show archive and service status")
    _add_data_options(status)

    report = subparsers.add_parser("report", help="Show an operator dashboard report")
    _add_limited_options(report, default=5)
    report.add_argument("--issue-limit", type=int, default=10)

    pending = subparsers.add_parser("pending", help="Show pending replies and proposals")
    _add_limited_options(pending, default=5)

    recent = subparsers.add_parser("recent", help="List recently active chats")
    _add_limited_options(recent, default=25)

    attention = subparsers.add_parser("attention", help="Rank chats needing attention")
    _add_limited_options(attention, default=25)

    needs_reply = subparsers.add_parser("needs-reply", help="List latest-inbound chats")
    _add_limited_options(needs_reply, default=50)

    draft = subparsers.add_parser("draft", help="Draft against a specific chat or message now")
    _add_data_options(draft)
    target = draft.add_mutually_exclusive_group(required=True)
    target.add_argument("--chat-id", type=int, help="Draft for the latest message in this chat")
    target.add_argument("--message-rowid", type=int, help="Draft for an exact archived message")
    draft.add_argument(
        "--respect-age",
        action="store_true",
        help="Apply max_inbox_age_hours; by default manual drafting ignores message age",
    )

    search = subparsers.add_parser("search", help="Search archived messages")
    _add_limited_options(search, default=25)
    search.add_argument("query")
    search.add_argument("--chat-id", type=int)
    search.add_argument("--since")
    search.add_argument("--until")

    unresolved = subparsers.add_parser("unresolved", help="List unresolved contact matches")
    _add_limited_options(unresolved, default=50)

    attachment_issues = subparsers.add_parser(
        "attachment-issues",
        help="List attachment archive issues",
    )
    _add_limited_options(attachment_issues, default=50)

    service = subparsers.add_parser("service", help="Show or control launchd services")
    service_subparsers = service.add_subparsers(dest="service_command_name", required=True)
    service_status_parser = service_subparsers.add_parser("status", help="Show service status")
    service_status_parser.add_argument("service", nargs="?", choices=["monitor", "worker"])
    service_status_parser.add_argument("--json", action="store_true", dest="json_output")
    for command in ("start", "stop", "restart"):
        service_command = service_subparsers.add_parser(command, help=f"{command} a service")
        service_command.add_argument("service", choices=["monitor", "worker"])

    logs = subparsers.add_parser("logs", help="Show service logs")
    logs.add_argument("service", choices=["monitor", "worker"])
    logs.add_argument("--errors", action="store_true", help="Read stderr log instead of stdout")
    logs.add_argument("--lines", type=int, default=80)
    logs.add_argument("--data-dir", help="Data dir. Defaults to configured ~/imsg-data")
    logs.add_argument("--json", action="store_true", dest="json_output")

    relationship = subparsers.add_parser(
        "relationship",
        help="View or edit operator, contact, group, and effective relationship context",
    )
    relationship_subparsers = relationship.add_subparsers(
        dest="relationship_scope",
        required=True,
    )
    operator_relationship = relationship_subparsers.add_parser("operator")
    contact_relationship = relationship_subparsers.add_parser("contact")
    contact_relationship.add_argument("contact_id")
    group_relationship = relationship_subparsers.add_parser("group")
    group_relationship.add_argument("chat_id", type=int)
    effective_relationship = relationship_subparsers.add_parser("effective")
    effective_relationship.add_argument("chat_id", type=int)
    for relationship_parser in (
        operator_relationship,
        contact_relationship,
        group_relationship,
        effective_relationship,
    ):
        _add_data_options(relationship_parser)
        relationship_parser.add_argument(
            "--set",
            action="append",
            default=[],
            dest="fields",
            metavar="KEY=VALUE",
            help="Set a relationship field; JSON booleans and numbers are accepted",
        )
    contact_relationship.add_argument("--notes")
    group_relationship.add_argument("--notes")

    return parser


def cli() -> None:
    load_dotenv()
    args = _parser().parse_args()
    if args.command == "status":
        run_status(args)
    elif args.command == "report":
        run_report(args)
    elif args.command == "pending":
        run_pending(args)
    elif args.command == "recent":
        run_recent(args)
    elif args.command == "attention":
        run_attention(args)
    elif args.command == "needs-reply":
        run_needs_reply(args)
    elif args.command == "draft":
        run_draft(args)
    elif args.command == "search":
        run_search(args)
    elif args.command == "unresolved":
        run_unresolved(args)
    elif args.command == "attachment-issues":
        run_attachment_issues(args)
    elif args.command == "service":
        run_service(args)
    elif args.command == "logs":
        run_logs(args)
    elif args.command == "relationship":
        run_relationship(args)


if __name__ == "__main__":
    cli()
