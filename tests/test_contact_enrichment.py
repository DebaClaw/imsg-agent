from __future__ import annotations

import subprocess

import pytest

from agent.contact_enrichment import (
    contacts_from_json,
    load_contacts_from_contacts_mcp,
    normalize_email,
    normalize_identifier,
    normalize_phone,
)


def test_normalize_identifier_handles_imessage_phone_prefix() -> None:
    assert normalize_identifier("iMessage;-;(801) 602-2838") == (
        "phone",
        "+18016022838",
    )


def test_normalize_identifier_handles_email() -> None:
    assert normalize_identifier(" Alice@Example.COM ") == ("email", "alice@example.com")


def test_normalize_helpers() -> None:
    assert normalize_email(" ZOB@EXAMPLE.COM ") == "zob@example.com"
    assert normalize_phone("801-602-2838") == "+18016022838"
    assert normalize_phone("+44 20 7946 0018") == "+442079460018"


def test_contacts_from_json_extracts_points() -> None:
    records = contacts_from_json(
        [
            {
                "id": "contact-1",
                "fullName": "Alex Example",
                "name": {"givenName": "Alex", "familyName": "Example"},
                "emails": [{"value": "alex@example.com", "type": "home"}],
                "phones": [{"value": "(801) 602-2838", "type": "mobile"}],
                "organization": {"name": "Acme", "title": "Friend"},
                "categories": ["friends"],
                "metadata": {"source": "apple"},
            }
        ]
    )

    assert len(records) == 1
    assert records[0].contact_id == "contact-1"
    assert records[0].full_name == "Alex Example"
    assert [point.value for point in records[0].points] == [
        "alex@example.com",
        "+18016022838",
    ]


def test_load_contacts_expands_user_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["env"] = env
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    monkeypatch.setattr("agent.contact_enrichment.subprocess.run", fake_run)

    result = load_contacts_from_contacts_mcp(
        command="bun ${HOME}/src/contacts-mcp/dist/index.js",
        store_path="~/contacts-store",
    )

    assert result == []
    args = captured["args"]
    assert isinstance(args, list)
    assert args[1].startswith("/")
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["CONTACTS_MCP_STORE"].startswith("/")


def test_load_contacts_reports_subprocess_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, args, stderr="bad contacts command")

    monkeypatch.setattr("agent.contact_enrichment.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="bad contacts command"):
        load_contacts_from_contacts_mcp(command="contacts-mcp")
