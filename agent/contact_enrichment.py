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
import tempfile
from base64 import b64decode
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
    photo_data_uri: str
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
                photo_data_uri=contact_photo_data_uri(raw, metadata),
                points=points,
            )
        )
    return records


def contact_photo_data_uri(contact: dict[str, Any], metadata: dict[str, Any]) -> str:
    """Normalize common contacts-mcp photo export shapes for local display only."""
    candidate: object = (
        contact.get("photo")
        or contact.get("image")
        or contact.get("thumbnail")
        or metadata.get("photo")
        or metadata.get("image")
    )
    if isinstance(candidate, str):
        value = candidate.strip()
        if value.startswith("data:image/"):
            return value
        if _is_base64(value):
            return f"data:image/jpeg;base64,{value}"
    if isinstance(candidate, dict):
        data = candidate.get("data") or candidate.get("base64")
        mime = str(candidate.get("mimeType") or candidate.get("mime_type") or "image/jpeg")
        if isinstance(data, str) and _is_base64(data) and mime.startswith("image/"):
            return f"data:{mime};base64,{data}"
    return ""


def _is_base64(value: str) -> bool:
    try:
        b64decode(value, validate=True)
    except ValueError:
        return False
    return bool(value)


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
    with tempfile.TemporaryDirectory(prefix="imsg-agent-contacts-") as tmpdir:
        output_path = os.path.join(tmpdir, "contacts.json")
        contacts_mcp_tool(
            command=command,
            tool="export_contacts",
            arguments={
                "format": "json",
                "outputPath": output_path,
                "includeArchived": include_archived,
            },
            store_path=store_path,
        )
        with open(output_path, encoding="utf-8") as handle:
            parsed = json.loads(handle.read())
    if not isinstance(parsed, list):
        raise ValueError("contacts-mcp export did not return a JSON list")
    return [item for item in parsed if isinstance(item, dict)]


def contacts_mcp_tool(
    *,
    command: str,
    tool: str,
    arguments: dict[str, Any],
    store_path: str | None = None,
) -> dict[str, Any]:
    """Call one tool on contacts-mcp's local stdio JSON-RPC server."""
    args = [
        os.path.expandvars(os.path.expanduser(part))
        for part in shlex.split(command)
    ]
    env = os.environ.copy()
    if store_path:
        env["CONTACTS_MCP_STORE"] = os.path.expandvars(os.path.expanduser(store_path))
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"contacts-mcp could not start: {exc}") from exc
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        _mcp_send(
            process,
            1,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "imsg-agent", "version": "0.1"},
            },
        )
        _mcp_response(process, 1)
        _mcp_send(process, 2, "tools/call", {"name": tool, "arguments": arguments})
        response = _mcp_response(process, 2)
    finally:
        process.terminate()
        process.wait(timeout=2)
    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise RuntimeError(f"contacts-mcp {tool} failed: {response}")
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return {}
    text = content[0].get("text")
    if not isinstance(text, str):
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"message": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _mcp_send(
    process: subprocess.Popen[str],
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> None:
    assert process.stdin is not None
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()


def _mcp_response(process: subprocess.Popen[str], request_id: int) -> dict[str, Any]:
    assert process.stdout is not None
    for _ in range(200):
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(f"contacts-mcp stopped unexpectedly: {stderr.strip()}")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == request_id and isinstance(payload, dict):
            return payload
    raise RuntimeError("contacts-mcp did not respond")
