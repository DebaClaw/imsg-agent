from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.archive_store import IMessageArchive
from agent.drafter import DraftResponse
from agent.manage_cli import (
    _parser,
    _relationship_field_values,
    log_path,
    run_draft,
    run_logs,
    run_pending,
    service_restart,
    service_status,
)
from agent.models import Chat, Draft, Message
from agent.store import MessageStore

NOW = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)


def _chat() -> Chat:
    return Chat(
        id=7,
        identifier="iMessage;-;+18015550101",
        name="Alex",
        service="iMessage",
        last_message_at=NOW,
        participants=["+18015550101"],
    )


def _message(rowid: int = 100) -> Message:
    return Message(
        rowid=rowid,
        chat_id=7,
        guid=f"GUID-{rowid}",
        sender="+18015550101",
        text="Can you help with this?",
        date=NOW,
        is_from_me=False,
        service="iMessage",
        has_attachments=False,
        chat_name="Alex",
        participants=["+18015550101"],
    )


class FakeOpenAIResponsesDraftingClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    async def create_draft(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
    ) -> DraftResponse:
        return DraftResponse(
            proposed_text="Yep, I can help with that.",
            reasoning="Manual CLI draft requested.",
        )


def test_manage_cli_accepts_operator_commands() -> None:
    pending = _parser().parse_args(["pending", "--limit", "5", "--json"])
    report = _parser().parse_args(["report", "--limit", "3", "--issue-limit", "2"])
    draft = _parser().parse_args(["draft", "--chat-id", "7", "--json"])
    search = _parser().parse_args(["search", "coffee", "--chat-id", "7"])
    service = _parser().parse_args(["service", "restart", "worker"])
    logs = _parser().parse_args(["logs", "monitor", "--errors", "--lines", "20"])
    relationship = _parser().parse_args(
        [
            "relationship",
            "contact",
            "contact-1",
            "--set",
            "tone=warm",
            "--set",
            "professional=false",
            "--notes",
            "Old friend",
        ]
    )
    effective = _parser().parse_args(["relationship", "effective", "7", "--json"])

    assert pending.command == "pending"
    assert pending.limit == 5
    assert pending.json_output is True
    assert report.command == "report"
    assert report.issue_limit == 2
    assert draft.command == "draft"
    assert draft.chat_id == 7
    assert draft.json_output is True
    assert search.command == "search"
    assert search.query == "coffee"
    assert search.chat_id == 7
    assert service.command == "service"
    assert service.service_command_name == "restart"
    assert service.service == "worker"
    assert logs.command == "logs"
    assert logs.errors is True
    assert logs.lines == 20
    assert relationship.command == "relationship"
    assert relationship.relationship_scope == "contact"
    assert relationship.contact_id == "contact-1"
    assert relationship.fields == ["tone=warm", "professional=false"]
    assert relationship.notes == "Old friend"
    assert effective.relationship_scope == "effective"
    assert effective.chat_id == 7


def test_manage_cli_relationship_fields_parse_json_values() -> None:
    fields = _relationship_field_values(
        ["tone=warm", "professional=false", "inherit_member_profiles=true"]
    )

    assert fields == {
        "tone": "warm",
        "professional": False,
        "inherit_member_profiles": True,
    }


def test_manage_pending_prints_matching_proposed_reply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
            target_identifier="iMessage;-;+18015550101",
            created_at=NOW,
            proposed_text="Yep, I can help with that.",
            reasoning="They asked directly.",
            prompt_version="v1",
            source_rowid=100,
        )
    )
    args = _parser().parse_args(
        ["pending", "--db", str(db_path), "--data-dir", str(tmp_path), "--limit", "5"]
    )

    run_pending(args)

    output = capsys.readouterr().out
    assert "draft_unapproved" in output
    assert "Yep, I can help with that." in output


def test_manage_draft_creates_proposal_for_chat(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "imessage.sqlite"
    archive = IMessageArchive(db_path)
    archive.upsert_chat(_chat())
    archive.upsert_message(_message())
    archive.close()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "agent.manage_cli.OpenAIResponsesDraftingClient",
        FakeOpenAIResponsesDraftingClient,
    )
    args = _parser().parse_args(
        ["draft", "--chat-id", "7", "--db", str(db_path), "--data-dir", str(tmp_path)]
    )

    run_draft(args)

    output = capsys.readouterr().out
    assert "status: draft_created" in output
    assert "Yep, I can help with that." in output
    assert MessageStore(tmp_path).draft_exists_for_source(7, 100)


def test_manage_draft_skips_chat_when_latest_message_is_from_me(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "imessage.sqlite"
    archive = IMessageArchive(db_path)
    archive.upsert_chat(_chat())
    mine = _message()
    mine.is_from_me = True
    archive.upsert_message(mine)
    archive.close()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    args = _parser().parse_args(
        ["draft", "--chat-id", "7", "--db", str(db_path), "--data-dir", str(tmp_path)]
    )

    run_draft(args)

    output = capsys.readouterr().out
    assert "Latest message is not an inbound reply target" in output


def test_manage_logs_reads_configured_log_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "imsg-agent-worker.err.log"
    path.write_text("first\nsecond\nthird\n", encoding="utf-8")
    args = _parser().parse_args(
        ["logs", "worker", "--errors", "--lines", "2", "--data-dir", str(tmp_path)]
    )

    run_logs(args)

    output = capsys.readouterr().out
    assert str(path) in output
    assert "first" not in output
    assert "second" in output
    assert "third" in output


def test_service_status_parses_launchctl_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "state = running\n", "")

    monkeypatch.setattr("agent.manage_cli._run_launchctl", fake_run)

    status = service_status("worker")

    assert status["service"] == "worker"
    assert status["loaded"] is True
    assert status["running"] is True
    assert status["detail"] == "running"


def test_service_restart_kickstarts_loaded_launchagent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent.manage_cli.plist_path", lambda _label: tmp_path / "worker.plist")
    (tmp_path / "worker.plist").touch()
    monkeypatch.setattr(
        "agent.manage_cli.service_status",
        lambda _service: {"loaded": True},
    )
    captured: dict[str, object] = {}

    def fake_launchctl(args: list[str], *, timeout: float = 5) -> None:
        captured["args"] = args
        captured["timeout"] = timeout

    monkeypatch.setattr("agent.manage_cli._launchctl_or_raise", fake_launchctl)

    service_restart("worker")

    assert captured["args"] == ["kickstart", "-k", f"gui/{os.getuid()}/com.imsg-agent.worker"]
    assert captured["timeout"] == 30
    assert "restarted worker" in capsys.readouterr().out


def test_log_path_selects_service_files(tmp_path: Path) -> None:
    assert log_path("monitor", data_dir=tmp_path, errors=False).name == "imsg-archive-monitor.log"
    assert log_path("worker", data_dir=tmp_path, errors=True).name == "imsg-agent-worker.err.log"
