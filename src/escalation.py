"""
Escalation engine — parses escalation tags from Claude's output and fires
Slack webhook + (optionally) email notifications to the human rep.
"""

import os
import re
import logging
from dataclasses import dataclass
from typing import Literal

import httpx

from .session import Session

logger = logging.getLogger(__name__)

Priority = Literal["HIGH", "NORMAL"]

_TAG_RE = re.compile(
    r"\[ESCALATE\|reason=([^|]+)\|priority=(HIGH|NORMAL)\|name=([^|]+)\|company=([^|]+)\|email=([^\]]+)\]"
)

_PRIORITY_EMOJI = {"HIGH": "🔴", "NORMAL": "🟡"}

_SEGMENT_LABELS = {
    "A": "Prospect",
    "B": "Active Client (Onboarding)",
    "C": "Established Client",
    None: "Unknown",
}


@dataclass
class EscalationEvent:
    reason: str
    priority: Priority
    name: str
    company: str
    email: str


def parse_escalation(text: str) -> EscalationEvent | None:
    match = _TAG_RE.search(text)
    if not match:
        return None
    reason, priority, name, company, email = match.groups()
    return EscalationEvent(
        reason=reason.strip(),
        priority=priority,
        name=name.strip(),
        company=company.strip(),
        email=email.strip(),
    )


def strip_escalation_tag(text: str) -> str:
    return _TAG_RE.sub("", text).strip()


def _build_slack_payload(event: EscalationEvent, session: Session) -> dict:
    emoji = _PRIORITY_EMOJI[event.priority]
    segment = _SEGMENT_LABELS.get(session.segment, "Unknown")
    last_turns = _summarise_last_turns(session.messages, n=3)

    return {
        "text": f"{emoji} *Rondo Escalation — {event.priority} priority*",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} Rondo Escalation — {event.priority} priority",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*User:*\n{event.name}"},
                    {"type": "mrkdwn", "text": f"*Company:*\n{event.company}"},
                    {"type": "mrkdwn", "text": f"*Email:*\n{event.email}"},
                    {"type": "mrkdwn", "text": f"*Segment:*\n{segment}"},
                    {"type": "mrkdwn", "text": f"*Session ID:*\n{session.id}"},
                    {"type": "mrkdwn", "text": f"*Reason:*\n{event.reason}"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Last conversation turns:*\n{last_turns}",
                },
            },
        ],
    }


def _summarise_last_turns(messages: list[dict], n: int = 3) -> str:
    recent = messages[-n * 2 :] if len(messages) >= n * 2 else messages
    lines = []
    for msg in recent:
        role = "User" if msg["role"] == "user" else "Rondo"
        content = msg["content"]
        # Truncate long messages for Slack readability
        if len(content) > 300:
            content = content[:297] + "..."
        lines.append(f"*{role}:* {content}")
    return "\n".join(lines) if lines else "No conversation history."


async def fire_escalation(event: EscalationEvent, session: Session) -> None:
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if webhook_url:
        try:
            payload = _build_slack_payload(event, session)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
            logger.info("Slack escalation sent for session %s", session.id)
        except Exception:
            logger.exception("Failed to send Slack escalation for session %s", session.id)
    else:
        logger.warning(
            "SLACK_WEBHOOK_URL not set — escalation logged only. Session: %s | Reason: %s | Priority: %s",
            session.id,
            event.reason,
            event.priority,
        )
