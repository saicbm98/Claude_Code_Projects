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
from datetime import datetime, timedelta, timezone

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
    activity_note,
    fmt_field,
    render_markdown,
    scrape_activity_tiered,
    scrape_posts,
    slugify,
    split_name,
    titlecase,
)

QA_MODEL = "claude-sonnet-4-6"  # Claude Sonnet 4.6 for extraction + Q&A
DEFAULT_SINCE_DAYS = 180        # initial window = 6 months
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
        # Context needed to re-scrape on demand in Phase 2 (also for loaded sessions).
        "profile_url": ss.get("profile_url", ""),
        "role": ss.get("role", ""),
        "profile": ss.get("profile"),
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
    ss.profile_url = d.get("profile_url", "")
    ss.role = d.get("role", "")
    ss.profile = d.get("profile")
    ss.qa_messages = []
    ss.slug = slug
    ss.phase = "qa"
    ss.candidates = []
    ss.query = {}
    ss.download_after_idx = -1  # inline button position unknown for loaded sessions


def new_session() -> None:
    """Clear the main chat for a fresh search. Sidebar history is on disk, so
    it survives untouched."""
    for k in ("messages", "phase", "candidates", "query", "report_md",
              "report_path", "person_name", "created_at", "slug", "pending_delete",
              "profile_url", "role", "profile", "qa_messages", "download_after_idx"):
        st.session_state.pop(k, None)
    init_state()


def delete_session(slug: str) -> None:
    """Delete only the session JSON. The .md report file (if any) is left
    untouched on disk."""
    try:
        os.remove(os.path.join(sessions_dir(), f"{slug}.json"))
    except FileNotFoundError:
        pass
    except Exception:
        pass


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


# --------------------------------------------------------------------------- #
# Chat helpers
# --------------------------------------------------------------------------- #
def add(role: str, content: str) -> None:
    """Append a message AND persist the session (saved after every message)."""
    st.session_state.messages.append(
        {"role": role, "content": content, "timestamp": _now_iso()})
    save_session()


def render_message(role: str, content) -> None:
    # Content should always be a plain string (written via add()), but if an
    # Anthropic SDK content-block list leaks in, extract only text parts and
    # drop any tool_use / tool_result blocks so they never appear in the UI.
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            btype = (block.get("type") if isinstance(block, dict)
                     else getattr(block, "type", None))
            if btype in ("tool_use", "tool_result"):
                continue
            text = (block.get("text", "") if isinstance(block, dict)
                    else getattr(block, "text", ""))
            if text:
                parts.append(str(text))
        content = "\n".join(parts)
    if not isinstance(content, str):
        content = str(content)
    content = content.strip()
    if not content:
        return
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


# Emoji clusters (base emoji + ZWJ joins, variation selectors, skin tones).
_EMOJI_RE = re.compile(
    "(?:[\U0001F300-\U0001FAFF\U00002600-\U000026FF\U00002700-\U000027BF"
    "\U00002B00-\U00002BFF\U0001F000-\U0001F0FF\U000024C2\U00002139"
    "\U0000FE0F\U0000200D\U000020E3\U0001F1E6-\U0001F1FF\U0001F3FB-\U0001F3FF]+)"
)
_EMOJI_FONT: dict = {"loaded": False, "font": None}


def _emoji_font():
    """A color emoji font from the host (Noto Color Emoji on Linux/Cloud, Segoe
    UI Emoji on Windows, Apple Color Emoji on macOS), or None."""
    if _EMOJI_FONT["loaded"]:
        return _EMOJI_FONT["font"]
    _EMOJI_FONT["loaded"] = True
    try:
        from PIL import ImageFont
    except Exception:
        return None
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto/NotoColorEmoji.ttf",
        "C:\\Windows\\Fonts\\seguiemj.ttf",
        "/System/Library/Fonts/Apple Color Emoji.ttc",
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        for size in (109, 137, 96, 64, 48):  # bitmap fonts need a strike size
            try:
                _EMOJI_FONT["font"] = ImageFont.truetype(path, size)
                return _EMOJI_FONT["font"]
            except Exception:
                continue
    return None


