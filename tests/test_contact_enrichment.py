from __future__ import annotations

from pathlib import Path

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


def test_contacts_from_json_uses_business_name_for_unknown_provider_card() -> None:
    records = contacts_from_json(
        [
            {
                "id": "business-1",
                "fullName": "Unknown",
                "organization": {"name": "SimonMed Imaging"},
                "phones": [],
            }
        ]
    )

    assert records[0].full_name == "SimonMed Imaging"


def test_contacts_from_json_preserves_a_contact_photo() -> None:
    records = contacts_from_json(
        [{"id": "contact-1", "fullName": "Alex", "photo": "aGVsbG8="}]
    )

    assert records[0].photo_data_uri == "data:image/jpeg;base64,aGVsbG8="


def test_load_contacts_expands_user_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_tool(
        *,
        command: str,
        tool: str,
        arguments: dict[str, object],
        store_path: str | None,
    ) -> dict[str, object]:
        captured.update(
            {
                "command": command,
                "tool": tool,
                "arguments": arguments,
                "store_path": store_path,
            }
        )
        output = str(arguments["outputPath"])
        Path(output).write_text("[]", encoding="utf-8")
        return {"exported": 0}

    monkeypatch.setattr("agent.contact_enrichment.contacts_mcp_tool", fake_tool)

    result = load_contacts_from_contacts_mcp(
        command="bun ${HOME}/src/contacts-mcp/dist/index.js",
        store_path="~/contacts-store",
    )

    assert result == []
    assert captured["tool"] == "export_contacts"
    assert captured["command"] == "bun ${HOME}/src/contacts-mcp/dist/index.js"
    assert captured["store_path"] == "~/contacts-store"


def test_load_contacts_reports_subprocess_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_tool(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("bad contacts command")

    monkeypatch.setattr("agent.contact_enrichment.contacts_mcp_tool", fake_tool)

    with pytest.raises(RuntimeError, match="bad contacts command"):
        load_contacts_from_contacts_mcp(command="contacts-mcp")
