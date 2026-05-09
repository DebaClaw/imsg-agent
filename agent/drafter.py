"""
drafter.py - Propose responses using the OpenAI Responses API.

Reads inbox files, builds per-chat context, calls an injectable drafting client,
and writes draft files to chats/{chatID}/drafts/.

This is the ONLY module that calls an external AI API.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .models import Draft, Message
from .store import MessageStore

logger = logging.getLogger(__name__)

PROMPT_VERSION = "draft_v1"
DEFAULT_MODEL = "gpt-5.5"


@dataclass(frozen=True)
class DraftResponse:
    proposed_text: str
    reasoning: str
    should_reply: bool = True


class DraftingClient(Protocol):
    async def create_draft(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
    ) -> DraftResponse:
        """Return a draft response from a model provider."""


class DraftingRateLimitError(RuntimeError):
    """Raised when the model provider asks the worker to slow down."""

    def __init__(self, message: str, *, retry_after_seconds: float = 60.0) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class DraftingQuotaError(RuntimeError):
    """Raised when the model provider reports no usable quota for drafting."""

    def __init__(self, message: str, *, retry_after_seconds: float = 3600.0) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class OpenAIResponsesDraftingClient:
    """Small adapter around OpenAI's Responses API."""

    def __init__(self, api_key: str | None = None) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - exercised only in runtime envs
            raise RuntimeError(
                "The openai package is required for drafting. Install project dependencies "
                "or run with a custom DraftingClient."
            ) from exc
        self._client = AsyncOpenAI(api_key=api_key)

    async def create_draft(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
    ) -> DraftResponse:
        try:
            response = await self._client.responses.create(
                model=model,
                instructions=instructions,
                input=input_text,
                text={"format": {"type": "json_object"}},
            )
        except Exception as exc:
            if _is_rate_limit_error(exc):
                if _openai_error_code(exc) == "insufficient_quota":
                    raise DraftingQuotaError(
                        "OpenAI quota exceeded while drafting; check project billing/quota",
                    ) from exc
                raise DraftingRateLimitError(
                    "OpenAI rate limit exceeded while drafting",
                    retry_after_seconds=_retry_after_seconds(exc),
                ) from exc
            raise
        return _parse_model_json(response.output_text)