def _emoji_png(cluster: str) -> str | None:
    """Rasterize one emoji (cluster) to a cached transparent color PNG; path."""
    font = _emoji_font()
    if font is None:
        return None
    try:
        import tempfile
        from PIL import Image, ImageDraw
        cache = os.path.join(tempfile.gettempdir(), "cr_emoji")
        os.makedirs(cache, exist_ok=True)
        key = "-".join(f"{ord(c):x}" for c in cluster)
        path = os.path.join(cache, key + ".png")
        if os.path.exists(path):
            return path
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        box = probe.textbbox((0, 0), cluster, font=font, embedded_color=True)
        w, h = max(1, box[2] - box[0]), max(1, box[3] - box[1])
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).text((-box[0], -box[1]), cluster, font=font,
                                    embedded_color=True)
        canvas.save(path)
        return path
    except Exception:
        return None


def _inline(text: str) -> str:
    """Markdown inline -> reportlab mini-HTML: **bold**, _italic_, inline emoji
    images. Emoji are stashed before escaping so tags can't be corrupted."""
    emojis: list[str] = []

    def _stash(m):
        emojis.append(m.group(0))
        return f"\x00{len(emojis) - 1}\x00"

    text = _EMOJI_RE.sub(_stash, text)

    if not _FONTS["unicode"]:
        for k, v in _SANITIZE.items():
            text = text.replace(k, v)
        text = text.encode("latin-1", "ignore").decode("latin-1")
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", text)

    def _unstash(m):
        cluster = emojis[int(m.group(1))]
        png = _emoji_png(cluster)
        if png:
            src = png.replace("\\", "/")
            return f'<img src="{src}" width="12" height="12" valign="-2"/>'
        return cluster if _FONTS["unicode"] else ""

    return re.sub("\x00(\\d+)\x00", _unstash, text)


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
# Q&A — agentic, with an on-demand re-scrape tool
# --------------------------------------------------------------------------- #
SCRAPE_TOOL = {
    "name": "scrape_posts",
    "description": (
        "Re-run the LinkedIn posts/reposts scraper (Apify) for the ALREADY-"
        "CONFIRMED person over a lookback window, returning fresh posts newest "
        "first. Use this whenever the user asks to scrape, scrape more, refresh, "
        "re-run, or fetch posts for a period such as 'the last 6 months', 'the "
        "last year', or 'apify scrape'. Do NOT use it for ordinary questions "
        "that the existing report already answers."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "months": {
                "type": "integer",
                "description": "Lookback window in months (e.g. 6, 12, 24). Default 6.",
            },
        },
        "required": ["months"],
    },
}

_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "a": 1}


def parse_months(text: str, default: int = 6) -> int:
    """Pull a lookback window (in months) out of a free-text message."""
    t = text.lower()
    m = re.search(r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
                  r"twelve|a)\s*(year|yr|month|mo)", t)
    if m:
        num = m.group(1)
        n = int(num) if num.isdigit() else _WORDNUM.get(num, 1)
        months = n * 12 if m.group(2).startswith(("year", "yr")) else n
        return max(1, min(24, months))
    if "year" in t:
        return 12
    return default


def _tool_months(tool_input: dict, prompt: str) -> int:
    m = tool_input.get("months") if isinstance(tool_input, dict) else None
    if isinstance(m, (int, float)) and m > 0:
        return max(1, min(24, int(m)))
    return parse_months(prompt)


def qa_system(report_md: str, person_name: str) -> str:
    return (
        f"You are a research assistant investigating {person_name}'s public "
        "LinkedIn activity.\n"
        "You have a tool, scrape_posts(months), that re-runs the LinkedIn posts "
        "scraper for THIS already-confirmed person over a lookback window (in "
        "months) and returns fresh posts.\n"
        "- Call scrape_posts when the user asks to scrape, scrape more, refresh, "
        "re-run, or fetch posts for a period (e.g. 'scrape the last 6 months', "
        "'get the last year', 'apify scrape').\n"
        "- For ordinary questions about the person, answer ONLY from the report "
        "below. If the answer isn't there, say so plainly. Be concise and "
        "specific; cite dates where useful.\n\n"
        "=== CURRENT REPORT ===\n" + (report_md or "(no report yet)")
    )


def posts_brief(rows, note: str, months: int) -> str:
    """Compact text of the freshly scraped posts to feed back to the model."""
    if not rows:
        return note
    lines = [note, ""]
    for dt, it in rows[:30]:
        d = dt.date().isoformat() if dt else "date n/a"
        txt = _text(it).strip().replace("\n", " ")
        if len(txt) > 400:
            txt = txt[:400] + "…"
        lines.append(f"[{d}] ({_item_type(it)}) {txt} — engagement: {_engagement(it)}")
    return "\n".join(lines)


