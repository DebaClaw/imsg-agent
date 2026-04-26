from __future__ import annotations

from agent.contact_enrichment import (
    contacts_from_json,
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
