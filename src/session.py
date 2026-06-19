"""
In-memory session store. Each session tracks the user's segment, conversation
history, collected contact details, and attempt counter for escalation logic.
Phase 2 can swap this out for Redis or a database.
"""

import uuid
from dataclasses import dataclass, field
from typing import Literal

Segment = Literal["A", "B", "C"] | None


@dataclass
class Session:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    segment: Segment = None
    messages: list[dict] = field(default_factory=list)
    attempt_count: int = 0  # consecutive unresolved turns in the current topic
    escalated: bool = False
    user_name: str | None = None
    user_company: str | None = None
    user_email: str | None = None

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def reset_attempts(self) -> None:
        self.attempt_count = 0

    def increment_attempt(self) -> None:
        self.attempt_count += 1

    def set_segment(self, segment: Segment) -> None:
        self.segment = segment
        self.attempt_count = 0

    def update_contact(
        self,
        name: str | None = None,
        company: str | None = None,
        email: str | None = None,
    ) -> None:
        if name:
            self.user_name = name
        if company:
            self.user_company = company
        if email:
            self.user_email = email


_store: dict[str, Session] = {}


def get_or_create(session_id: str | None) -> Session:
    if session_id and session_id in _store:
        return _store[session_id]
    session = Session(id=session_id or str(uuid.uuid4()))
    _store[session.id] = session
    return session


def get(session_id: str) -> Session | None:
    return _store.get(session_id)


def delete(session_id: str) -> None:
    _store.pop(session_id, None)