def rescrape_and_report(apify: ApifyClient, months: int):
    """Re-scrape the confirmed profile over `months`, show the posts in chat,
    and refresh the report (markdown + download + session JSON)."""
    ss = st.session_state
    url = ss.get("profile_url")
    since = datetime.now(timezone.utc) - timedelta(days=months * 30)
    with st.spinner(f"Re-scraping the last {months} months via Apify…"):
        try:
            rows = scrape_posts(apify, url, since, MAX_POSTS)
        except ApifyError as exc:
            add("assistant", f"⚠️ Apify error during re-scrape: {exc}")
            return [], f"Re-scrape failed: {exc}"

    if rows:
        note = f"Activity found: showing posts from the last {months} months."
    else:
        note = (f"No posts or reposts found in the last {months} months. "
                "Profile and career history are unchanged.")

    # Rebuild the report with the new window; keep profile/role/name.
    md = render_markdown(ss.get("person_name") or "report", ss.get("role", ""),
                         url, since, rows, profile=ss.get("profile"),
                         window_note=note)
    ss.report_md = md
    save_session()

    # Present the new posts in chat, newest first (same format as the original).
    add("assistant", f"**{note}**")
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
    add("assistant",
        f"**Updated report — {len(rows)} item(s)** ({summary or 'none'}) "
        f"over the last {months} months.")
    # Mark this message as the anchor for the inline download button.
    st.session_state.download_after_idx = len(st.session_state.messages) - 1
    return rows, note


def run_agentic_qa(prompt: str, apify, ai, force_tool: bool = False) -> None:
    """One Phase-2 turn: the model answers from the report, or calls scrape_posts."""
    ss = st.session_state
    url = ss.get("profile_url")
    can_scrape = bool(apify and url)
    tools = [SCRAPE_TOOL] if can_scrape else []
    system = qa_system(ss.get("report_md", ""), ss.get("person_name") or "this person")

    msgs = list(ss.get("qa_messages", []))
    msgs.append({"role": "user", "content": prompt})
    tool_choice = ({"type": "tool", "name": "scrape_posts"}
                   if (force_tool and can_scrape) else {"type": "auto"})

    final_text = ""
    for _ in range(4):  # cap tool iterations
        kwargs = dict(model=QA_MODEL, max_tokens=1200, system=system, messages=msgs)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        try:
            resp = ai.messages.create(**kwargs)
        except Exception as exc:
            add("assistant", f"⚠️ Q&A error: {exc}")
            return

        if resp.stop_reason == "tool_use":
            msgs.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "scrape_posts":
                    months = _tool_months(block.input, prompt)
                    rows, note = rescrape_and_report(apify, months)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": posts_brief(rows, note, months)})
            msgs.append({"role": "user", "content": results})
            tool_choice = {"type": "auto"}  # let the model wrap up after scraping
            continue

        final_text = "".join(b.text for b in resp.content
                             if getattr(b, "type", None) == "text").strip()
        break

    if final_text:
        add("assistant", final_text)

    # Keep a lightweight (text-only) history for context on the next turn.
    hist = list(ss.get("qa_messages", []))
    hist.append({"role": "user", "content": prompt})
    if final_text:
        hist.append({"role": "assistant", "content": final_text})
    ss.qa_messages = hist[-12:]


def handle_qa(prompt: str, apify, ai) -> None:
    forced = any(k in prompt.lower() for k in ("scrape", "apify"))
    has_ctx = bool(apify and st.session_state.get("profile_url"))

    if ai is None:
        # No LLM: still honour an explicit scrape request deterministically.
        if forced and has_ctx:
            months = parse_months(prompt)
            rescrape_and_report(apify, months)
            add("assistant", "_(Add `ANTHROPIC_API_KEY` for conversational Q&A.)_")
        elif forced:
            add("assistant", "I can't re-scrape — no Apify token or confirmed "
                             "profile in this session.")
        else:
            add("assistant", "Q&A needs `ANTHROPIC_API_KEY`. Add it to secrets and reload.")
        return

    if forced and not has_ctx:
        add("assistant", "I can't re-scrape (no Apify token or confirmed profile "
                         "here), but I can still answer from the existing report.")
        run_agentic_qa(prompt, apify, ai, force_tool=False)
        return

    run_agentic_qa(prompt, apify, ai, force_tool=forced)


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
    ss.setdefault("pending_delete", None)
    ss.setdefault("profile_url", "")
    ss.setdefault("role", "")
    ss.setdefault("profile", None)
    ss.setdefault("qa_messages", [])
    ss.setdefault("download_after_idx", -1)
    if not ss.messages:
        ss.messages.append(
            {"role": "assistant", "content": GREETING, "timestamp": _now_iso()})


