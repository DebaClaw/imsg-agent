from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, Unpack

from agent.reviewer import DraftReviewer, ReviewPolicy, default_policy_path
from agent.store import _parse_frontmatter, _write_frontmatter

NOW = datetime(2026, 7, 7, 6, 0, 0, tzinfo=UTC)
CUTOFF = "2026-07-07T05:00:00+00:00"


class ReviewPolicyKwargs(TypedDict, total=False):
    enabled: bool
    review_after: datetime | None
    max_draft_age_hours: float
    max_length: int
    max_approvals_per_chat_per_day: int
    max_approvals_per_day: int
    allow_urls: bool
    profanity: list[str]
    reveal_terms_ci: list[str]
    reveal_terms_cs: list[str]
    escalate_keywords: list[str]
    commitment_phrases: list[str]


def make_policy(**overrides: Unpack[ReviewPolicyKwargs]) -> ReviewPolicy:
    base: ReviewPolicyKwargs = {
        "enabled": True,
        "review_after": datetime.fromisoformat(CUTOFF),
        "max_draft_age_hours": 24.0,
        "max_length": 900,
        "max_approvals_per_chat_per_day": 5,
        "max_approvals_per_day": 20,
        "allow_urls": False,
        "profanity": ["fuck", "shit", "asshole"],
        "reveal_terms_ci": ["assistant", "chatbot", "language model", "automation", "drafted"],
        "reveal_terms_cs": ["AI", "Rue"],
        "escalate_keywords": ["hospital", "funeral", "lawyer", "loan"],
        "commitment_phrases": ["i'll be there", "i promise", "i'll pay"],
    }
    base.update(overrides)
    return ReviewPolicy(**base)


