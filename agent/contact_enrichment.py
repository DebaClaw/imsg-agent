"""
contact_enrichment.py - Deterministic Contacts enrichment for the SQLite archive.

This module intentionally uses code-only matching: exact normalized emails and phone
numbers. Ambiguous and unresolved identifiers are recorded for operator review.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

PHONE_CHARS_RE = re.compile(r"[^0-9+]+")
IMSG_PREFIX_RE = re.compile(r"^[^;]*;-;")


@dataclass(frozen=True)
class ContactPoint:
    kind: str
    value: str
    original_value: str
    label: str
    primary: bool


@dataclass(frozen=True)
class ContactRecord:
    contact_id: str
    full_name: str
    given_name: str
    family_name: str
    organization_name: str
    organization_title: str
    birthday: str
    notes: str
    categories_json: str
    metadata_json: str
    points: list[ContactPoint]


@dataclass(frozen=True)
class ContactsSyncResult:
    contacts: int
    contact_points: int


@dataclass(frozen=True)
class ContactsEnrichResult:
    chats: int
    matched: int
    ambiguous: int
    unresolved: int


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str, default_country: str = "US") -> str:
    cleaned = PHONE_CHARS_RE.sub("", value.strip())
    if not cleaned:
        return ""
    if cleaned.startswith("+"):
        return "+" + re.sub(r"\D+", "", cleaned)
    digits = re.sub(r"\D+", "", cleaned)
    if default_country.upper() == "US":
        if len(digits) == 10:
            return f"+1{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
    return f"+{digits}" if digits else ""


def normalize_identifier(value: str, default_country: str = "US") -> tuple[str, str] | None:
    candidate = IMSG_PREFIX_RE.sub("", value.strip())
    if not candidate:
        return None
    if "@" in candidate:
        normalized = normalize_email(candidate)
        return ("email", normalized) if normalized else None
    normalized_phone = normalize_phone(candidate, default_country)
    if normalized_phone:
        return "phone", normalized_phone
    return None


def contacts_from_json(
    data: list[dict[str, Any]],
    default_country: str = "US",
) -> list[ContactRecord]:
    records: list[ContactRecord] = []
    for raw in data:
        contact_id = str(raw.get("id") or "")
        if not contact_id:
            continue
        name_raw = raw.get("name")
        name: dict[str, Any] = name_raw if isinstance(name_raw, dict) else {}
        organization_raw = raw.get("organization")
        organization: dict[str, Any] = (
            organization_raw if isinstance(organization_raw, dict) else {}
        )
        metadata_raw = raw.get("metadata")
        metadata: dict[str, Any] = metadata_raw if isinstance(metadata_raw, dict) else {}
        categories_raw = raw.get("categories")
        categories: list[Any] = categories_raw if isinstance(categories_raw, list) else []
        points = contact_points_from_contact(raw, default_country)
        records.append(
            ContactRecord(
                contact_id=contact_id,
                full_name=str(raw.get("fullName") or ""),
                given_name=str(name.get("givenName") or ""),
                family_name=str(name.get("familyName") or ""),
                organization_name=str(organization.get("name") or ""),
                organization_title=str(organization.get("title") or ""),
                birthday=str(raw.get("birthday") or ""),
                notes=str(raw.get("notes") or ""),
                categories_json=json.dumps(categories, ensure_ascii=False),
                metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                points=points,
            )
        )
    return records


def contact_points_from_contact(
    contact: dict[str, Any],
    default_country: str = "US",
) -> list[ContactPoint]:
    points: list[ContactPoint] = []
    emails_raw = contact.get("emails")
    phones_raw = contact.get("phones")
    emails: list[Any] = emails_raw if isinstance(emails_raw, list) else []
    phones: list[Any] = phones_raw if isinstance(phones_raw, list) else []
    for email in emails:
        if not isinstance(email, dict):
            continue
        original = str(email.get("value") or "")
        value = normalize_email(original)
        if value:
            points.append(
                ContactPoint(
                    kind="email",
                    value=value,
                    original_value=original,
                    label=str(email.get("type") or ""),
                    primary=bool(email.get("primary")),
                )
            )
    for phone in phones:
        if not isinstance(phone, dict):
            continue
        original = str(phone.get("originalValue") or phone.get("value") or "")
        value = normalize_phone(str(phone.get("value") or ""), default_country)
        if value:
            points.append(
                ContactPoint(
                    kind="phone",
                    value=value,
                    original_value=original,
                    label=str(phone.get("type") or ""),
                    primary=bool(phone.get("primary")),
                )
            )
    return points


def load_contacts_from_contacts_mcp(
    *,
    command: str,
    store_path: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    args = [*shlex.split(command), "export", "--format", "json", "--output", "-"]
    if include_archived:
        args.append("--include-archived")
    env = os.environ.copy()
    if store_path:
        env["CONTACTS_MCP_STORE"] = store_path
    completed = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    parsed = json.loads(completed.stdout)
    if not isinstance(parsed, list):
        raise ValueError("contacts-mcp export did not return a JSON list")
    return [item for item in parsed if isinstance(item, dict)]
