#!/usr/bin/env python3
"""WhatsApp-style Streamlit chat for researching a person's LinkedIn activity.

Run:
    streamlit run chat_researcher.py

Sessions (like Claude.ai / ChatGPT):
    Every conversation is persisted to sessions/<slug>.json and listed in the
    sidebar (newest first). Saved after every message. Click a past session to
    reload its full history and keep asking questions; "New search" starts a
    fresh chat while the sidebar history stays intact.

Phase 1 - RESEARCH:
    Type naturally, e.g. "Lauren Peate, Multitudes, Auckland New Zealand".
    Resolve candidates -> confirm -> scrape full profile + posts -> report.

Phase 2 - Q&A:
    Ask questions about the scraped activity, answered by Claude using the
    scraped markdown report as context.

Secrets:
    APIFY_TOKEN       - required (Apify backend).
    ANTHROPIC_API_KEY - optional; enables LLM extraction + Q&A.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import streamlit as st

# Make sibling modules (actors.py, research_person.py) importable regardless of
# the working directory. On Streamlit Community Cloud the CWD is the repo root,
# not this subdirectory, so the entrypoint's own folder must be on sys.path.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

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
    titlecase,
)

QA_MODEL = "claude-sonnet-4-6"  # Claude Sonnet 4.6 for extraction + Q&A
DEFAULT_SINCE_DAYS = 60
MAX_CANDIDATES = 5
MAX_POSTS = 40
SESSIONS_DIRNAME = "sessions"
GREETING = ("Ready. Who do you want to research? Give me a **name, company, "
            "and location** — e.g. `Lauren Peate, Multitudes, Auckland New Zealand`.")


# --------------------------------------------------------------------------- #
# Secrets bridge: Streamlit Cloud -> os.environ
# --------------------------------------------------------------------------- #
def load_secrets_into_env() -> None:
    """Copy top-level Streamlit secrets into os.environ so the app can read
    them with os.environ.get(...) both on Cloud and locally. No-op locally."""
    try:
        for key, value in st.secrets.items():
            if isinstance(value, str) and not os.environ.get(key):
                os.environ[key] = value
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Session persistence (sessions/<slug>.json)
# --------------------------------------------------------------------------- #
def sessions_dir() -> str:
    d = os.path.join(os.getcwd(), SESSIONS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_date(iso: str, mtime: float | None = None) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        if mtime:
            dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        else:
            return ""
    return dt.strftime("%b %d, %H:%M")


def save_session() -> None:
    """Write the active session to disk. No-op until a slug exists (i.e. until
    the person's name is known). Called after every message."""
    ss = st.session_state
    slug = ss.get("slug")
    if not slug:
        return
    data = {
        "slug": slug,
        "person_name": ss.get("person_name") or "Untitled",
        "created_at": ss.get("created_at") or _now_iso(),
        "messages": ss.get("messages", []),
        "report_md": ss.get("report_md", ""),
    }
    try:
        path = os.path.join(sessions_dir(), f"{slug}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass  # never let a persistence hiccup break the chat


def list_sessions() -> list[dict]:
    """All saved sessions, newest first."""
    out: list[dict] = []
    try:
        for fn in os.listdir(sessions_dir()):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(sessions_dir(), fn)
            try:
                with open(path, encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            mtime = os.path.getmtime(path)
            out.append({
                "slug": d.get("slug") or fn[:-5],
                "person_name": d.get("person_name") or fn[:-5],
                "created_at": d.get("created_at") or "",
                "has_report": bool(d.get("report_md")),
                "date_label": _fmt_date(d.get("created_at", ""), mtime),
                "_sort": d.get("created_at") or "",
                "_mtime": mtime,
            })
    except Exception:
        pass
    out.sort(key=lambda s: (s["_sort"], s["_mtime"]), reverse=True)
    return out


def register_session(name: str) -> None:
    """Create the session id once the person's name is known (Phase 1)."""
    ss = st.session_state
    if ss.get("slug"):
        return
    ss.person_name = name
    ss.created_at = _now_iso()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    ss.slug = f"{slugify(name)}-{stamp}"
    save_session()


def load_session(slug: str) -> None:
    """Restore a saved session into Phase 2 Q&A mode."""
    path = os.path.join(sessions_dir(), f"{slug}.json")
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    ss = st.session_state
    ss.messages = d.get("messages", [])
    ss.report_md = d.get("report_md", "")
    ss.report_path = ""
    ss.person_name = d.get("person_name", "")
    ss.created_at = d.get("created_at", _now_iso())
    ss.slug = slug
    ss.phase = "qa"
    ss.candidates = []
    ss.query = {}


def new_session() -> None:
    """Clear the main chat for a fresh search. Sidebar history is on disk, so
    it survives untouched."""
    for k in ("messages", "phase", "candidates", "query", "report_md",
              "report_path", "person_name", "created_at", "slug"):
        st.session_state.pop(k, None)
    init_state()


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
    Prefer the Claude API for messy input; fall back to comma parsing."""
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
            pass

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
def resolve_candidates(client: ApifyClient, first: str, last: str,
                       location: str | None) -> list[dict]:
    actor = REGISTRY["resolve"]
    run_input = {
        "firstName": first,
        "lastName": last,
        "profileScraperMode": "Short",
        "maxPages": 1,
        "maxItems": MAX_CANDIDATES,
        "strictSearch": True,
    }
    if location:
        run_input["locations"] = [location]
    return client.run_actor(actor.actor_id, run_input)


def scrape_profile(client: ApifyClient, url: str) -> dict | None:
    actor = REGISTRY["confirm"]  # harvestapi/linkedin-profile-scraper
    items = client.run_actor(
        actor.actor_id,
        {"profileScraperMode": "Profile details no email ($4 per 1k)",
         "queries": [url]},
    )
    return items[0] if items else None


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
    """Append a message AND persist the session (saved after every message)."""
    st.session_state.messages.append(
        {"role": role, "content": content, "timestamp": _now_iso()})
    save_session()


def render_message(role: str, content: str) -> None:
    with st.chat_message(role):
        if role == "user":
            st.markdown('<span class="cr-user"></span>', unsafe_allow_html=True)
        st.markdown(content)


WHATSAPP_CSS = """
<style>
[data-testid="stChatMessage"]:has(.cr-user) {
    flex-direction: row-reverse;
    text-align: right;
    background: #dcf8c6;
    border-radius: 12px;
    padding: 8px 12px;
    margin-left: 18%;
}
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
# PDF report rendering (reportlab)
# --------------------------------------------------------------------------- #
import html  # noqa: E402
import re  # noqa: E402
from io import BytesIO  # noqa: E402

_FONTS = {"ready": False, "base": "Helvetica", "bold": "Helvetica-Bold", "unicode": False}
_SANITIZE = {
    "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", "•": "-",
    " ": " ",
}


def _find_unicode_font() -> tuple[str | None, str | None]:
    """Locate a Unicode TTF (+ bold) already on the host, so the PDF can render
    em-dashes, curly quotes, accents, etc. Falls back to None (Helvetica)."""
    pairs = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
         "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
        ("C:\\Windows\\Fonts\\arial.ttf", "C:\\Windows\\Fonts\\arialbd.ttf"),
        ("C:\\Windows\\Fonts\\segoeui.ttf", "C:\\Windows\\Fonts\\segoeuib.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ]
    for reg, bold in pairs:
        if os.path.exists(reg):
            return reg, (bold if os.path.exists(bold) else reg)
    return None, None


def _ensure_fonts() -> None:
    if _FONTS["ready"]:
        return
    _FONTS["ready"] = True
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        reg, bold = _find_unicode_font()
        if reg:
            pdfmetrics.registerFont(TTFont("ReportBody", reg))
            pdfmetrics.registerFont(TTFont("ReportBody-Bold", bold))
            pdfmetrics.registerFontFamily("ReportBody", normal="ReportBody",
                                          bold="ReportBody-Bold")
            _FONTS.update(base="ReportBody", bold="ReportBody-Bold", unicode=True)
    except Exception:
        pass  # keep Helvetica fallback


def _inline(text: str) -> str:
    """Markdown inline -> reportlab mini-HTML. Escapes XML, then **bold**/_italic_."""
    if not _FONTS["unicode"]:
        for k, v in _SANITIZE.items():
            text = text.replace(k, v)
        text = text.encode("latin-1", "ignore").decode("latin-1")
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", text)
    return text


def render_pdf(person_name: str, report_md: str) -> bytes:
    """Render the markdown report to a readable, paginated PDF."""
    _ensure_fonts()
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (HRFlowable, Paragraph, SimpleDocTemplate,
                                    Spacer)

    base, bold = _FONTS["base"], _FONTS["bold"]
    body = ParagraphStyle("body", fontName=base, fontSize=10, leading=14,
                          spaceAfter=2)
    h1 = ParagraphStyle("h1", fontName=bold, fontSize=18, leading=22,
                        spaceAfter=8, textColor=colors.HexColor("#0a66c2"))
    h2 = ParagraphStyle("h2", fontName=bold, fontSize=14, leading=18,
                        spaceBefore=10, spaceAfter=4,
                        textColor=colors.HexColor("#222222"))
    h3 = ParagraphStyle("h3", fontName=bold, fontSize=11.5, leading=15,
                        spaceBefore=6, spaceAfter=2)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=12)
    quote = ParagraphStyle("quote", fontName=base, fontSize=9, leading=13,
                           leftIndent=10, textColor=colors.HexColor("#666666"))

    flow: list = []
    for raw in (report_md or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flow.append(Spacer(1, 5))
        elif line.startswith("# "):
            flow.append(Paragraph(_inline(line[2:]), h1))
        elif line.startswith("## "):
            flow.append(Paragraph(_inline(line[3:]), h2))
        elif line.startswith("### "):
            flow.append(Paragraph(_inline(line[4:]), h3))
        elif line.startswith("> "):
            flow.append(Paragraph(_inline(line[2:]), quote))
        elif line.strip() == "---":
            flow.append(Spacer(1, 3))
            flow.append(HRFlowable(width="100%", thickness=0.5,
                                   color=colors.HexColor("#dddddd")))
            flow.append(Spacer(1, 3))
        elif line.lstrip().startswith(("- ", "* ")):
            flow.append(Paragraph("•&nbsp;" + _inline(line.lstrip()[2:]), bullet))
        else:
            flow.append(Paragraph(_inline(line), body))

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, title=f"{person_name} — LinkedIn report",
        topMargin=1.6 * cm, bottomMargin=1.6 * cm,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
    )
    doc.build(flow)
    return buf.getvalue()


def report_download():
    """(data, file_name, mime, label) for the report download button.
    PDF when possible; falls back to markdown if reportlab is unavailable."""
    md = st.session_state.report_md
    name = st.session_state.person_name or "report"
    slug = slugify(name)
    try:
        return (render_pdf(name, md), f"{slug}.pdf", "application/pdf",
                "⬇️  Download report (PDF)")
    except Exception:
        return (md.encode("utf-8"), f"{slug}.md", "text/markdown",
                "⬇️  Download report (.md — PDF unavailable)")


# --------------------------------------------------------------------------- #
# Q&A
# --------------------------------------------------------------------------- #
def answer_question(client, question: str, markdown_ctx: str):
    """Stream an answer using ONLY the scraped markdown as context."""
    system = (
        "You are a research assistant. Answer the user's question about this "
        "person using ONLY the LinkedIn report below as your source. If the "
        "answer isn't in the report, say so plainly. Be concise and specific; "
        "cite dates where useful.\n\n"
        "=== SCRAPED REPORT ===\n" + (markdown_ctx or "(no report available)")
    )
    with client.messages.stream(
        model=QA_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": question}],
    ) as stream:
        yield from stream.text_stream


# --------------------------------------------------------------------------- #
# Phase handlers
# --------------------------------------------------------------------------- #
def init_state() -> None:
    ss = st.session_state
    ss.setdefault("messages", [])
    ss.setdefault("phase", "await_query")   # await_query | await_confirm | qa
    ss.setdefault("candidates", [])
    ss.setdefault("query", {})
    ss.setdefault("report_md", "")
    ss.setdefault("report_path", "")
    ss.setdefault("person_name", "")
    ss.setdefault("created_at", "")
    ss.setdefault("slug", None)
    if not ss.messages:
        ss.messages.append(
            {"role": "assistant", "content": GREETING, "timestamp": _now_iso()})


def handle_query(prompt: str, apify: ApifyClient, ai) -> None:
    q = extract_query(prompt, ai)
    if not q.get("name"):
        add("assistant", "I couldn't spot a name there. Try `Name, Company, Location`.")
        return

    # Normalise name/company/location to title case before the API call.
    q["name"] = titlecase(q.get("name", ""))
    q["current_company"] = titlecase(q.get("current_company", ""))
    q["past_company"] = titlecase(q.get("past_company", ""))
    q["location"] = titlecase(q.get("location", ""))
    st.session_state.query = q

    # Register the session now that the person's name is known.
    register_session(q["name"])

    first, last = split_name(q["name"])
    location = q.get("location") or None
    summary = (f"Searching for **{q['name']}**"
               + (f" · {q['current_company']}" if q.get("current_company") else "")
               + (f" · {location}" if location else "")
               + "  \n_(cheap search — no activity scraped yet)_")
    add("assistant", summary)

    # Fallback ladder: name+location, then name only (deduped).
    attempts: list[tuple[str, str | None]] = []
    if location:
        attempts.append(("name + location", location))
    attempts.append(("name only", None))

    items: list[dict] = []
    for i, (label, loc) in enumerate(attempts):
        if i > 0:
            add("assistant", f"No matches yet — retrying with **{label}**…")
        with st.spinner(f"Resolving via Apify ({label})…"):
            try:
                items = resolve_candidates(apify, first, last, loc)
            except ApifyError as exc:
                add("assistant", f"⚠️ Apify error during resolve: {exc}")
                return
        if items:
            if i > 0:
                add("assistant", f"_(matched on fallback: {label})_")
            break

    if not items:
        add("assistant", "No candidates found, even with name only. "
                         "Check the spelling or try a different person.")
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
        f"Confirmed **{chosen['name']}**. Pulling full profile + career history, "
        f"then posts + reposts since **{since.date().isoformat()}** (newest first)…")

    clean_url = url.rstrip("/")
    profile = None
    with st.spinner("Fetching full profile via Apify…"):
        try:
            profile = scrape_profile(apify, clean_url)
        except ApifyError as exc:
            add("assistant", f"⚠️ Apify error fetching profile: {exc}")
    with st.spinner("Scraping activity via Apify…"):
        try:
            rows = scrape_activity(apify, clean_url, since)
        except ApifyError as exc:
            add("assistant", f"⚠️ Apify error during scrape: {exc}")
            return

    name = (fmt_field(profile or {}, "name", "fullName")
            or " ".join(x for x in (fmt_field(profile or {}, "firstName"),
                                    fmt_field(profile or {}, "lastName")) if x)
            or chosen["name"])
    role = (" | ".join(x for x in (
                fmt_field(profile or {}, "headline", "occupation"),
                fmt_field(profile or {}, "location.linkedinText",
                          "location.parsed.text", "location"),
            ) if x)
            or " | ".join(x for x in (chosen["headline"], chosen["company"],
                                      chosen["location"]) if x))
    md = render_markdown(name, role, url, since, rows, profile=profile)
    st.session_state.report_md = md   # kept for Q&A context + PDF rendering
    st.session_state.report_path = ""
    # Use the confirmed display name for the sidebar going forward.
    st.session_state.person_name = name
    save_session()

    if profile:
        exp = profile.get("experience") or []
        about = fmt_field(profile, "about", "summary")
        prof_lines = [f"**Profile captured for {name}**"]
        if role:
            prof_lines.append(f"· {role}")
        if exp:
            roles_preview = "; ".join(
                " — ".join(x for x in (fmt_field(e, "position", "title"),
                                       fmt_field(e, "companyName", "company")) if x)
                for e in exp[:5]
            )
            prof_lines.append(f"· **{len(exp)} roles** in career history: {roles_preview}"
                              + (" …" if len(exp) > 5 else ""))
        if about:
            prof_lines.append("· About/bio captured ✓")
        prof_lines.append("_(full career history + bio are at the top of the report)_")
        add("assistant", "  \n".join(prof_lines))
    else:
        add("assistant", "_(couldn't fetch the full profile — report has activity only)_")

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
    add("assistant", "📄 Report ready — use the **Download report** button above.")
    add("assistant",
        "You can now ask me anything about this person — e.g. _what has she "
        "been posting about_, _summarise her career_, _any recurring themes_.")
    st.session_state.phase = "qa"


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def render_sidebar() -> None:
    ss = st.session_state
    with st.sidebar:
        st.header("🔎 Activity Researcher")
        if st.button("➕  New search", use_container_width=True):
            new_session()
            st.rerun()
        st.caption(
            f"Apify {'✅' if os.environ.get('APIFY_TOKEN') else '❌'}"
            f"  ·  Claude {'✅' if os.environ.get('ANTHROPIC_API_KEY') else '❌'}"
        )
        st.divider()
        st.subheader("History")
        sessions = list_sessions()
        if not sessions:
            st.caption("No saved sessions yet.")
        for s in sessions:
            active = s["slug"] == ss.get("slug")
            icon = "📄" if s["has_report"] else "💬"
            label = f"{icon}  {s['person_name']}  ·  {s['date_label']}"
            if st.button(label, key=f"sess_{s['slug']}", use_container_width=True,
                         type="primary" if active else "secondary"):
                load_session(s["slug"])
                st.rerun()


def main() -> None:
    st.set_page_config(page_title="LinkedIn Activity Researcher", page_icon="🔎")
    load_secrets_into_env()
    st.markdown(WHATSAPP_CSS, unsafe_allow_html=True)
    init_state()
    render_sidebar()

    # Download button for the report (after a scrape, or on a loaded session).
    if st.session_state.report_md:
        data, file_name, mime, label = report_download()
        st.download_button(label, data=data, file_name=file_name, mime=mime,
                           key="dl_report")

    # Replay history
    for m in st.session_state.messages:
        render_message(m["role"], m["content"])

    apify_token = os.environ.get("APIFY_TOKEN", "").strip()
    placeholder = ("Ask about this person…" if st.session_state.phase == "qa"
                   else "Name, company, location…")
    prompt = st.chat_input(placeholder)
    if not prompt:
        return

    if not apify_token and st.session_state.phase != "qa":
        render_message("user", prompt)
        add("user", prompt)
        add("assistant",
            "**APIFY_TOKEN is not set.** Set it (or add it to Streamlit secrets) "
            "and reload.")
        st.rerun()

    apify = ApifyClient(token=apify_token) if apify_token else None
    ai = get_anthropic()

    render_message("user", prompt)
    phase = st.session_state.phase

    if phase == "qa":
        add("user", prompt)
        if ai is None:
            add("assistant", "Q&A needs `ANTHROPIC_API_KEY`. Add it to secrets and reload.")
        else:
            with st.chat_message("assistant"):
                try:
                    answer = st.write_stream(
                        answer_question(ai, prompt, st.session_state.report_md))
                except Exception as exc:
                    answer = f"⚠️ Q&A error: {exc}"
                    st.markdown(answer)
            add("assistant", answer)
        return

    add("user", prompt)
    if phase == "await_query":
        handle_query(prompt, apify, ai)
    elif phase == "await_confirm":
        handle_confirm(prompt, apify)
    st.rerun()


if __name__ == "__main__":
    main()