def write_context(data_dir: Path, chat_id: int, **fields: Any) -> None:
    meta: dict[str, Any] = {
        "chat_id": chat_id,
        "identifier": "+12065550100",
        "participants": ["+12065550100"],
        "service": "iMessage",
        "name": f"chat {chat_id}",
        "reviewer_delegate": True,
        "professional": False,
    }
    meta.update(fields)
    # None means "remove the key" so tests can exercise absent-field defaults.
    meta = {k: v for k, v in meta.items() if v is not None}
    chat_dir = data_dir / "chats" / str(chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    (chat_dir / "context.md").write_text(_write_frontmatter(meta, ""), encoding="utf-8")


def write_draft(
    data_dir: Path,
    chat_id: int,
    body: str,
    *,
    uuid: str = "20260707T053000Z-1000",
    created_at: str = "2026-07-07T05:30:00Z",
    approved: bool = False,
    extra_meta: dict[str, Any] | None = None,
) -> Path:
    meta: dict[str, Any] = {
        "uuid": uuid,
        "chat_id": chat_id,
        "target_identifier": "+12065550100",
        "created_at": created_at,
        "approved": approved,
        "prompt_version": "draft_v1",
        "reasoning": "test draft",
        "service": "iMessage",
        "source_rowid": 1000,
    }
    if extra_meta:
        meta.update(extra_meta)
    drafts_dir = data_dir / "chats" / str(chat_id) / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    path = drafts_dir / f"{uuid}.md"
    path.write_text(_write_frontmatter(meta, body), encoding="utf-8")
    return path


def run_reviewer(
    data_dir: Path,
    policy: ReviewPolicy | None = None,
    **kwargs: Any,
) -> dict[str, int]:
    reviewer = DraftReviewer(data_dir, policy or make_policy(), now=NOW, **kwargs)
    return reviewer.run_pass()


def read_meta(path: Path) -> dict[str, Any]:
    meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
    return meta


# ---------------------------------------------------------------------------
# Happy path & delegation
# ---------------------------------------------------------------------------


def test_clean_draft_in_delegated_chat_is_approved(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "Sounds good, see you soon!")
    counts = run_reviewer(tmp_path)
    meta = read_meta(path)
    assert counts["approved"] == 1
    assert meta["approved"] is True
    assert meta["review_status"] == "approved"
    assert meta["reviewer"] == "review_v1"
    assert "[reviewer] approved" in meta["reasoning"]


def test_undelegated_chat_is_held(tmp_path: Path) -> None:
    write_context(tmp_path, 9, reviewer_delegate=None)
    path = write_draft(tmp_path, 9, "Sounds good!")
    counts = run_reviewer(tmp_path)
    meta = read_meta(path)
    assert counts["held"] == 1
    assert meta["approved"] is False
    assert "not delegated" in meta["review_reason"]


def test_drafter_auto_approve_flag_does_not_delegate_reviewer(tmp_path: Path) -> None:
    # auto_approve is the drafter's creation-time flag; it must NOT satisfy the reviewer.
    write_context(tmp_path, 9, reviewer_delegate=None, auto_approve=True)
    path = write_draft(tmp_path, 9, "Sounds good!")
    run_reviewer(tmp_path)
    assert read_meta(path)["approved"] is False


def test_professional_chat_escalates(tmp_path: Path) -> None:
    write_context(tmp_path, 9, professional=True)
    path = write_draft(tmp_path, 9, "Sounds good!")
    counts = run_reviewer(tmp_path)
    assert counts["escalated"] == 1
    assert read_meta(path)["approved"] is False


def test_unknown_professional_status_is_held(tmp_path: Path) -> None:
    write_context(tmp_path, 9, professional=None)
    path = write_draft(tmp_path, 9, "Sounds good!")
    counts = run_reviewer(tmp_path)
    assert counts["held"] == 1
    assert "professional status unknown" in read_meta(path)["review_reason"]


def test_group_chat_escalates_without_group_delegation(tmp_path: Path) -> None:
    write_context(tmp_path, 9, participants=["+12065550100", "+12065550101"])
    path = write_draft(tmp_path, 9, "Sounds good!")
    counts = run_reviewer(tmp_path)
    assert counts["escalated"] == 1
    assert "group chat" in read_meta(path)["review_reason"]


def test_delegated_group_chat_is_approved(tmp_path: Path) -> None:
    write_context(
        tmp_path, 9, participants=["+12065550100", "+12065550101"], group_delegated=True
    )
    path = write_draft(tmp_path, 9, "Sounds good!")
    counts = run_reviewer(tmp_path)
    assert counts["approved"] == 1
    assert read_meta(path)["approved"] is True


def test_chat_without_participants_is_held(tmp_path: Path) -> None:
    # The chat-0 misrouting class: no identifier, no participants.
    write_context(tmp_path, 0, identifier="", participants=[])
    path = write_draft(tmp_path, 0, "Sounds good!")
    counts = run_reviewer(tmp_path)
    assert counts["held"] == 1
    assert "participants" in read_meta(path)["review_reason"]


# ---------------------------------------------------------------------------
# Freshness & backlog
# ---------------------------------------------------------------------------


def test_stale_draft_is_held(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(
        tmp_path,
        9,
        "Sounds good!",
        uuid="20260707T051000Z-999",
        created_at="2026-07-05T05:10:00Z",  # >24h before NOW but uuid after cutoff
    )
    counts = run_reviewer(tmp_path)
    assert counts["held"] == 1
    assert "older than" in read_meta(path)["review_reason"]


def test_backlog_drafts_before_cutoff_are_untouched(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(
        tmp_path,
        9,
        "Old backlog draft",
        uuid="20260501T120000Z-500",
        created_at="2026-05-01T12:00:00Z",
    )
    before = path.read_text(encoding="utf-8")
    counts = run_reviewer(tmp_path)
    assert counts == {"approved": 0, "held": 0, "escalated": 0, "skipped": 0}
    assert path.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Content gates
# ---------------------------------------------------------------------------


def test_profanity_is_held_by_default(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "That's fucking wild!")
    counts = run_reviewer(tmp_path)
    assert counts["held"] == 1
    assert "profanity" in read_meta(path)["review_reason"]


def test_profanity_allowed_with_chat_override(tmp_path: Path) -> None:
    write_context(tmp_path, 9, allow_profanity=True)
    path = write_draft(tmp_path, 9, "That's fucking wild!")
    counts = run_reviewer(tmp_path)
    assert counts["approved"] == 1
    assert read_meta(path)["approved"] is True


def test_system_reveal_term_is_held(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "My AI helped me write this")
    counts = run_reviewer(tmp_path)
    assert counts["held"] == 1
    assert "references the system" in read_meta(path)["review_reason"]


def test_lowercase_rue_word_is_not_a_reveal(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "You'll rue the day you bet against the Mariners")
    counts = run_reviewer(tmp_path)
    assert counts["approved"] == 1
    assert read_meta(path)["approved"] is True


def test_escalation_keyword_escalates_and_logs(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "How did it go at the hospital?")
    counts = run_reviewer(tmp_path)
    assert counts["escalated"] == 1
    assert read_meta(path)["approved"] is False
    escalations = (tmp_path / "review-queue" / "escalations.md").read_text(encoding="utf-8")
    assert "chat 9" in escalations
    assert "hospital" in escalations


def test_chat_alert_on_keywords_escalate(tmp_path: Path) -> None:
    write_context(tmp_path, 9, alert_on=["mariners"])
    path = write_draft(tmp_path, 9, "Did you catch the Mariners game?")
    counts = run_reviewer(tmp_path)
    assert counts["escalated"] == 1
    assert read_meta(path)["approved"] is False


def test_chat_alert_on_as_comma_string_escalates(tmp_path: Path) -> None:
    # context.template.md documents alert_on as a comma-separated string.
    write_context(tmp_path, 9, alert_on="family conflict, mariners, money stuff")
    path = write_draft(tmp_path, 9, "Did you catch the Mariners game?")
    counts = run_reviewer(tmp_path)
    assert counts["escalated"] == 1
    assert read_meta(path)["approved"] is False


def test_alert_on_string_does_not_match_per_character(tmp_path: Path) -> None:
    # A prose alert_on string must not degrade into single-letter matches.
    write_context(tmp_path, 9, alert_on="anything about the divorce")
    path = write_draft(tmp_path, 9, "Sounds good, see you soon!")
    counts = run_reviewer(tmp_path)
    assert counts["approved"] == 1
    assert read_meta(path)["approved"] is True


def test_money_amount_escalates(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "Sure, $200 works for me")
    counts = run_reviewer(tmp_path)
    assert counts["escalated"] == 1
    assert "money" in read_meta(path)["review_reason"]


def test_commitment_phrase_escalates(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "I'll be there at 6 tomorrow")
    counts = run_reviewer(tmp_path)
    assert counts["escalated"] == 1
    assert "commits Jon" in read_meta(path)["review_reason"]


def test_url_escalates(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "Check out https://example.com")
    counts = run_reviewer(tmp_path)
    assert counts["escalated"] == 1
    assert "URL" in read_meta(path)["review_reason"]


def test_empty_body_is_held(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "   ")
    counts = run_reviewer(tmp_path)
    assert counts["held"] == 1
    assert "empty" in read_meta(path)["review_reason"]


def test_overlong_body_is_held(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "x" * 1000)
    counts = run_reviewer(tmp_path)
    assert counts["held"] == 1
    assert "longer than" in read_meta(path)["review_reason"]


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


def test_per_chat_daily_rate_limit(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    policy = make_policy(max_approvals_per_chat_per_day=2)
    paths = [
        write_draft(
            tmp_path,
            9,
            f"Sounds good {i}!",
            uuid=f"20260707T0530{i:02d}Z-{1000 + i}",
        )
        for i in range(3)
    ]
    counts = run_reviewer(tmp_path, policy)
    assert counts["approved"] == 2
    assert counts["held"] == 1
    metas = [read_meta(p) for p in paths]
    assert sum(1 for m in metas if m["approved"]) == 2


def test_global_daily_rate_limit_spans_chats(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    write_context(tmp_path, 10)
    policy = make_policy(max_approvals_per_day=1)
    write_draft(tmp_path, 9, "Sounds good!", uuid="20260707T053001Z-1001")
    write_draft(tmp_path, 10, "Sounds good!", uuid="20260707T053002Z-1002")
    counts = run_reviewer(tmp_path, policy)
    assert counts["approved"] == 1
    assert counts["held"] == 1


def test_rate_limit_state_persists_across_passes(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    policy = make_policy(max_approvals_per_chat_per_day=1)
    write_draft(tmp_path, 9, "First!", uuid="20260707T053001Z-1001")
    assert run_reviewer(tmp_path, policy)["approved"] == 1
    write_draft(tmp_path, 9, "Second!", uuid="20260707T053002Z-1002")
    counts = run_reviewer(tmp_path, policy)
    assert counts["approved"] == 0
    assert counts["held"] == 1


# ---------------------------------------------------------------------------
# Fail-closed & idempotency
# ---------------------------------------------------------------------------


def test_disabled_policy_reviews_nothing(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "Sounds good!")
    counts = run_reviewer(tmp_path, make_policy(enabled=False))
    assert counts == {"approved": 0, "held": 0, "escalated": 0, "skipped": 0}
    assert read_meta(path)["approved"] is False


def test_missing_policy_file_fails_closed(tmp_path: Path) -> None:
    policy = ReviewPolicy.load(default_policy_path(tmp_path))
    assert policy.enabled is False


def test_malformed_policy_file_fails_closed(tmp_path: Path) -> None:
    policy_path = tmp_path / "review-policy.json"
    policy_path.write_text("{not json", encoding="utf-8")
    assert ReviewPolicy.load(policy_path).enabled is False


def test_policy_without_review_after_fails_closed(tmp_path: Path) -> None:
    policy_path = tmp_path / "review-policy.json"
    policy_path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    assert ReviewPolicy.load(policy_path).enabled is False


def test_already_reviewed_draft_is_skipped(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "That's fucking wild!")
    assert run_reviewer(tmp_path)["held"] == 1
    first = path.read_text(encoding="utf-8")
    counts = run_reviewer(tmp_path)
    assert counts["skipped"] == 1
    assert counts["held"] == 0
    assert path.read_text(encoding="utf-8") == first


def test_already_approved_draft_is_skipped(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    write_draft(tmp_path, 9, "Sounds good!", approved=True)
    counts = run_reviewer(tmp_path)
    assert counts["skipped"] == 1
    assert counts["approved"] == 0


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    path = write_draft(tmp_path, 9, "Sounds good!")
    before = path.read_text(encoding="utf-8")
    counts = run_reviewer(tmp_path, dry_run=True)
    assert counts["approved"] == 1
    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "review-state.json").exists()


def test_unparseable_draft_is_skipped_not_crashed(tmp_path: Path) -> None:
    write_context(tmp_path, 9)
    drafts_dir = tmp_path / "chats" / "9" / "drafts"
    drafts_dir.mkdir(parents=True)
    (drafts_dir / "20260707T053000Z-1000.md").write_text("no frontmatter", encoding="utf-8")
    counts = run_reviewer(tmp_path)
    assert counts["skipped"] == 1
