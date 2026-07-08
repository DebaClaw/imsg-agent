"""
reviewer.py - Automated draft review with deterministic safety gates.

Fills the gap between the drafter (writes drafts with approved: false) and the
sender (sends anything with approved: true). A draft is approved only when the
chat is explicitly delegated AND every safety gate passes. Everything else is
held or escalated with a written reason. The engine is fail-closed: a missing,
malformed, or disabled policy file approves nothing.

Delegation is per chat, in chats/{id}/context.md frontmatter:

    reviewer_delegate: true   # required — opt this chat into automated review
    professional: false       # required — unknown professional status blocks approval
    allow_profanity: true     # optional — waive the profanity gate for this chat
    group_delegated: true     # optional — allow a delegated group chat
    alert_on: [health, money] # optional — extra escalation keywords for this chat

`reviewer_delegate` is intentionally distinct from `auto_approve`, which the
drafter consumes to approve at creation time with no review at all.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeAlias, cast

from dotenv import load_dotenv

from .store import MessageStore, _parse_frontmatter, _write_frontmatter

logger = logging.getLogger(__name__)

REVIEWER_VERSION = "review_v1"

PASS = "pass"
HOLD = "hold"
ESCALATE = "escalate"
APPROVE = "approve"

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_MONEY_RE = re.compile(r"[$€£]\s?\d|\b\d+\s?(dollars|bucks|USD)\b", re.IGNORECASE)
_UUID_TS_RE = re.compile(r"^(\d{8}T\d{6}Z)")

Meta: TypeAlias = dict[str, Any]
ReviewCounts: TypeAlias = dict[str, int]
ReviewState: TypeAlias = dict[str, Any]


@dataclass
class ReviewPolicy:
    enabled: bool = False
    review_after: datetime | None = None
    max_draft_age_hours: float = 24.0
    max_length: int = 900
    max_approvals_per_chat_per_day: int = 5
    max_approvals_per_day: int = 20
    allow_urls: bool = False
    profanity: list[str] = field(default_factory=list)
    reveal_terms_ci: list[str] = field(default_factory=list)
    reveal_terms_cs: list[str] = field(default_factory=list)
    escalate_keywords: list[str] = field(default_factory=list)
    commitment_phrases: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> ReviewPolicy:
        """Load policy from JSON. Any problem returns a disabled (fail-closed) policy."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Review policy unavailable at %s (%s); reviewer disabled", path, exc)
            return cls(enabled=False)
        try:
            review_after_raw = data.get("review_after")
            review_after = (
                datetime.fromisoformat(str(review_after_raw).replace("Z", "+00:00"))
                if review_after_raw
                else None
            )
            if review_after is None:
                logger.warning("Review policy missing review_after; reviewer disabled")
                return cls(enabled=False)
            return cls(
                enabled=bool(data.get("enabled", False)),
                review_after=review_after,
                max_draft_age_hours=float(data.get("max_draft_age_hours", 24.0)),
                max_length=int(data.get("max_length", 900)),
                max_approvals_per_chat_per_day=int(
                    data.get("max_approvals_per_chat_per_day", 5)
                ),
                max_approvals_per_day=int(data.get("max_approvals_per_day", 20)),
                allow_urls=bool(data.get("allow_urls", False)),
                profanity=[str(w).lower() for w in data.get("profanity", [])],
                reveal_terms_ci=[str(w).lower() for w in data.get("reveal_terms_ci", [])],
                reveal_terms_cs=[str(w) for w in data.get("reveal_terms_cs", [])],
                escalate_keywords=[str(w).lower() for w in data.get("escalate_keywords", [])],
                commitment_phrases=[
                    str(w).lower() for w in data.get("commitment_phrases", [])
                ],
            )
        except Exception as exc:
            logger.warning("Review policy at %s malformed (%s); reviewer disabled", path, exc)
            return cls(enabled=False)


@dataclass
class GateResult:
    gate: str
    outcome: str  # PASS | HOLD | ESCALATE
    detail: str = ""


