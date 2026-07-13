from __future__ import annotations

import json

import pytest

from agent.business_research import BusinessResearchError, parse_business_research


def test_parse_business_research_normalizes_phone_and_sources() -> None:
    result = parse_business_research(
        json.dumps(
            {
                "business_name": "Northwind Coffee",
                "location": "Seattle, WA",
                "website": "https://northwind.example",
                "phone": "(206) 555-0199",
                "email": "hello@northwind.example",
                "address": "123 Pike St, Seattle, WA",
                "phone_status": "verified",
                "confidence": 120,
                "summary": "Official site lists the number.",
                "sources": [
                    {"title": "Northwind", "url": "https://northwind.example/contact"},
                    {"title": "Bad", "url": "javascript:alert(1)"},
                ],
            }
        ),
        requested_name="Northwind Coffee",
        location="Seattle, WA",
    )

    assert result["phone_normalized"] == "+12065550199"
    assert result["phone_status"] == "verified"
    assert result["confidence"] == 100
    assert result["sources"] == [{"title": "Northwind", "url": "https://northwind.example/contact"}]
    assert result["candidate"]["phones"] == [
        {"value": "(206) 555-0199", "label": "work", "primary": True}
    ]


def test_parse_business_research_rejects_invalid_json() -> None:
    with pytest.raises(BusinessResearchError, match="invalid JSON"):
        parse_business_research("not json", requested_name="Northwind", location="Seattle")
