"""
Core chatbot logic — wraps the Claude API and orchestrates session management,
segment routing, and escalation detection.
"""

import os
import re
import logging
from typing import AsyncIterator

import anthropic

from .session import Session, get_or_create
from .system_prompt import build_system_prompt
from .escalation import parse_escalation, strip_escalation_tag, fire_escalation

logger = logging.getLogger(__name__)

_SEGMENT_KEYWORDS = {
    "A": {"a", "exploring", "prospect", "first time", "new to round", "1"},
    "B": {"b", "new client", "getting set up", "onboarding", "setting up", "just signed up", "2"},
    "C": {"c", "existing client", "established", "already using", "current client", "3"},
}

_RESET_KEYWORDS = {"main menu", "start over", "restart", "back to menu", "menu"}

MODEL = "claude-opus-4-7"


def _detect_segment(text: str) -> str | None:
    lowered = text.lower().strip()
    for segment, keywords in _SEGMENT_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return segment
    return None


def _is_reset(text: str) -> bool:
    return any(kw in text.lower() for kw in _RESET_KEYWORDS)


async def chat_stream(
    session_id: str | None,
    user_message: str,
) -> AsyncIterator[tuple[str, Session]]:
    """
    Yields (text_chunk, session) pairs as Claude streams its response.
    The final chunk always yields ("", session) so callers can inspect the
    completed session state (including any escalation that was fired).
    """
    session = get_or_create(session_id)
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    if _is_reset(user_message):
        session.set_segment(None)
        session.reset_attempts()

    if session.segment is None:
        detected = _detect_segment(user_message)
        if detected:
            session.set_segment(detected)

    session.add_user_message(user_message)

    system_prompt = build_system_prompt(session.segment)

    full_response = ""
    async with client.messages.stream(
        model=MODEL,
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=system_prompt,
        messages=session.messages,
    ) as stream:
        async for text in stream.text_stream:
            full_response += text
            yield text, session

    # Post-process: detect and fire escalation, strip tag from stored response
    escalation_event = parse_escalation(full_response)
    clean_response = strip_escalation_tag(full_response)

    if escalation_event and not session.escalated:
        session.escalated = True
        _update_contact_from_event(session, escalation_event)
        await fire_escalation(escalation_event, session)

    session.add_assistant_message(clean_response)
    session.reset_attempts()

    # Emit a terminal sentinel with the updated session state
    yield "", session


def _update_contact_from_event(session: Session, event) -> None:
    session.update_contact(
        name=None if event.name in ("UNKNOWN", "") else event.name,
        company=None if event.company in ("UNKNOWN", "") else event.company,
        email=None if event.email in ("UNKNOWN", "") else event.email,
    )


async def chat_simple(
    session_id: str | None,
    user_message: str,
) -> tuple[str, Session]:
    """Non-streaming version for simple integrations."""
    full_text = ""
    final_session = None
    async for chunk, session in chat_stream(session_id, user_message):
        full_text += chunk
        final_session = session
    return full_text, final_session
