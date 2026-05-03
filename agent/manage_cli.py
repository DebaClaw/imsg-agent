"""
manage_cli.py - Operator CLI for imsg-agent.

This command is the human-facing control surface over the lower-level archive, worker,
and MCP commands. It does not send messages or approve drafts.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .archive_store import ArchiveRow, IMessageArchive
from .config import Config, load_config
from .pending_report import pending_replies
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
            "pending": pending_replies(archive, store, limit=args.limit),
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
        rows = pending_replies(archive, MessageStore(_data_dir(args, config)), limit=args.limit)
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
    _run_launchctl(["bootout", f"gui/{os.getuid()}", str(plist)])
    service_start(service)


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


def _run_launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["launchctl", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["launchctl", *args], 1, "", str(exc))


def _launchctl_or_raise(args: list[str]) -> None:
    result = _run_launchctl(args)
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


if __name__ == "__main__":
    cli()
