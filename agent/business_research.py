"""Public, review-first business contact discovery through the Responses API."""
from __future__ import annotations

import json
from typing import Any

from .contact_enrichment import normalize_phone

JSON = dict[str, Any]


class BusinessResearchError(RuntimeError):
    """Raised when a business-research response cannot be used safely."""


class OpenAIBusinessResearcher:
    """Research public business details without creating or editing a contact."""

    def __init__(self, *, api_key: str, model: str) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - runtime dependency guard
            raise BusinessResearchError(
                "The openai package is required for business research."
            ) from exc
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def research(self, *, name: str, location: str) -> JSON:
        try:
            response = self._client.responses.create(
                model=self._model,
                tools=[{"type": "web_search"}],
                instructions=(
                    "Research one public-facing business. Use web search. Prefer the business's "
                    "official website and corroborate phone numbers with a second reputable "
                    "source when possible. Do not invent a phone number, email, address, "
                    "website, or source. Return JSON only with: business_name, location, "
                    "website, phone, email, address, phone_status (verified, candidate, or "
                    "not_found), confidence (0-100), summary, and sources (a list of {title, "
                    "url}). phone_status is verified only when the phone appears on the official "
                    "site or is corroborated by two independent public sources."
                ),
                input=f"Find contact details for this business: {name}\nLocation: {location}",
                text={"format": {"type": "json_object"}},
            )
        except Exception as exc:
            raise BusinessResearchError(f"Business research failed: {exc}") from exc
        return parse_business_research(response.output_text, requested_name=name, location=location)


def parse_business_research(payload: str, *, requested_name: str, location: str) -> JSON:
    """Validate model output and prepare fields for explicit operator review."""
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BusinessResearchError("Business research returned invalid JSON.") from exc
    if not isinstance(raw, dict):
        raise BusinessResearchError("Business research returned an invalid result.")

    business_name = _text(raw.get("business_name")) or requested_name.strip()
    if not business_name:
        raise BusinessResearchError("A business name is required for research.")
    phone = _text(raw.get("phone"))
    normalized_phone = normalize_phone(phone) if phone else ""
    phone_status = _text(raw.get("phone_status")).lower()
    if phone_status not in {"verified", "candidate", "not_found"}:
        phone_status = "candidate" if normalized_phone else "not_found"
    confidence = _confidence(raw.get("confidence"))
    sources = _sources(raw.get("sources"))
    summary = _text(raw.get("summary"))
    website = _url(raw.get("website"))
    email = _text(raw.get("email"))
    address = _text(raw.get("address"))

    notes = ["Public business research. Review before saving."]
    if location.strip():
        notes.append(f"Requested location: {location.strip()}")
    if address:
        notes.append(f"Address: {address}")
    if website:
        notes.append(f"Website: {website}")
    if summary:
        notes.append(summary)
    if sources:
        notes.append("Sources: " + "; ".join(source["url"] for source in sources))

    candidate: JSON = {
        "fullName": business_name,
        "organization": {"name": business_name},
        "notes": "\n".join(notes),
        "categories": ["business"],
    }
    if phone:
        candidate["phones"] = [{"value": phone, "label": "work", "primary": True}]
    if email:
        candidate["emails"] = [{"value": email, "label": "work", "primary": True}]
    return {
        "business_name": business_name,
        "location": _text(raw.get("location")) or location.strip(),
        "website": website,
        "phone": phone,
        "phone_normalized": normalized_phone,
        "phone_status": phone_status,
        "email": email,
        "address": address,
        "confidence": confidence,
        "summary": summary,
        "sources": sources,
        "candidate": candidate,
    }


def _text(value: object) -> str:
    return str(value or "").strip()


def _url(value: object) -> str:
    url = _text(value)
    return url if url.startswith(("https://", "http://")) else ""


def _confidence(value: object) -> int:
    try:
        return max(0, min(100, int(str(value))))
    except (TypeError, ValueError):
        return 0


def _sources(value: object) -> list[JSON]:
    if not isinstance(value, list):
        return []
    sources: list[JSON] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        url = _url(item.get("url"))
        if url:
            sources.append({"title": _text(item.get("title")) or url, "url": url})
    return sources
