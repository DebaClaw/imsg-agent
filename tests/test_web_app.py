from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.archive_store import IMessageArchive
from agent.config import Config
from agent.contact_enrichment import contacts_from_json
from agent.models import Chat, Draft, Message
from agent.store import MessageStore, _parse_frontmatter
from agent.web_app import WebAPIError, WebService

NOW = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)


def _config(tmp_path: Path, *, openai_api_key: str | None = None) -> Config:
    return Config(
        imsg_binary=Path("imsg"),
        data_dir=tmp_path,
        rpc_timeout_seconds=30,
        rpc_read_limit_bytes=1024,
        watch_debounce_ms=250,
        history_limit=50,
        chat_context_messages=20,
        auto_approve=False,
        default_service="auto",
        max_inbox_age_hours=48,
        openai_api_key=openai_api_key,
        draft_model="gpt-5.5",
        maintenance_interval_seconds=5.0,
        nudge_after_hours=72,
        contacts_command="contacts-mcp",
        contacts_store=None,
    )


def _service(tmp_path: Path) -> WebService:
    return WebService(
        config=_config(tmp_path),
        data_dir=tmp_path,
        db_path=tmp_path / "imessage.sqlite",
    )


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


def _draft(*, approved: bool = False) -> Draft:
    return Draft(
        uuid="draft-1",
        chat_id=7,
        target_identifier="iMessage;-;+18015550101",
        created_at=NOW,
        proposed_text="Yep, I can help with that.",
        reasoning="They asked a straightforward question.",
        prompt_version="v1",
        approved=approved,
        source_rowid=100,
        model="gpt-5.5",
    )


def _seed_archive(tmp_path: Path) -> None:
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.upsert_chat(_chat())
        archive.upsert_message(_message())