class Drafter:
    def __init__(
        self,
        store: MessageStore,
        client: DraftingClient,
        *,
        default_model: str = DEFAULT_MODEL,
        max_inbox_age_hours: int = 48,
        auto_approve_default: bool = False,
        prompt_path: Path | None = None,
        now: datetime | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._default_model = default_model
        self._max_age = timedelta(hours=max_inbox_age_hours)
        self._auto_approve_default = auto_approve_default
        self._prompt_path = prompt_path or Path(__file__).parent / "prompts" / "draft_v1.txt"
        self._now = now

    async def run_pass(self) -> int:
        """Process all inbox files that do not already have a draft/outbox/archive."""
        count = 0
        for inbox_path in self._store.list_unprocessed_inbox():
            if await self.process_inbox(inbox_path) is not None:
                count += 1
        return count

    async def process_inbox(self, inbox_path: Path) -> Draft | None:
        message = self._store.read_inbox_message(inbox_path)
        if message is None:
            return None
        return await self.process_message(message)

    async def process_message(
        self,
        message: Message,
        *,
        history_override: str | None = None,
    ) -> Draft | None:
        context, context_body = self._store.read_chat_context_document(message.chat_id)
        history = (
            history_override
            if history_override is not None
            else self._store.read_chat_history(message.chat_id)
        )

        if self._store.draft_exists_for_source(message.chat_id, message.rowid):
            logger.debug(
                "Skipping rowid=%d chat_id=%d; draft already exists",
                message.rowid,
                message.chat_id,
            )
            return None
        if message.is_from_me:
            logger.debug("Skipping rowid=%d; message is from operator", message.rowid)
            return None
        if message.is_reaction:
            logger.debug("Skipping rowid=%d; message is a reaction", message.rowid)
            return None
        if not message.text.strip() and not message.has_attachments:
            logger.debug("Skipping rowid=%d; no text or attachments to respond to", message.rowid)
            return None
        if bool(context.get("do_not_draft")):
            logger.info(
                "Skipping rowid=%d chat_id=%d; do_not_draft=true",
                message.rowid,
                message.chat_id,
            )
            return None
        if self._is_group_chat(context) and context.get("do_not_draft") is not False:
            logger.info(
                "Skipping rowid=%d chat_id=%d; group chat requires do_not_draft=false opt-in",
                message.rowid,
                message.chat_id,
            )
            return None
        if self._is_too_old(message.date):
            logger.info(
                "Skipping rowid=%d chat_id=%d; message is older than max age",
                message.rowid,
                message.chat_id,
            )
            return None

        model = str(context.get("model") or self._default_model)
        auto_approved = self._should_auto_approve(context)
        created_at = self._current_time()
        draft_uuid = f"{created_at.strftime('%Y%m%dT%H%M%SZ')}-{message.rowid}"

        response = await self._client.create_draft(
            model=model,
            instructions=self._build_instructions(),
            input_text=self._build_input_text(
                context=context,
                context_body=context_body,
                history=history,
                new_message_text=message.text,
                source_rowid=message.rowid,
            ),
        )
        if not response.should_reply:
            self._store.write_no_reply_decision(
                uuid=draft_uuid,
                chat_id=message.chat_id,
                target_identifier=str(
                    context.get("identifier") or context.get("target_identifier") or ""
                ),
                source_rowid=message.rowid,
                created_at=created_at,
                reasoning=response.reasoning,
                model=model,
            )
            logger.info(
                "Recorded no-reply decision uuid=%s rowid=%d chat_id=%d",
                draft_uuid,
                message.rowid,
                message.chat_id,
            )
            return None

        draft = Draft(
            uuid=draft_uuid,
            chat_id=message.chat_id,
            target_identifier=str(
                context.get("identifier") or context.get("target_identifier") or ""
            ),
            created_at=created_at,
            proposed_text=response.proposed_text,
            reasoning=response.reasoning,
            prompt_version=PROMPT_VERSION,
            approved=auto_approved,
            source_rowid=message.rowid,
            model=model,
            auto_approved=auto_approved,
            service=message.service or str(context.get("service") or "auto"),
        )
        self._store.write_draft(draft)
        logger.info(
            "Wrote draft uuid=%s rowid=%d chat_id=%d auto_approved=%s",
            draft.uuid,
            message.rowid,
            message.chat_id,
            auto_approved,
        )
        return draft

    def _build_instructions(self) -> str:
        prompt = self._prompt_path.read_text(encoding="utf-8")
        return prompt.strip()

    def _build_input_text(
        self,
        *,
        context: dict[str, Any],
        context_body: str,
        history: str,
        new_message_text: str,
        source_rowid: int,
    ) -> str:
        structured_context = {
            key: context.get(key)
            for key in (
                "chat_id",
                "name",
                "service",
                "participants",
                "relationship",
                "tone",
                "professional",
                "auto_approve",
                "do_not_draft",
                "agent_notes",
            )
            if key in context
        }
        return "\n\n".join(
            [
                "CHAT CONTEXT FRONTMATTER\n"
                + json.dumps(structured_context, ensure_ascii=False, sort_keys=True),
                "CHAT CONTEXT NOTES\n" + (context_body.strip() or "(none)"),
                "CURRENT TIME\n" + self._current_time().isoformat().replace("+00:00", "Z"),
                "RECENT CHAT HISTORY\n" + (history.strip() or "(none)"),
                f"NEW MESSAGE rowid={source_rowid}\n{new_message_text.strip()}",
                (
                    "Return strict JSON only with keys should_reply, proposed_text, and "
                    "reasoning. should_reply is false when no reply is needed or when a "
                    "reply would no longer be logical. proposed_text is the exact iMessage "
                    "draft text when should_reply is true, or an empty string when false. "
                    "reasoning is a short private explanation for the operator."
                ),
            ]
        )

    def _should_auto_approve(self, context: dict[str, Any]) -> bool:
        requested = bool(context.get("auto_approve", self._auto_approve_default))
        # Unknown professional status is treated as professional for autonomous sends.
        professional = context.get("professional") is not False
        return (
            requested
            and not professional
            and not bool(context.get("do_not_draft"))
            and not self._is_group_chat(context)
        )

    def _is_group_chat(self, context: dict[str, Any]) -> bool:
        participants = context.get("participants")
        if isinstance(participants, list) and len(participants) > 1:
            return True
        identifier = str(context.get("identifier") or context.get("target_identifier") or "")
        return ";+;" in identifier

    def _is_too_old(self, message_date: datetime) -> bool:
        if self._max_age.total_seconds() <= 0:
            return False
        return self._current_time() - message_date.astimezone(UTC) > self._max_age

    def _current_time(self) -> datetime:
        return (self._now or datetime.now(UTC)).astimezone(UTC)


def _parse_model_json(raw_text: str) -> DraftResponse:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError("Drafting model did not return valid JSON") from exc
    should_reply = bool(data.get("should_reply", data.get("should_draft", True)))
    proposed_text = str(data.get("proposed_text") or "").strip()
    reasoning = str(data.get("reasoning") or "").strip()
    if should_reply and not proposed_text:
        raise ValueError("Drafting model returned empty proposed_text")
    if not reasoning:
        reasoning = "Model determined no reply was needed." if not should_reply else ""
    return DraftResponse(
        proposed_text=proposed_text,
        reasoning=reasoning,
        should_reply=should_reply,
    )


def _is_rate_limit_error(exc: Exception) -> bool:
    return int(getattr(exc, "status_code", 0) or 0) == 429


def _openai_error_code(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        code = body.get("code")
        if isinstance(code, str):
            return code
        error = body.get("error")
        if isinstance(error, dict):
            nested_code = error.get("code")
            if isinstance(nested_code, str):
                return nested_code
    response = getattr(exc, "response", None)
    try:
        payload = response.json() if response is not None else None
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str):
                return code
    return ""


def _retry_after_seconds(exc: Exception) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return 60.0
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(1.0, float(retry_after))
    except (TypeError, ValueError):
        return 60.0