def _normalise_query(q: dict) -> dict:
    return {
        "name": titlecase(q.get("name", "")),
        "current_company": titlecase(q.get("current_company", "")),
        "past_company": titlecase(q.get("past_company", "")),
        "location": titlecase(q.get("location", "")),
    }


def _search_summary(q: dict, prefix: str = "Searching for") -> str:
    loc = q.get("location")
    return (f"{prefix} **{q['name']}**"
            + (f" · {q['current_company']}" if q.get("current_company") else "")
            + (f" · {loc}" if loc else "")
            + "  \n_(cheap search — no activity scraped yet)_")


def run_resolution(q: dict, apify: ApifyClient) -> None:
    """Resolve candidates for q and show them. Always stays in await_confirm so
    the user can confirm, reject, or refine — the chat never dead-ends."""
    first, last = split_name(q.get("name", ""))
    location = q.get("location") or None

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
                st.session_state.phase = "await_confirm"
                return
        if items:
            if i > 0:
                add("assistant", f"_(matched on fallback: {label})_")
            break

    st.session_state.phase = "await_confirm"  # stay searching until confirmed
    if not items:
        st.session_state.candidates = []
        add("assistant",
            "I couldn't find anyone with those details. Tell me more to narrow "
            "it down — a different **location**, **company**, **job title**, or a "
            "corrected **name** — and I'll search again.")
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
    lines.append("\nReply **yes** or a **number** to confirm — or, if none are "
                 "right, tell me what's different (location, company, job title, "
                 "corrected name) and I'll search again.")
    add("assistant", "\n".join(lines))


def handle_query(prompt: str, apify: ApifyClient, ai) -> None:
    q = extract_query(prompt, ai)
    if not q.get("name"):
        add("assistant", "I couldn't spot a name there. Try `Name, Company, Location`.")
        return
    q = _normalise_query(q)
    st.session_state.query = q
    register_session(q["name"])  # session known once we have a name
    add("assistant", _search_summary(q))
    run_resolution(q, apify)


def handle_research_update(prompt: str, apify: ApifyClient, ai) -> None:
    """The user rejected the candidates and/or gave new details. Merge them with
    the original search params and resolve again."""
    base = dict(st.session_state.query or {})
    new = _normalise_query(extract_query(prompt, ai))
    merged = dict(base)
    changed = False

    # Only override the name when a full (2+ word) name is supplied, so a bare
    # first name in a correction ("no, Sara works at…") doesn't drop the surname.
    nn = new.get("name", "").strip()
    if nn and len(nn.split()) >= 2 and nn.lower() != base.get("name", "").lower():
        merged["name"] = nn
        changed = True
    for k in ("location", "current_company", "past_company"):
        v = new.get(k, "").strip()
        if v and v.lower() != base.get(k, "").lower():
            merged[k] = v
            changed = True

    if not changed:
        add("assistant",
            "Okay — not those. Give me a bit more to go on (a different "
            "**location**, **company**, **job title**, or a corrected **name**) "
            "and I'll run a fresh search.")
        st.session_state.phase = "await_confirm"
        return

    st.session_state.query = merged
    add("assistant", _search_summary(merged, prefix="Re-searching with updated details:"))
    run_resolution(merged, apify)


def _confirm_index(choice: str, n: int):
    """Return a 0-based candidate index for a confirmation, 'oob' for an
    out-of-range number, or None if the message isn't a confirmation."""
    c = choice.strip().lower().rstrip("!. ")
    if c.isdigit():
        v = int(c)
        return v - 1 if 1 <= v <= n else "oob"
    m = re.fullmatch(r"(?:number|option|candidate|no\.?)\s*(\d+)", c)
    if m:
        v = int(m.group(1))
        return v - 1 if 1 <= v <= n else "oob"
    affirm = {"yes", "y", "yep", "yeah", "yup", "confirm", "confirmed", "correct",
              "ok", "okay", "sure", "that's right", "thats right", "first",
              "the first", "first one", "the first one", "number one", "yes please"}
    if c in affirm or c.startswith(("yes ", "yep ", "yeah ", "confirm", "correct")):
        return 0
    return None