@dataclass
class ReviewDecision:
    decision: str  # APPROVE | HOLD | ESCALATE
    reason: str
    gates: list[GateResult]


def _word_present(
    text: str, term: str, *, case_sensitive: bool = False, allow_suffix: bool = False
) -> bool:
    flags = 0 if case_sensitive else re.IGNORECASE
    trailing = "" if allow_suffix else r"(?![\w'])"
    return re.search(rf"(?<![\w']){re.escape(term)}{trailing}", text, flags) is not None


def _alert_keywords(raw: object) -> list[str]:
    """context.template.md documents alert_on as a comma-separated string; older
    COMMS.md guidance uses a list. Accept both; never iterate a string per-char."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip().lower() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(w).strip().lower() for w in raw if str(w).strip()]
    return []


def _meta_chat_id(meta: Meta) -> int | None:
    try:
        return int(meta["chat_id"])
    except (KeyError, TypeError, ValueError):
        return None


class DraftReviewer:
    def __init__(
        self,
        data_dir: Path,
        policy: ReviewPolicy,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> None:
        self._data_dir = data_dir
        self._store = MessageStore(data_dir)
        self._policy = policy
        self._now = now or datetime.now(UTC)
        self._dry_run = dry_run
        self._state_path = data_dir / "review-state.json"
        self._escalations_path = data_dir / "review-queue" / "escalations.md"
        self._state = self._load_state()

    # ------------------------------------------------------------------
    # State (daily rate-limit counters)
    # ------------------------------------------------------------------

    def _load_state(self) -> ReviewState:
        today = self._now.strftime("%Y-%m-%d")
        try:
            loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            loaded = {}
        state = cast(ReviewState, loaded) if isinstance(loaded, dict) else {}
        if state.get("day") != today:
            state = {"day": today, "total": 0, "per_chat": {}}
        state.setdefault("total", 0)
        state.setdefault("per_chat", {})
        return state

    def _save_state(self) -> None:
        if self._dry_run:
            return
        self._state_path.write_text(
            json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8"
        )

    def _approvals_remaining(self, chat_id: int) -> bool:
        if self._state["total"] >= self._policy.max_approvals_per_day:
            return False
        per_chat = int(self._state["per_chat"].get(str(chat_id), 0))
        return per_chat < self._policy.max_approvals_per_chat_per_day

    def _record_approval(self, chat_id: int) -> None:
        self._state["total"] += 1
        key = str(chat_id)
        self._state["per_chat"][key] = int(self._state["per_chat"].get(key, 0)) + 1
        self._save_state()

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def evaluate(self, meta: Meta, body: str, context: Meta) -> ReviewDecision:
        """Run every gate; the worst outcome wins (escalate > hold > approve)."""
        gates: list[GateResult] = []

        def gate(name: str, outcome: str, detail: str = "") -> None:
            gates.append(GateResult(name, outcome, detail))

        # --- Eligibility -------------------------------------------------
        if context.get("reviewer_delegate") is not True:
            gate("delegated", HOLD, "chat is not delegated (reviewer_delegate is not true)")
        else:
            gate("delegated", PASS)

        professional = context.get("professional")
        if professional is True:
            gate("professional", ESCALATE, "professional chat always requires Jon")
        elif professional is not False:
            gate(
                "professional",
                HOLD,
                "professional status unknown; set professional: false to delegate",
            )
        else:
            gate("professional", PASS)

        participants = context.get("participants") or []
        identifier = str(context.get("identifier") or "").strip()
        if not participants or not identifier:
            gate("identity", HOLD, "chat has no participants/identifier (misrouted draft)")
        else:
            gate("identity", PASS)

        is_group = bool(context.get("is_group")) or len(participants) > 1
        if is_group and context.get("group_delegated") is not True:
            gate("group", ESCALATE, "group chat without explicit group_delegated: true")
        else:
            gate("group", PASS)

        # --- Freshness ---------------------------------------------------
        created_at = _parse_created_at(meta)
        if created_at is None:
            gate("freshness", HOLD, "draft has no parseable created_at")
        elif self._now - created_at > timedelta(hours=self._policy.max_draft_age_hours):
            gate(
                "freshness",
                HOLD,
                f"draft older than {self._policy.max_draft_age_hours}h; conversation moved on",
            )
        else:
            gate("freshness", PASS)

        # --- Content -----------------------------------------------------
        text = body.strip()
        if not text:
            gate("body", HOLD, "empty draft body")
        elif len(text) > self._policy.max_length:
            gate("body", HOLD, f"draft longer than {self._policy.max_length} chars")
        else:
            gate("body", PASS)

        if text and not self._policy.allow_urls and _URL_RE.search(text):
            gate("urls", ESCALATE, "draft contains a URL")
        else:
            gate("urls", PASS)

        reveal_hit = next(
            (
                t
                for t in self._policy.reveal_terms_cs
                if _word_present(text, t, case_sensitive=True)
            ),
            None,
        ) or next(
            (t for t in self._policy.reveal_terms_ci if _word_present(text, t)),
            None,
        )
        if reveal_hit:
            gate("reveal", HOLD, f"draft references the system ({reveal_hit!r})")
        else:
            gate("reveal", PASS)

        # Stem match so "fuck" catches "fucking"/"fucked"; list entries should be stems.
        profanity_hit = next(
            (w for w in self._policy.profanity if _word_present(text, w, allow_suffix=True)),
            None,
        )
        if profanity_hit and context.get("allow_profanity") is not True:
            gate(
                "profanity",
                HOLD,
                f"profanity ({profanity_hit!r}) and chat has no allow_profanity override",
            )
        else:
            gate("profanity", PASS)

        chat_alerts = _alert_keywords(context.get("alert_on"))
        alert_hit = next(
            (
                w
                for w in (*self._policy.escalate_keywords, *chat_alerts)
                if _word_present(text, w)
            ),
            None,
        )
        if alert_hit:
            gate("alerts", ESCALATE, f"sensitive topic ({alert_hit!r})")
        elif _MONEY_RE.search(text):
            gate("alerts", ESCALATE, "draft mentions a money amount")
        else:
            gate("alerts", PASS)

        commitment_hit = next(
            (p for p in self._policy.commitment_phrases if p in text.lower()), None
        )
        if commitment_hit:
            gate("commitments", ESCALATE, f"draft commits Jon ({commitment_hit!r})")
        else:
            gate("commitments", PASS)

        # --- Rate limits ---------------------------------------------------
        chat_id = _meta_chat_id(meta)
        if chat_id is None or not self._approvals_remaining(chat_id):
            gate("rate_limit", HOLD, "daily auto-approval limit reached")
        else:
            gate("rate_limit", PASS)

        if any(g.outcome == ESCALATE for g in gates):
            worst = next(g for g in gates if g.outcome == ESCALATE)
            return ReviewDecision(ESCALATE, f"{worst.gate}: {worst.detail}", gates)
        if any(g.outcome == HOLD for g in gates):
            worst = next(g for g in gates if g.outcome == HOLD)
            return ReviewDecision(HOLD, f"{worst.gate}: {worst.detail}", gates)
        return ReviewDecision(APPROVE, f"all {len(gates)} gates passed", gates)

    # ------------------------------------------------------------------
    # Scan & writeback
    # ------------------------------------------------------------------

    def run_pass(self) -> ReviewCounts:
        """Review every eligible pending draft once. Returns outcome counts."""
        counts = {"approved": 0, "held": 0, "escalated": 0, "skipped": 0}
        if not self._policy.enabled or self._policy.review_after is None:
            logger.info("Reviewer disabled by policy; nothing reviewed")
            return counts

        for path in self._pending_draft_paths():
            try:
                meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Skipping unparseable draft %s: %s", path, exc)
                counts["skipped"] += 1
                continue
            if meta.get("approved") or meta.get("review_status"):
                counts["skipped"] += 1
                continue
            chat_id = _meta_chat_id(meta)
            if chat_id is None or not meta.get("uuid"):
                logger.warning("Skipping draft with missing uuid/chat_id: %s", path)
                counts["skipped"] += 1
                continue

            context, _ = self._store.read_chat_context_document(chat_id)
            decision = self.evaluate(meta, body, context)

            if decision.decision == APPROVE:
                # Burn quota before flipping the bit: a crash between the two
                # can only under-approve, never over-approve.
                self._record_approval(chat_id)
                self._write_decision(path, meta, body, "approved", decision, approved=True)
                counts["approved"] += 1
            elif decision.decision == ESCALATE:
                self._write_decision(path, meta, body, "escalate", decision, approved=False)
                self._append_escalation(meta, decision)
                counts["escalated"] += 1
            else:
                self._write_decision(path, meta, body, "hold", decision, approved=False)
                counts["held"] += 1
            logger.info(
                "Reviewed draft uuid=%s chat_id=%s decision=%s (%s)",
                meta.get("uuid"),
                chat_id,
                decision.decision,
                decision.reason,
            )
        return counts

    def _pending_draft_paths(self) -> list[Path]:
        """Drafts created at/after policy.review_after, cheap-filtered by uuid timestamp."""
        chats_dir = self._data_dir / "chats"
        if not chats_dir.exists():
            return []
        cutoff = self._policy.review_after
        assert cutoff is not None
        out: list[Path] = []
        for path in sorted(chats_dir.glob("*/drafts/*.md")):
            m = _UUID_TS_RE.match(path.name)
            if not m:
                continue
            try:
                stamp = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            except ValueError:
                continue
            if stamp >= cutoff:
                out.append(path)
        return out

    def _write_decision(
        self,
        path: Path,
        meta: Meta,
        body: str,
        status: str,
        decision: ReviewDecision,
        *,
        approved: bool,
    ) -> None:
        if self._dry_run:
            return
        meta = dict(meta)
        meta["approved"] = approved
        meta["review_status"] = status
        meta["review_reason"] = decision.reason
        meta["reviewed_at"] = self._now.strftime("%Y-%m-%dT%H:%M:%SZ")
        meta["reviewer"] = REVIEWER_VERSION
        existing = str(meta.get("reasoning") or "").strip()
        note = f"[reviewer] {status}: {decision.reason}"
        meta["reasoning"] = f"{existing}\n{note}".strip() if existing else note
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(_write_frontmatter(meta, body), encoding="utf-8")
        os.replace(tmp, path)

    def _append_escalation(self, meta: Meta, decision: ReviewDecision) -> None:
        if self._dry_run:
            return
        self._escalations_path.parent.mkdir(parents=True, exist_ok=True)
        line = (
            f"- {self._now.strftime('%Y-%m-%dT%H:%M:%SZ')} chat {meta.get('chat_id')} "
            f"draft {meta.get('uuid')}: {decision.reason}\n"
        )
        with open(self._escalations_path, "a", encoding="utf-8") as fh:
            fh.write(line)


def _parse_created_at(meta: Meta) -> datetime | None:
    raw = meta.get("created_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def default_policy_path(data_dir: Path) -> Path:
    return data_dir / "review-policy.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review pending drafts against safety gates.")
    parser.add_argument("--data-dir", help="imsg data dir (default: ~/imsg-data)")
    parser.add_argument(
        "--policy", help="Policy JSON path (default: {data-dir}/review-policy.json)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Evaluate but write nothing")
    return parser


def cli() -> None:
    load_dotenv()
    args = _parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    data_dir = (
        Path(args.data_dir).expanduser() if args.data_dir else Path("~/imsg-data").expanduser()
    )
    policy_path = Path(args.policy).expanduser() if args.policy else default_policy_path(data_dir)
    policy = ReviewPolicy.load(policy_path)
    reviewer = DraftReviewer(data_dir, policy, dry_run=bool(args.dry_run))
    counts = reviewer.run_pass()
    print(
        f"reviewed: approved={counts['approved']} held={counts['held']} "
        f"escalated={counts['escalated']} skipped={counts['skipped']}"
        + (" (dry-run)" if args.dry_run else "")
    )


if __name__ == "__main__":
    cli()
