from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.models import Draft, OutboxItem
from agent.sender import (
    ApprovalScanner,
    DeliveryVerification,
    DeliveryVerificationFailed,
    MessagesDeliveryVerifier,
    Sender,
)
from agent.store import MessageStore, _parse_frontmatter, _write_frontmatter

NOW = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


class FakeRPC:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict[str, object]] = []

    async def send(
        self,
        text: str = "",
        file: str | None = None,
        service: str = "auto",
        chat_id: int | None = None,
        to: str | None = None,
        chat_identifier: str | None = None,
        chat_guid: str | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("send timeout")
        self.sent.append(
            {
                "text": text,
                "file": file,
                "service": service,
                "chat_id": chat_id,
                "to": to,
                "chat_identifier": chat_identifier,
                "chat_guid": chat_guid,
            }
        )


class FakeVerifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.verified: list[dict[str, object]] = []

    async def verify(
        self,
        *,
        item: OutboxItem,
        attempt_started_at: datetime,
    ) -> DeliveryVerification:
        if self.fail:
            raise DeliveryVerificationFailed(
                "send returned but no outgoing Messages row was observed"
            )
        self.verified.append({"item": item, "attempt_started_at": attempt_started_at})
        return DeliveryVerification(rowid=99, date=1)


def _draft(approved: bool = True, *, auto_approved: bool = False) -> Draft:
    return Draft(
        uuid="20260425T120000Z-1",
        chat_id=7,
        target_identifier="SMS;-;+14155550101",
        created_at=NOW,
        proposed_text="Yes, still on for Thursday.",
        reasoning="They asked for confirmation.",
        prompt_version="draft_v1",
        approved=approved,
        source_rowid=1,
        model="gpt-5.5",
        auto_approved=auto_approved,
        service="SMS",
    )


def _move_approved_draft_to_outbox(store: MessageStore) -> None:
    store.write_draft(_draft())
    assert ApprovalScanner(store).run_pass() == 1


def test_approval_scanner_moves_approved_draft_to_outbox(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.write_draft(_draft())

    moved = ApprovalScanner(store).run_pass()

    assert moved == 1
    assert not list((tmp_path / "chats" / "7" / "drafts").glob("*.md"))
    outbox_path = tmp_path / "outbox" / "20260425T120000Z-1.md"
    meta, body = _parse_frontmatter(outbox_path.read_text())
    assert meta["source_draft_uuid"] == "20260425T120000Z-1"
    assert meta["source_rowid"] == 1
    assert meta["reasoning"] == "They asked for confirmation."
    assert meta["service"] == "SMS"
    assert body.strip() == "Yes, still on for Thursday."


def test_approval_scanner_leaves_unapproved_draft(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.write_draft(_draft(approved=False))

    moved = ApprovalScanner(store).run_pass()

    assert moved == 0
    assert list((tmp_path / "chats" / "7" / "drafts").glob("*.md"))
    assert not (tmp_path / "outbox").exists()


@pytest.mark.asyncio
async def test_sender_sends_and_archives_reasoning(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _move_approved_draft_to_outbox(store)
    rpc = FakeRPC()
    verifier = FakeVerifier()

    sent = await Sender(store, rpc, verifier=verifier).run_pass()  # type: ignore[arg-type]

    assert sent == 1
    assert rpc.sent == [
        {
            "text": "Yes, still on for Thursday.",
            "file": None,
            "service": "auto",
            "chat_id": 7,
            "to": None,
            "chat_identifier": None,
            "chat_guid": None,
        }
    ]
    sent_path = tmp_path / "sent" / "20260425T120000Z-1.md"
    meta, body = _parse_frontmatter(sent_path.read_text())
    assert meta["source_draft_uuid"] == "20260425T120000Z-1"
    assert meta["reasoning"] == "They asked for confirmation."
    assert meta["service"] == "SMS"
    assert body.strip() == "Yes, still on for Thursday."
    assert not list((tmp_path / "outbox").glob("*.md"))
    assert len(verifier.verified) == 1


@pytest.mark.asyncio
async def test_sender_moves_failed_send_to_errors(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _move_approved_draft_to_outbox(store)

    sent = await Sender(
        store,
        FakeRPC(fail=True),  # type: ignore[arg-type]
        verifier=FakeVerifier(),
    ).run_pass()

    assert sent == 0
    assert not (tmp_path / "sent" / "20260425T120000Z-1.md").exists()
    error_path = tmp_path / "errors" / "20260425T120000Z-1.md"
    meta, body = _parse_frontmatter(error_path.read_text())
    assert "RPC timeout/error" in meta["error"]
    assert "send timeout" in meta["error"]
    assert meta["reasoning"] == "They asked for confirmation."
    assert body.strip() == "Yes, still on for Thursday."


@pytest.mark.asyncio
async def test_sender_moves_unverified_send_to_errors(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _move_approved_draft_to_outbox(store)
    rpc = FakeRPC()

    sent = await Sender(store, rpc, verifier=FakeVerifier(fail=True)).run_pass()  # type: ignore[arg-type]

    assert sent == 0
    assert rpc.sent
    assert not (tmp_path / "sent" / "20260425T120000Z-1.md").exists()
    error_path = tmp_path / "errors" / "20260425T120000Z-1.md"
    meta, body = _parse_frontmatter(error_path.read_text())
    assert "no outgoing Messages row was observed" in meta["error"]
    assert meta["reasoning"] == "They asked for confirmation."
    assert body.strip() == "Yes, still on for Thursday."


@pytest.mark.asyncio
async def test_sender_rejects_attachment_outside_allowlist(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    _move_approved_draft_to_outbox(store)
    outbox_path = tmp_path / "outbox" / "20260425T120000Z-1.md"
    meta, body = _parse_frontmatter(outbox_path.read_text())
    meta["attachment_path"] = "/tmp/not-allowed.txt"
    outbox_path.write_text(_write_frontmatter(meta, body))
    rpc = FakeRPC()

    sent = await Sender(store, rpc).run_pass()  # type: ignore[arg-type]

    assert sent == 0
    assert rpc.sent == []
    error_path = tmp_path / "errors" / "20260425T120000Z-1.md"
    err_meta, _ = _parse_frontmatter(error_path.read_text())
    assert "attachment outside" in err_meta["error"]


@pytest.mark.asyncio
async def test_messages_delivery_verifier_requires_matching_row_after_attempt(
    tmp_path: Path,
) -> None:
    chat_db = tmp_path / "chat.db"
    db = sqlite3.connect(chat_db)
    db.executescript(
        """
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            text TEXT,
            is_from_me INTEGER,
            date INTEGER
        );
        CREATE TABLE chat_message_join (
            chat_id INTEGER,
            message_id INTEGER
        );
        """
    )
    verifier = MessagesDeliveryVerifier(
        chat_db,
        timeout_seconds=0,
        poll_interval_seconds=0,
    )
    item = OutboxItem(
        uuid="uuid",
        chat_id=7,
        target_identifier="SMS;-;+14155550101",
        text="Yes, still on for Thursday.",
        attachment_path=None,
        created_at=NOW,
    )
    before_attempt = verifier._to_messages_date(NOW) - 1
    after_attempt = verifier._to_messages_date(NOW) + 1
    rows = [
        (1, item.text, 1, before_attempt, 7),
        (2, item.text, 1, after_attempt, 8),
        (3, "wrong text", 1, after_attempt, 7),
        (4, item.text, 0, after_attempt, 7),
        (5, item.text, 1, after_attempt, 7),
    ]
    db.executemany(
        "INSERT INTO message (ROWID, text, is_from_me, date) VALUES (?, ?, ?, ?)",
        [r[:4] for r in rows],
    )
    db.executemany(
        "INSERT INTO chat_message_join (message_id, chat_id) VALUES (?, ?)",
        [(r[0], r[4]) for r in rows],
    )
    db.commit()
    db.close()

    verification = await verifier.verify(item=item, attempt_started_at=NOW)

    assert verification.rowid == 5


@pytest.mark.asyncio
async def test_messages_delivery_verifier_rejects_only_pre_attempt_rows(
    tmp_path: Path,
) -> None:
    chat_db = tmp_path / "chat.db"
    db = sqlite3.connect(chat_db)
    db.executescript(
        """
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            text TEXT,
            is_from_me INTEGER,
            date INTEGER
        );
        CREATE TABLE chat_message_join (
            chat_id INTEGER,
            message_id INTEGER
        );
        """
    )
    verifier = MessagesDeliveryVerifier(
        chat_db,
        timeout_seconds=0,
        poll_interval_seconds=0,
    )
    item = OutboxItem(
        uuid="uuid",
        chat_id=7,
        target_identifier="SMS;-;+14155550101",
        text="Yes, still on for Thursday.",
        attachment_path=None,
        created_at=NOW,
    )
    db.execute(
        "INSERT INTO message (ROWID, text, is_from_me, date) VALUES (?, ?, ?, ?)",
        (1, item.text, 1, verifier._to_messages_date(NOW) - 1),
    )
    db.execute(
        "INSERT INTO chat_message_join (message_id, chat_id) VALUES (?, ?)",
        (1, 7),
    )
    db.commit()
    db.close()

    with pytest.raises(DeliveryVerificationFailed, match="no outgoing Messages row"):
        await verifier.verify(item=item, attempt_started_at=NOW)
