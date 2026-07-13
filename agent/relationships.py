"""Relationship-profile composition for operator, contacts, groups, and chats."""
from __future__ import annotations

from typing import Any

from .archive_store import IMessageArchive
from .store import MessageStore

PROFILE_FIELDS = {
    "relationship",
    "tone",
    "self_presentation",
    "interpretation",
    "communication_preferences",
    "boundaries",
    "agent_notes",
    "professional",
    "do_not_draft",
}

OPERATOR_RELATIONSHIP_FIELDS = {
    "default_tone",
    "self_presentation",
    "interpretation_style",
    "communication_values",
    "boundaries",
    "relationship_notes",
}

GROUP_PROFILE_FIELDS = PROFILE_FIELDS | {
    "name",
    "purpose",
    "dynamics",
    "inherit_member_profiles",
}


def linked_member_relationships(
    *,
    store: MessageStore,
    archive: IMessageArchive,
    chat_id: int,
) -> list[dict[str, Any]]:
    """Return editable profiles only for contacts confirmed in the selected chat."""
    operator_contact_id = str(store.read_operator_profile().get("contact_id") or "")
    members: list[dict[str, Any]] = []
    for contact in archive.confirmed_chat_contacts(chat_id):
        contact_id = str(contact.get("contact_id") or "")
        if not contact_id or contact_id == operator_contact_id:
            continue
        profile, notes = store.read_contact_relationship_document(contact_id)
        members.append(
            {
                "contact_id": contact_id,
                "name": str(contact.get("full_name") or contact_id),
                "profile": profile,
                "notes": notes,
            }
        )
    return members


def effective_relationship_context(
    *,
    store: MessageStore,
    archive: IMessageArchive,
    chat_id: int,
) -> dict[str, Any]:
    """Compose only the selected chat's operator, member, group, and chat context."""
    conversation, conversation_notes = store.read_chat_context_document(chat_id)
    seed = archive.chat_context_seed(chat_id)
    is_group = bool(seed.get("is_group") or conversation.get("is_group"))
    group, group_notes = (
        store.read_group_relationship_document(chat_id) if is_group else ({}, "")
    )
    inherit_members = not is_group or group.get("inherit_member_profiles") is not False
    members = (
        linked_member_relationships(store=store, archive=archive, chat_id=chat_id)
        if inherit_members
        else []
    )

    operator = {
        key: value
        for key, value in store.read_operator_profile().items()
        if key in OPERATOR_RELATIONSHIP_FIELDS and value not in (None, "", [])
    }
    profile_sources = [member["profile"] for member in members]
    if group:
        profile_sources.append(group)
    safety = {
        "do_not_draft": any(bool(profile.get("do_not_draft")) for profile in profile_sources),
        "professional": any(bool(profile.get("professional")) for profile in profile_sources),
    }
    return {
        "chat_id": chat_id,
        "is_group": is_group,
        "precedence": [
            "operator defaults",
            "linked member profiles",
            "group profile",
            "conversation context",
        ],
        "operator": operator,
        "members": members,
        "group": group,
        "group_notes": group_notes,
        "conversation": conversation,
        "conversation_notes": conversation_notes,
        "safety": safety,
    }
