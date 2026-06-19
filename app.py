"""
FastAPI server for the Rondo chatbot.

Endpoints:
  POST /chat          — non-streaming JSON response
  POST /chat/stream   — Server-Sent Events stream
  GET  /session/{id}  — session metadata
  DELETE /session/{id} — end a session
"""

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

from src.chatbot import chat_simple, chat_stream
from src.session import delete as delete_session
from src.session import get as get_session


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")
    yield


app = FastAPI(title="Rondo — Round Treasury Chatbot", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Request / response models                                                    #
# --------------------------------------------------------------------------- #


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


class ChatResponse(BaseModel):
    session_id: str
    response: str
    segment: str | None
    escalated: bool


class SessionInfo(BaseModel):
    session_id: str
    segment: str | None
    escalated: bool
    message_count: int
    user_name: str | None
    user_company: str | None
    user_email: str | None


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Non-streaming chat endpoint — waits for the full response."""
    response_text, session = await chat_simple(req.session_id, req.message)
    return ChatResponse(
        session_id=session.id,
        response=response_text,
        segment=session.segment,
        escalated=session.escalated,
    )


@app.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest):
    """
    Server-Sent Events streaming endpoint.

    Event types emitted:
      data: <text chunk>      — incremental response text
      event: done             — stream complete; data is JSON session summary
      event: error            — data is error message
    """

    async def event_generator():
        try:
            async for chunk, session in chat_stream(req.session_id, req.message):
                if chunk:
                    yield {"data": chunk}
                else:
                    # Terminal sentinel — emit session summary as a "done" event
                    import json

                    summary = {
                        "session_id": session.id,
                        "segment": session.segment,
                        "escalated": session.escalated,
                    }
                    yield {"event": "done", "data": json.dumps(summary)}
        except Exception as exc:
            logging.getLogger(__name__).exception("Stream error")
            yield {"event": "error", "data": str(exc)}

    return EventSourceResponse(event_generator())


@app.get("/session/{session_id}", response_model=SessionInfo)
async def get_session_info(session_id: str) -> SessionInfo:
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionInfo(
        session_id=session.id,
        segment=session.segment,
        escalated=session.escalated,
        message_count=len(session.messages),
        user_name=session.user_name,
        user_company=session.user_company,
        user_email=session.user_email,
    )


@app.delete("/session/{session_id}")
async def end_session(session_id: str):
    delete_session(session_id)
    return {"deleted": session_id}


@app.get("/health")
async def health():
    return {"status": "ok", "bot": "Rondo"}


@app.get("/", response_class=HTMLResponse)
async def demo_widget():
    """Serves the demo chat widget."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "widget.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()