def test_web_pending_reads_archive_and_draft_artifact(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    MessageStore(tmp_path).write_draft(_draft())

    rows = _service(tmp_path).pending(limit=5, days=0)

    assert len(rows) == 1
    assert rows[0]["chat_id"] == 7
    assert rows[0]["draft_status"] == "draft_unapproved"
    assert rows[0]["proposed_text"] == "Yep, I can help with that."


def test_web_chat_returns_context_and_messages(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    MessageStore(tmp_path).write_chat_context(
        7,
        {
            "chat_id": 7,
            "relationship": "close friend",
            "tone": "warm",
            "professional": False,
            "notes": "Met through the hiking group.",
        },
    )

    payload = _service(tmp_path).chat(7)

    assert payload["context"]["relationship"] == "close friend"
    assert payload["notes"] == "Met through the hiking group."
    assert payload["messages"][0]["text"] == "Can you help with this?"
    assert payload["reply"]["message_rowid"] == 100
    assert payload["reply"]["draft_status"] == "missing"
    assert payload["reply"]["direction"] == "inbound"


def test_web_recent_and_attention_include_reply_workflow_state(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    MessageStore(tmp_path).write_draft(_draft())

    service = _service(tmp_path)
    recent = service.recent(limit=5)
    attention = service.attention(limit=5)

    for row in (recent[0], attention[0]):
        assert row["message_rowid"] == 100
        assert row["draft_status"] == "draft_unapproved"
        assert row["proposed_text"] == "Yep, I can help with that."
        assert row["channel"] == "iMessage"
        assert row["direction"] == "inbound"
        assert row["can_request_draft"] is True


def test_web_recent_exposes_outbound_latest_message_for_follow_up(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    outbound = _message(rowid=101)
    outbound.is_from_me = True
    outbound.sender = "me"
    outbound.text = "Just checking in."
    outbound.date = NOW.replace(hour=13)
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.upsert_message(outbound)

    row = _service(tmp_path).recent(limit=5)[0]

    assert row["message_rowid"] == 101
    assert row["last_text"] == "Just checking in."
    assert row["latest_is_from_me"] == 1
    assert row["direction"] == "outbound"
    assert row["draft_status"] == "missing"


def test_web_edit_draft_keeps_it_unapproved(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.write_draft(_draft(approved=True))

    payload = _service(tmp_path).edit_draft("draft-1", text="Updated reply.")
    draft = store.read_draft(tmp_path / "chats" / "7" / "drafts" / "draft-1.md")

    assert payload["status"] == "edited"
    assert draft is not None
    assert draft.proposed_text == "Updated reply."
    assert draft.approved is False


def test_web_approve_moves_draft_to_outbox_without_sending(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.write_chat_context(7, {"chat_id": 7, "professional": True})
    store.write_draft(_draft())

    payload = _service(tmp_path).approve_draft("draft-1", text="Approved reply.")
    outbox = store.list_outbox()
    item = store.read_outbox_item(outbox[0])

    assert payload["status"] == "queued"
    assert payload["sent"] is False
    assert payload["professional"] is True
    assert item is not None
    assert item.text == "Approved reply."
    assert not (tmp_path / "chats" / "7" / "drafts" / "draft-1.md").exists()
    assert not (tmp_path / "sent" / "draft-1.md").exists()


def test_web_request_draft_requires_api_key(tmp_path: Path) -> None:
    _seed_archive(tmp_path)

    with pytest.raises(WebAPIError, match="OPENAI_API_KEY"):
        _service(tmp_path).request_draft(message_rowid=100)


def test_web_reject_draft_records_no_reply_decision(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    store = MessageStore(tmp_path)
    store.write_draft(_draft())

    payload = _service(tmp_path).reject_draft("draft-1", reasoning="Already handled.")

    rows = _service(tmp_path).pending(limit=5)
    assert payload["status"] == "rejected"
    assert rows == []
    assert not (tmp_path / "chats" / "7" / "drafts" / "draft-1.md").exists()
    assert list((tmp_path / "no_reply").glob("*.md"))
    assert list((tmp_path / "draft_archive").glob("*.md"))


def test_web_archives_draft_without_deleting_the_artifact(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.write_draft(_draft())

    payload = _service(tmp_path).archive_draft("draft-1", reason="Not timely anymore.")

    archive_path = tmp_path / "draft_archive" / "draft-1.md"
    assert payload["status"] == "archived"
    assert archive_path.exists()
    meta, body = _parse_frontmatter(archive_path.read_text(encoding="utf-8"))
    assert meta["status"] == "archived"
    assert meta["archive_reason"] == "Not timely anymore."
    assert body.strip() == "Yep, I can help with that."
    assert not (tmp_path / "chats" / "7" / "drafts" / "draft-1.md").exists()


def test_web_pending_defaults_to_seven_days_and_can_explore_older(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    MessageStore(tmp_path).write_draft(_draft())

    assert _service(tmp_path).pending(limit=5) == []
    assert len(_service(tmp_path).pending(limit=5, days=0)) == 1


def test_web_profile_and_preferences_persist_locally(tmp_path: Path) -> None:
    service = _service(tmp_path)

    profile = service.update_operator_profile(
        {"display_name": "Debbie", "aliases": ["+18015550100"]}
    )
    preferences = service.update_observatory_preferences(
        {"pending_days": 14, "relationship_types": ["friend", "family"]}
    )

    assert profile["operator"]["display_name"] == "Debbie"
    assert profile["operator"]["aliases"] == ["+18015550100"]
    assert preferences["preferences"]["pending_days"] == 14
    assert preferences["preferences"]["relationship_types"] == ["friend", "family"]


def test_web_profile_uses_configured_synced_contact_card(tmp_path: Path) -> None:
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.replace_contacts(
            contacts_from_json(
                [
                    {
                        "id": "operator-contact",
                        "fullName": "Debbie Example",
                        "organization": {"name": "Example Studio"},
                        "photo": "aGVsbG8=",
                    }
                ]
            )
        )

    profile = _service(tmp_path).update_operator_profile(
        {"display_name": "Fallback", "contact_id": "operator-contact"}
    )["operator"]

    assert profile["display_name"] == "Debbie Example"
    assert profile["contact"]["organization_name"] == "Example Studio"
    assert profile["avatar_data_uri"] == "data:image/jpeg;base64,aGVsbG8="


def test_web_operator_identity_excludes_me_from_context_recipients(tmp_path: Path) -> None:
    own_number = "+18015550100"
    recipient = "+18015550101"
    chat = _chat()
    chat.participants = [own_number, recipient, "Me"]
    message = _message()
    message.participants = [own_number, recipient, "Me"]
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.upsert_chat(chat)
        archive.upsert_message(message)
        archive.replace_contacts(
            contacts_from_json(
                [
                    {
                        "id": "operator-contact",
                        "fullName": "Debbie",
                        "phones": [{"value": own_number}],
                    }
                ]
            )
        )

    service = _service(tmp_path)
    profile = service.update_operator_profile(
        {"name": "Debbie", "contact_id": "operator-contact", "aliases": [own_number]}
    )["operator"]
    chat_payload = service.chat(7)
    service.update_chat_context(7, fields={"participants": [own_number, recipient, "Me"]})
    context = MessageStore(tmp_path).read_chat_context(7)

    assert profile["identity"]["name"] == "Debbie"
    assert profile["identity"]["contact_id"] == "operator-contact"
    assert chat_payload["recipients"] == [recipient]
    assert context["participants"] == [recipient]


def test_web_hides_operator_contact_from_orbit_and_chat_contacts(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.replace_contacts(
            contacts_from_json(
                [
                    {"id": "operator-contact", "fullName": "Debbie"},
                    {"id": "alex-contact", "fullName": "Alex"},
                ]
            )
        )
        archive.link_chat_contact(7, "operator-contact")
        archive.link_chat_contact(7, "alex-contact")

    service = _service(tmp_path)
    service.update_operator_profile({"name": "Debbie", "contact_id": "operator-contact"})
    chat = service.chat(7)
    orbit = service.orbit(direction="incoming", days=0)

    assert [contact["contact_id"] for contact in chat["contacts"]] == ["alex-contact"]
    assert orbit["items"][0]["contacts"] == "Alex"


def test_web_contact_review_stays_local_and_can_prepare_a_vcard(tmp_path: Path) -> None:
    _seed_archive(tmp_path)

    payload = _service(tmp_path).review_contact(
        chat_id=7,
        decision="prepare_contact",
        notes="Check this person before importing.",
    )

    assert payload["decision"] == "prepare_contact"
    assert (tmp_path / "contact_reviews" / "7.md").exists()
    candidate = tmp_path / "contact_candidates" / "7.vcf"
    assert candidate.exists()
    assert "BEGIN:VCARD" in candidate.read_text(encoding="utf-8")


def test_web_orbit_hides_ignored_spam_until_requested(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.upsert_chat(
            Chat(
                id=8,
                identifier="iMessage;-;+18015550102",
                name="Sam",
                service="iMessage",
                last_message_at=NOW,
                participants=["+18015550102"],
            )
        )
        archive.upsert_message(
            Message(
                rowid=101,
                chat_id=8,
                guid="GUID-101",
                sender="+18015550102",
                text="Hello from Sam",
                date=NOW,
                is_from_me=False,
                service="iMessage",
                has_attachments=False,
                chat_name="Sam",
                participants=["+18015550102"],
            )
        )

    service = _service(tmp_path)
    service.review_contact(chat_id=7, decision="ignore_spam")

    hidden = service.orbit(direction="incoming", days=0)
    included = service.orbit(direction="incoming", days=0, include_spam=True)

    assert hidden["total"] == 1
    assert [row["chat_id"] for row in hidden["items"]] == [8]
    assert included["total"] == 2
    assert {row["chat_id"] for row in included["items"]} == {7, 8}


def test_web_ignore_spam_fills_blank_context_and_blocks_drafting(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    MessageStore(tmp_path).write_chat_context(
        7,
        {
            "chat_id": 7,
            "relationship": "",
            "tone": "Already documented tone.",
            "auto_approve": True,
            "do_not_draft": False,
            "agent_notes": "",
            "notes": "",
        },
    )

    payload = _service(tmp_path).review_contact(chat_id=7, decision="ignore_spam")
    context, notes = MessageStore(tmp_path).read_chat_context_document(7)

    assert payload["decision"] == "ignore_spam"
    assert context["relationship"] == "unwanted contact / spam"
    assert context["tone"] == "Already documented tone."
    assert context["do_not_draft"] is True
    assert context["auto_approve"] is False
    assert "Do not draft replies" in context["agent_notes"]
    assert "Unwanted sender" in notes


def test_web_browses_and_manually_links_synced_contacts(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.replace_contacts(
            contacts_from_json(
                [{"id": "contact-1", "fullName": "Alex Appleseed", "phones": []}]
            )
        )

    service = _service(tmp_path)
    contacts = service.contacts(limit=10)
    linked = service.link_contact(chat_id=7, contact_id="contact-1")
    chat = service.chat(7)
    unlinked = service.unlink_contact(chat_id=7, contact_id="contact-1")

    assert contacts[0]["full_name"] == "Alex Appleseed"
    assert linked["contacts"][0]["manual"] == 1
    assert chat["contacts"][0]["contact_id"] == "contact-1"
    assert unlinked["contacts"] == []


def test_web_contacts_page_filters_substrings_and_pages_results(tmp_path: Path) -> None:
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.replace_contacts(
            contacts_from_json(
                [
                    {"id": "contact-1", "fullName": "Jane Doe"},
                    {"id": "contact-2", "fullName": "Jon Doe"},
                    {"id": "contact-3", "fullName": "Ada Lovelace"},
                ]
            )
        )

    service = _service(tmp_path)
    doe = service.contacts_page(limit=1, offset=1, query="oe")
    substring = service.contacts_page(limit=10, offset=0, query="on")

    assert doe["total"] == 2
    assert doe["items"][0]["full_name"] == "Jon Doe"
    assert doe["next_offset"] is None
    assert substring["items"][0]["full_name"] == "Jon Doe"


def test_web_business_research_requires_api_key(tmp_path: Path) -> None:
    with pytest.raises(WebAPIError, match="OPENAI_API_KEY") as error:
        _service(tmp_path).research_business_contact(
            name="Northwind Coffee",
            location="Seattle, WA",
        )

    assert error.value.status == 503


def test_web_business_research_returns_review_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResearcher:
        def __init__(self, *, api_key: str, model: str) -> None:
            assert api_key == "test-key"
            assert model == "gpt-5.5"

        def research(self, *, name: str, location: str) -> dict[str, object]:
            return {"business_name": name, "location": location, "candidate": {"fullName": name}}

    monkeypatch.setattr("agent.operator_service.OpenAIBusinessResearcher", FakeResearcher)
    service = WebService(
        config=_config(tmp_path, openai_api_key="test-key"),
        data_dir=tmp_path,
        db_path=tmp_path / "imessage.sqlite",
    )

    result = service.research_business_contact(name="Northwind Coffee", location="Seattle, WA")

    assert result["candidate"] == {"fullName": "Northwind Coffee"}


def test_web_chat_accepts_a_before_cursor(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    later = _message(rowid=101)
    later.date = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.upsert_message(later)

    payload = _service(tmp_path).chat(7, before="2026-05-04T00:00:00Z")

    assert [message["message_rowid"] for message in payload["messages"]] == [100]


def test_web_chat_pages_messages_by_oldest_rowid(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        for rowid in (101, 102, 103):
            message = _message(rowid=rowid)
            message.text = f"Message {rowid}"
            archive.upsert_message(message)

    service = _service(tmp_path)
    newest = service.chat(7, limit=2)
    older = service.chat(7, limit=2, before_rowid=int(newest["next_before_rowid"]))

    assert [message["message_rowid"] for message in newest["messages"]] == [102, 103]
    assert newest["has_more"] is True
    assert [message["message_rowid"] for message in older["messages"]] == [100, 101]
    assert older["has_more"] is False


def test_web_overview_prioritizes_favorite_recent_chat(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    favorite_chat = Chat(
        id=8,
        identifier="iMessage;-;+18015550102",
        name="Lesley",
        service="iMessage",
        last_message_at=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        participants=["+18015550102"],
    )
    favorite_message = Message(
        rowid=200,
        chat_id=8,
        guid="GUID-200",
        sender="me",
        text="Love you.",
        date=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        is_from_me=True,
        service="iMessage",
        has_attachments=False,
        chat_name="Lesley",
        participants=["+18015550102"],
    )
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.upsert_chat(favorite_chat)
        archive.upsert_message(favorite_message)
    MessageStore(tmp_path).write_chat_context(8, {"chat_id": 8, "favorite": True})

    payload = _service(tmp_path).overview(limit=5)

    assert payload["attention"][0]["chat_id"] == 8
    assert payload["attention"][0]["favorite"] is True
    assert _service(tmp_path).changes(since=str(payload["revision"]))["changed"] is False


def test_web_orbit_scores_two_way_contacts_and_pages_by_direction(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    earlier = _message(rowid=99)
    earlier.is_from_me = True
    earlier.sender = "me"
    earlier.text = "Thursday works."
    earlier.date = NOW.replace(hour=10)
    outgoing_chat = Chat(
        id=8,
        identifier="iMessage;-;+18015550102",
        name="Jordan",
        service="iMessage",
        last_message_at=NOW,
        participants=["+18015550102"],
    )
    outgoing = Message(
        rowid=200,
        chat_id=8,
        guid="GUID-200",
        sender="me",
        text="Checking in.",
        date=NOW,
        is_from_me=True,
        service="iMessage",
        has_attachments=False,
        chat_name="Jordan",
        participants=["+18015550102"],
    )
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.upsert_message(earlier)
        archive.upsert_chat(outgoing_chat)
        archive.upsert_message(outgoing)
        archive.replace_contacts(
            contacts_from_json([{"id": "contact-1", "fullName": "Alex", "phones": []}])
        )
        archive.link_chat_contact(7, "contact-1")

    service = _service(tmp_path)
    service.update_contact_importance("contact-1", 3)
    incoming = service.orbit(direction="incoming", days=0)
    outgoing_page = service.orbit(direction="outgoing", days=0)
    first_page = service.orbit(limit=1, offset=0, direction="either", days=0)
    second_page = service.orbit(limit=1, offset=1, direction="either", days=0)

    ranked = incoming["items"][0]
    assert ranked["chat_id"] == 7
    assert ranked["importance"] == 3
    assert ranked["two_way_score"] == 1
    assert ranked["orbit_score"] > ranked["score"]
    assert [row["chat_id"] for row in outgoing_page["items"]] == [8]
    assert first_page["total"] == 2
    assert first_page["next_offset"] == 1
    assert len(second_page["items"]) == 1


def test_web_mark_no_reply_hides_missing_pending_item(tmp_path: Path) -> None:
    _seed_archive(tmp_path)

    payload = _service(tmp_path).mark_no_reply(
        chat_id=7,
        source_rowid=100,
        reasoning="No response needed.",
    )

    assert payload["status"] == "no_reply_recorded"
    assert _service(tmp_path).pending(limit=5) == []


def test_web_update_chat_context_persists_fields_and_notes(tmp_path: Path) -> None:
    _seed_archive(tmp_path)

    payload = _service(tmp_path).update_chat_context(
        7,
        fields={
            "relationship": "close friend",
            "tone": "warm",
            "professional": False,
            "ignored": "nope",
        },
        notes="Met through the hiking group.",
    )

    context, notes = MessageStore(tmp_path).read_chat_context_document(7)
    assert payload["status"] == "saved"
    assert context["relationship"] == "close friend"
    assert context["tone"] == "warm"
    assert context["professional"] is False
    assert "ignored" not in context
    assert notes == "Met through the hiking group."


def test_web_update_chat_context_seeds_archive_identity_without_existing_context(
    tmp_path: Path,
) -> None:
    _seed_archive(tmp_path)

    payload = _service(tmp_path).update_chat_context(
        7,
        fields={
            "relationship": "friend",
            "tone": "casual",
            "professional": False,
            "auto_approve": False,
            "do_not_draft": False,
        },
        notes="",
    )

    context, notes = MessageStore(tmp_path).read_chat_context_document(7)
    assert payload["status"] == "saved"
    assert context["chat_id"] == 7
    assert context["name"] == "Alex"
    assert context["identifier"] == "iMessage;-;+18015550101"
    assert context["service"] == "iMessage"
    assert context["participants"] == ["+18015550101"]
    assert context["relationship"] == "friend"
    assert context["tone"] == "casual"
    assert context["professional"] is False
    assert context["auto_approve"] is False
    assert context["do_not_draft"] is False
    assert notes == ""


def test_web_update_chat_context_preserves_notes_when_notes_omitted(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    MessageStore(tmp_path).write_chat_context(
        7,
        {
            "chat_id": 7,
            "relationship": "friend",
            "notes": "Keep this note.",
        },
    )

    _service(tmp_path).update_chat_context(7, fields={"tone": "warmer"})

    context, notes = MessageStore(tmp_path).read_chat_context_document(7)
    assert context["relationship"] == "friend"
    assert context["tone"] == "warmer"
    assert notes == "Keep this note."


def test_web_operator_relationship_profile_is_editable(tmp_path: Path) -> None:
    service = _service(tmp_path)

    payload = service.update_operator_relationship(
        {
            "default_tone": "warm and direct",
            "self_presentation": "grounded and curious",
            "interpretation_style": "assume good intent but notice ambiguity",
            "communication_values": "clarity, care, and honest boundaries",
            "boundaries": "do not invent commitments",
        }
    )

    assert payload["operator"]["default_tone"] == "warm and direct"
    assert service.operator_relationship()["self_presentation"] == "grounded and curious"


def test_web_contact_relationship_profile_is_local_and_editable(tmp_path: Path) -> None:
    _seed_archive(tmp_path)
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.replace_contacts(
            contacts_from_json([{"id": "contact-1", "fullName": "Alex"}])
        )

    service = _service(tmp_path)
    saved = service.update_contact_relationship(
        "contact-1",
        fields={
            "relationship": "close friend",
            "tone": "playful",
            "self_presentation": "relaxed and present",
            "interpretation": "brief replies are normal, not dismissive",
            "professional": False,
        },
        notes="College roommate.",
    )
    contact = service.contact("contact-1")

    assert saved["profile"]["relationship"] == "close friend"
    assert saved["notes"] == "College roommate."
    assert contact["relationship_profile"]["interpretation"].startswith("brief replies")
    assert contact["relationship_notes"] == "College roommate."


def test_web_group_profile_inherits_only_linked_member_profiles(tmp_path: Path) -> None:
    group = _chat()
    group.is_group = True
    group.participants = ["+18015550101", "+18015550102"]
    message = _message()
    message.is_group = True
    message.participants = list(group.participants)
    with IMessageArchive(tmp_path / "imessage.sqlite") as archive:
        archive.upsert_chat(group)
        archive.upsert_message(message)
        archive.replace_contacts(
            contacts_from_json(
                [
                    {"id": "contact-1", "fullName": "Alex"},
                    {"id": "contact-2", "fullName": "Morgan"},
                    {"id": "operator-contact", "fullName": "Debbie"},
                    {"id": "outside", "fullName": "Outside Person"},
                ]
            )
        )
        archive.link_chat_contact(7, "contact-1")
        archive.link_chat_contact(7, "contact-2")
        archive.link_chat_contact(7, "operator-contact")

    service = _service(tmp_path)
    service.update_operator_profile({"contact_id": "operator-contact"})
    service.update_contact_relationship(
        "contact-1",
        fields={"relationship": "sibling", "interpretation": "uses dry humor"},
        notes="Family context.",
    )
    service.update_contact_relationship(
        "contact-2",
        fields={"relationship": "friend", "professional": True},
    )
    service.update_contact_relationship(
        "outside",
        fields={"relationship": "unrelated secret"},
    )
    saved = service.update_group_relationship(
        7,
        fields={
            "name": "Weekend crew",
            "purpose": "make plans without over-structuring",
            "tone": "light",
            "inherit_member_profiles": True,
        },
        notes="Alex and Morgan know each other well.",
    )
    effective = service.relationship_context(7)
    chat = service.chat(7)

    assert saved["profile"]["name"] == "Weekend crew"
    assert [member["contact_id"] for member in effective["members"]] == [
        "contact-1",
        "contact-2",
    ]
    assert "outside" not in str(effective)
    assert effective["safety"]["professional"] is True
    assert chat["group_profile"]["purpose"].startswith("make plans")
    assert len(chat["member_profiles"]) == 2

    service.update_group_relationship(
        7,
        fields={"inherit_member_profiles": False},
    )
    assert service.relationship_context(7)["members"] == []
    assert len(service.group_relationship(7)["members"]) == 2
    assert len(service.chat(7)["member_profiles"]) == 2
