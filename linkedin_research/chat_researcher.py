#!/usr/bin/env python3
"""WhatsApp-style Streamlit chat for researching a person's LinkedIn activity.

Run:
    streamlit run chat_researcher.py

Phase 1 - RESEARCH:
    Type naturally, e.g. "Lauren Peate, Multitudes, Auckland New Zealand".
    The app extracts name/company/location, resolves candidate profiles, and
    asks you to confirm. Reply "yes" or a number to pick. It then scrapes posts
    + reposts (newest first) and saves a markdown report.

Phase 2 - Q&A:
    Ask questions about the scraped activity ("what has she been posting about",
    "summarise her activity", "any career themes"). Answered by Claude using the
    scraped markdown as context.

Secrets:
    APIFY_TOKEN       - required (Apify backend).
    ANTHROPIC_API_KEY - optional; enables LLM extraction + Q&A. Without it, the
                        app falls back to simple comma parsing and disables Q&A.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import streamlit as st

# Make sibling modules importable regardless of Streamlit's CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from actors import REGISTRY, ApifyClient, ApifyError  # noqa: E402
from research_person import (  # noqa: E402
    _engagement,
    _item_type,
    _text,
    _url,
    fmt_field,
    parse_item_date,
    parse_since,
    render_markdown,
    slugify,
    split_name,
)

QA_MODEL = "claude-sonnet-4-6"  # Claude Sonnet 4.6 for extraction + Q&A
DEFAULT_SINCE_DAYS = 60
MAX_CANDIDATES = 5
MAX_POSTS = 40


# --------------------------------------------------------------------------- #
# Secrets bridge: Streamlit Cloud -> os.environ
# --------------------------------------------------------------------------- #
def load_secrets_into_env() -> None:
    """Copy Streamlit secrets into os.environ.

    On Streamlit Community Cloud, secrets set in the dashboard are exposed via
    st.secrets (not always as OS env vars). We copy top-level string secrets
    into os.environ so the whole app can keep reading them with
    os.environ.get(...), which also works locally with plain env vars.
    No-op when there's no secrets.toml (e.g. local CLI runs).
    """
    try:
        for key, value in st.secrets.items():
            if isinstance(value, str) and not os.environ.get(key):
                os.environ[key] = value
    except Exception:
        pass  # no secrets configured -> rely on real environment variables


# --------------------------------------------------------------------------- #
# Anthropic client (optional)
# --------------------------------------------------------------------------- #
def get_anthropic():
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        return anthropic.Anthropic()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Extraction: name / company / location from a free-text message
# --------------------------------------------------------------------------- #
def extract_query(message: str, client) -> dict:
    """Return {name, location, current_company, past_company}.

    Prefer the Claude API for messy input; fall back to comma parsing.
    """
    if client is not None:
        try:
            schema = {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "location": {"type": "string"},
                    "current_company": {"type": "string"},
                    "past_company": {"type": "string"},
                },
                "required": ["name", "location", "current_company", "past_company"],
                "additionalProperties": False,
            }
            resp = client.messages.create(
                model=QA_MODEL,
                max_tokens=300,
                system=(
                    "Extract LinkedIn search fields from the user's message. "
                    "Return the person's full name, their location, current "
                    "company, and any former/past company. Use empty strings "
                    "for anything not present. Do not guess."
                ),
                messages=[{"role": "user", "content": message}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            text = next((b.text for b in resp.content if b.type == "text"), "{}")
            data = json.loads(text)
            if data.get("name"):
                return data
        except Exception:
            pass  # fall through to parsing

    # Simple comma parsing: "Name, Company, Location"
    parts = [p.strip() for p in message.split(",") if p.strip()]
    out = {"name": "", "location": "", "current_company": "", "past_company": ""}
    if parts:
        out["name"] = parts[0]
    if len(parts) >= 2:
        out["current_company"] = parts[1]
    if len(parts) >= 3:
        out["location"] = ", ".join(parts[2:])
    return out


# --------------------------------------------------------------------------- #
# Apify steps
# --------------------------------------------------------------------------- #
def resolve_candidates(client: ApifyClient, q: dict) -> list[dict]:
    actor = REGISTRY["resolve"]
    first, last = split_name(q["name"])
    run_input = {
        "firstName": first,
        "lastName": last,
        "profileScraperMode": "Short",
        "maxPages": 1,
        "maxItems": MAX_CANDIDATES,
        "strictSearch": True,
    }
    if q.get("location"):
        run_input["locations"] = [q["location"]]
    return client.run_actor(actor.actor_id, run_input)


def candidate_view(it: dict) -> dict:
    return {
        "name": fmt_field(it, "name", "fullName"),
        "headline": fmt_field(it, "position", "headline", "occupation"),
        "company": fmt_field(it, "currentCompany.name", "company"),
        "location": fmt_field(it, "location.linkedinText", "location", "locationName"),
        "url": fmt_field(it, "linkedinUrl", "url", "profileUrl"),
    }


def scrape_activity(client: ApifyClient, url: str, since) -> list[tuple]:
    actor = REGISTRY["posts"]
    run_input = {
        "targetUrls": [url],
        "maxPosts": MAX_POSTS,
        "postedLimitDate": since.date().isoformat(),
        "includeReposts": True,
        "includeQuotePosts": True,
        "scrapeComments": False,
        "scrapeReactions": False,
    }
    items = client.run_actor(actor.actor_id, run_input)
    rows = []
    for it in items:
        dt = parse_item_date(it)
        if dt and dt < since:
            continue
        rows.append((dt, it))
    rows.sort(key=lambda r: (r[0] is None, -(r[0].timestamp() if r[0] else 0)))
    return rows


# --------------------------------------------------------------------------- #
# Chat helpers
# --------------------------------------------------------------------------- #
def add(role: str, content: str) -> None:
    st.session_state.messages.append({"role": role, "content": content})


def render_message(role: str, content: str) -> None:
    with st.chat_message(role):
        if role == "user":
            # marker span lets CSS right-align + tint user bubbles
            st.markdown('<span class="cr-user"></span>', unsafe_allow_html=True)
        st.markdown(content)


WHATSAPP_CSS = """
<style>
/* Right-align + green tint for user messages (WhatsApp style) */
[data-testid="stChatMessage"]:has(.cr-user) {
    flex-direction: row-reverse;
    text-align: right;
    background: #dcf8c6;
    border-radius: 12px;
    padding: 8px 12px;
    margin-left: 18%;
}
/* Assistant messages: left, light grey */
[data-testid="stChatMessage"]:not(:has(.cr-user)) {
    background: #f5f5f5;
    border-radius: 12px;
    padding: 8px 12px;
    margin-right: 18%;
}
.cr-user { display: none; }
</style>
"""


# --------------------------------------------------------------------------- #
# Q&A
# --------------------------------------------------------------------------- #
def answer_question(client, question: str, markdown_ctx: str):
    """Stream an answer using ONLY the scraped markdown as context."""
    system = (
        "You are a research assistant. Answer the user's question about this "
        "person using ONLY the LinkedIn activity report below as your source. "
        "If the answer isn't in the report, say so plainly. Be concise and "
        "specific; cite dates where useful.\n\n"
        "=== SCRAPED ACTIVITY REPORT ===\n" + markdown_ctx
    )
    with client.messages.stream(
        model=QA_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": question}],
    ) as stream:
        yield from stream.text_stream


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
def init_state() -> None:
    ss = st.session_state
    ss.setdefault("messages", [])
    ss.setdefault("phase", "await_query")   # await_query | await_confirm | qa
    ss.setdefault("candidates", [])
    ss.setdefault("query", {})
    ss.setdefault("report_md", "")
    ss.setdefault("report_path", "")
    if not ss.messages:
        add("assistant",
            "Ready. Who do you want to research? Give me a **name, company, "
            "and location** — e.g. `Lauren Peate, Multitudes, Auckland New Zealand`.")


def handle_query(prompt: str, apify: ApifyClient, ai) -> None:
    q = extract_query(prompt, ai)
    if not q.get("name"):
        add("assistant", "I couldn't spot a name there. Try `Name, Company, Location`.")
        return
    st.session_state.query = q
    summary = (f"Searching for **{q['name']}**"
               + (f" · {q['current_company']}" if q.get("current_company") else "")
               + (f" · {q['location']}" if q.get("location") else "")
               + "  \n_(cheap search — no activity scraped yet)_")
    add("assistant", summary)
    with st.spinner("Resolving candidate profiles via Apify…"):
        try:
            items = resolve_candidates(apify, q)
        except ApifyError as exc:
            add("assistant", f"⚠️ Apify error during resolve: {exc}")
            return
    if not items:
        add("assistant", "No candidates found. Try adding/relaxing the location or company.")
        return
    st.session_state.candidates = [candidate_view(it) for it in items]
    lines = ["I found these candidate(s):\n"]
    for i, c in enumerate(st.session_state.candidates, 1):
        lines.append(
            f"**[{i}] {c['name'] or '(name n/a)'}**  \n"
            + (f"· {c['headline']}  \n" if c['headline'] else "")
            + (f"· {c['company']}  \n" if c['company'] else "")
            + (f"· {c['location']}  \n" if c['location'] else "")
            + (f"· {c['url']}" if c['url'] else "")
        )
    lines.append("\nReply **yes** (or **1**) to confirm the top match, or a number to pick another.")
    add("assistant", "\n".join(lines))
    st.session_state.phase = "await_confirm"


def handle_confirm(prompt: str, apify: ApifyClient) -> None:
    cands = st.session_state.candidates
    choice = prompt.strip().lower()
    idx = 0
    if choice in ("yes", "y", "confirm", "1", "first"):
        idx = 0
    elif choice.isdigit():
        idx = int(choice) - 1
    elif choice in ("no", "n", "none", "cancel"):
        add("assistant", "Okay — give me another name, company, and location.")
        st.session_state.phase = "await_query"
        return
    else:
        add("assistant", "Please reply **yes** or a candidate **number** (e.g. `2`).")
        return
    if idx < 0 or idx >= len(cands):
        add("assistant", f"There's no candidate {idx + 1}. Pick 1–{len(cands)}.")
        return

    chosen = cands[idx]
    url = chosen["url"]
    if not url:
        add("assistant", "That candidate has no profile URL to scrape. Pick another.")
        return
    since = parse_since(None, DEFAULT_SINCE_DAYS)
    add("assistant",
        f"Confirmed **{chosen['name']}**. Scraping posts + reposts since "
        f"**{since.date().isoformat()}** (newest first)…")
    with st.spinner("Scraping activity via Apify…"):
        try:
            rows = scrape_activity(apify, url.rstrip("/"), since)
        except ApifyError as exc:
            add("assistant", f"⚠️ Apify error during scrape: {exc}")
            return

    role = " | ".join(x for x in (chosen["headline"], chosen["company"],
                                  chosen["location"]) if x)
    md = render_markdown(chosen["name"], role, url, since, rows)
    path = os.path.join(os.getcwd(), f"{slugify(chosen['name'])}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)
    st.session_state.report_md = md
    st.session_state.report_path = path

    if not rows:
        add("assistant", "No posts or reposts found in this window.")
    else:
        for dt, it in rows:
            date_str = dt.date().isoformat() if dt else "date n/a"
            add("assistant",
                f"**{date_str}** · _{_item_type(it)}_\n\n{_text(it).strip()}\n\n"
                f"**Engagement:** {_engagement(it)}  \n"
                f"**URL:** {_url(it) or 'n/a'}")
    counts: dict[str, int] = {}
    for _, it in rows:
        counts[_item_type(it)] = counts.get(_item_type(it), 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in counts.items())
    add("assistant", f"**Done — {len(rows)} item(s)** ({summary or 'none'}).")
    add("assistant", f"📄 Report saved to **{os.path.basename(path)}**")
    add("assistant",
        "You can now ask me anything about this activity — e.g. _what has she "
        "been posting about_, _summarise her activity_, _any career themes_.")
    st.session_state.phase = "qa"


def main() -> None:
    st.set_page_config(page_title="LinkedIn Activity Researcher", page_icon="🔎")
    load_secrets_into_env()  # Streamlit Cloud secrets -> os.environ
    st.markdown(WHATSAPP_CSS, unsafe_allow_html=True)
    init_state()

    with st.sidebar:
        st.header("🔎 Activity Researcher")
        st.caption("LinkedIn-first, Apify-backed. Resolve → confirm → scrape → ask.")
        st.write("**Apify token:**", "✅ set" if os.environ.get("APIFY_TOKEN") else "❌ missing")
        st.write("**Claude (Q&A):**", "✅ set" if os.environ.get("ANTHROPIC_API_KEY") else "❌ off")
        if st.session_state.report_path:
            st.success(f"Report: {os.path.basename(st.session_state.report_path)}")
        if st.button("🔄 New search"):
            for k in ("messages", "phase", "candidates", "query",
                      "report_md", "report_path"):
                st.session_state.pop(k, None)
            st.rerun()

    # Replay history
    for m in st.session_state.messages:
        render_message(m["role"], m["content"])

    # Guard: Apify token
    apify_token = os.environ.get("APIFY_TOKEN", "").strip()
    placeholder = ("Ask about the activity…" if st.session_state.phase == "qa"
                   else "Name, company, location…")
    prompt = st.chat_input(placeholder)
    if not prompt:
        return

    if not apify_token and st.session_state.phase != "qa":
        render_message("user", prompt)
        add("user", prompt)
        add("assistant",
            "**APIFY_TOKEN is not set.** Set it in your environment and restart:\n"
            "`$env:APIFY_TOKEN = 'apify_api_...'` (PowerShell) then "
            "`streamlit run chat_researcher.py`.")
        st.rerun()

    apify = ApifyClient(token=apify_token) if apify_token else None
    ai = get_anthropic()

    render_message("user", prompt)
    phase = st.session_state.phase

    if phase == "qa":
        # render user, then stream answer live (handle_qa renders its own bubble)
        st.session_state.messages.append({"role": "user", "content": prompt})
        if ai is None:
            add("assistant", "Q&A needs `ANTHROPIC_API_KEY`. Set it and restart.")
        else:
            with st.chat_message("assistant"):
                try:
                    answer = st.write_stream(
                        answer_question(ai, prompt, st.session_state.report_md))
                except Exception as exc:
                    answer = f"⚠️ Q&A error: {exc}"
                    st.markdown(answer)
            st.session_state.messages.append({"role": "assistant", "content": answer})
        return

    add("user", prompt)
    if phase == "await_query":
        handle_query(prompt, apify, ai)
    elif phase == "await_confirm":
        handle_confirm(prompt, apify)
    st.rerun()


if __name__ == "__main__":
    main()