def handle_confirm(prompt: str, apify: ApifyClient, ai) -> None:
    cands = st.session_state.candidates
    res = _confirm_index(prompt, len(cands)) if cands else None

    if res == "oob":
        add("assistant",
            f"There's no candidate {prompt.strip()} — pick 1–{len(cands)}, or "
            "tell me what's different and I'll search again.")
        return
    if res is None:
        # Not a confirmation -> rejection and/or updated details -> re-search.
        handle_research_update(prompt, apify, ai)
        return

    idx = res
    chosen = cands[idx]
    url = chosen["url"]
    if not url:
        add("assistant", "That candidate has no profile URL to scrape. Pick another.")
        return
    add("assistant",
        f"Confirmed **{chosen['name']}**. Pulling full profile + career history, "
        f"then scanning for posts (last 6 months, widening if empty)…")

    clean_url = url.rstrip("/")
    profile = None
    with st.spinner("Fetching full profile via Apify…"):
        try:
            profile = scrape_profile(apify, clean_url)
        except ApifyError as exc:
            add("assistant", f"⚠️ Apify error fetching profile: {exc}")
    # Tiered window fallback: 2 months -> 6 months -> 1 year -> 2 years.
    with st.spinner("Scraping activity via Apify (expanding window if empty)…"):
        try:
            rows, used_days, since = scrape_activity_tiered(
                apify, clean_url, MAX_POSTS,
                on_message=lambda m: add("assistant", m))
        except ApifyError as exc:
            add("assistant", f"⚠️ Apify error during scrape: {exc}")
            return
    note = activity_note(used_days)

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
    md = render_markdown(name, role, url, since, rows, profile=profile,
                         window_note=note)
    st.session_state.report_md = md   # kept for Q&A context + PDF rendering
    st.session_state.report_path = ""
    # Use the confirmed display name for the sidebar going forward.
    st.session_state.person_name = name
    # Context for agentic Phase 2 re-scraping.
    st.session_state.profile_url = clean_url
    st.session_state.role = role
    st.session_state.profile = profile
    st.session_state.qa_messages = []
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

    # State which window actually returned results (or that none did).
    add("assistant", f"**{note}**")
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
    # Mark this message as the anchor for the inline download button.
    st.session_state.download_after_idx = len(st.session_state.messages) - 1
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
            slug = s["slug"]
            active = slug == ss.get("slug")
            icon = "📄" if s["has_report"] else "💬"
            label = f"{icon}  {s['person_name']}  ·  {s['date_label']}"
            open_col, del_col = st.columns([0.82, 0.18])
            with open_col:
                if st.button(label, key=f"open_{slug}", use_container_width=True,
                             type="primary" if active else "secondary"):
                    load_session(slug)
                    ss.pending_delete = None
                    st.rerun()
            with del_col:
                if st.button("🗑", key=f"del_{slug}", use_container_width=True,
                             help="Delete this session"):
                    ss.pending_delete = slug
                    st.rerun()
            if ss.get("pending_delete") == slug:
                st.caption(f"Delete “{s['person_name']}”? The report file is kept.")
                yes_col, no_col = st.columns(2)
                with yes_col:
                    if st.button("✅ Delete", key=f"delyes_{slug}",
                                 use_container_width=True):
                        delete_session(slug)
                        ss.pending_delete = None
                        if active:
                            new_session()  # clear the open chat if we deleted it
                        st.rerun()
                with no_col:
                    if st.button("✖ Cancel", key=f"delno_{slug}",
                                 use_container_width=True):
                        ss.pending_delete = None
                        st.rerun()


def main() -> None:
    st.set_page_config(page_title="LinkedIn Activity Researcher", page_icon="🔎")
    load_secrets_into_env()
    st.markdown(WHATSAPP_CSS, unsafe_allow_html=True)
    init_state()
    render_sidebar()

    # Sidebar download button (always visible when report exists).
    if st.session_state.report_md:
        with st.sidebar:
            data, file_name, mime, label = report_download()
            st.download_button(label, data=data, file_name=file_name, mime=mime,
                               key="dl_sidebar")

    # Replay history; inject the inline download button right after the scrape
    # summary message so it sits in the chat flow, not just the sidebar.
    dl_idx = st.session_state.get("download_after_idx", -1)
    for i, m in enumerate(st.session_state.messages):
        render_message(m["role"], m["content"])
        if i == dl_idx and st.session_state.report_md:
            dl_data, dl_file, dl_mime, dl_label = report_download()
            st.download_button(dl_label, data=dl_data, file_name=dl_file,
                               mime=dl_mime, key="dl_inline")

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
        handle_qa(prompt, apify, ai)
        st.rerun()
        return

    add("user", prompt)
    if phase == "await_query":
        handle_query(prompt, apify, ai)
    elif phase == "await_confirm":
        handle_confirm(prompt, apify, ai)
    st.rerun()


if __name__ == "__main__":
    main()
