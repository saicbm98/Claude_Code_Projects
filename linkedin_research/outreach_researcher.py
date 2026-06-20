#!/usr/bin/env python3
"""NZ Outreach Researcher — one-page Streamlit tool for job-search outreach.

End-to-end flow on a single page:

  1+2. DISCOVER  — Perplexity Agent API (people_search + web_search) finds real,
                   named people at a target company matching the chosen personas
                   and free-text constraints. Returns a structured candidate list.
  3.    SHORTLIST — st.data_editor with a checkbox column to tick who to keep.
  4.    SCRAPE    — the SAME proven Apify pipeline the Activity Researcher uses
                   (harvestapi profile scraper + posts scraper) deep-scrapes only
                   the ticked people: full career history, about, recent posts.
  5.    CSV       — download the combined discovery + scraped data.
  6.    DRAFT     — an embedded Claude chat (with the scraped data as context)
                   for hook identification and outreach-email drafting.

This is a SEPARATE standalone entrypoint. It deliberately does NOT modify the
existing Activity Researcher (chat_researcher.py) — instead it REUSES its proven
helpers (Apify client, actor registry, profile/post scrapers, markdown report
renderer, secrets bridge, Anthropic client) by importing them.

Run:
    streamlit run linkedin_research/outreach_researcher.py

Secrets (read from st.secrets, never hardcoded):
    APIFY_TOKEN        — required for Stage 4 deep scrape (existing name, reused).
    ANTHROPIC_API_KEY  — required for the Stage 6 drafting chat (existing name).
    PERPLEXITY_API_KEY — required for Stage 1+2 discovery (new; key starts pplx-).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

# Make the sibling modules (actors.py, research_person.py, chat_researcher.py)
# importable regardless of the working directory. On Streamlit Community Cloud
# the CWD is the repo root, not this subdirectory, so this folder must be on
# sys.path — exactly as chat_researcher.py does it.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from actors import ApifyClient, ApifyError  # noqa: E402
from research_person import (  # noqa: E402
    _engagement,
    _exp_dates,
    _item_type,
    _text,
    _url,
    activity_note,
    fmt_field,
    scrape_activity_tiered,
    slugify,
    split_name,
    titlecase,
)
# Reuse the existing app's proven helpers rather than reimplementing them.
from chat_researcher import (  # noqa: E402
    MAX_POSTS,
    candidate_view,
    get_anthropic,
    load_secrets_into_env,
    resolve_candidates,
    scrape_profile,
)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
# The drafting chat model. Swap this single line to "claude-opus-4-8" for higher
# quality drafts (slower / pricier).
CHAT_MODEL = "claude-sonnet-4-6"

# Perplexity Agent API. The endpoint below is the documented OpenAI-compatible
# alias of /v1/agent; both accept the same body. Verified against the live docs
# at https://docs.perplexity.ai/docs/agent-api/quickstart (June 2026).
PPLX_ENDPOINT = "https://api.perplexity.ai/v1/responses"

# Research depth -> Perplexity behaviour. Depth drives the web_search
# `search_context_size` (low/medium/high, the documented token budgets), and we
# also scale the model tier, reasoning effort, agent steps, the people_search
# token budget, and `max_people` (the requested number of results) so "High"
# really is deeper AND returns more people. `max_people` is NOT an API limit —
# the Agent API has no max_results param; it is the count we ask the model for in
# the instructions (see research_instructions). All people_search models below
# are from the live docs' supported list; tweak freely.
DEPTH_CONFIG = {
    "Low": {
        "model": "openai/gpt-5-mini",
        "search_context_size": "low",
        "effort": "low",
        "max_steps": 4,
        "people_tokens": 8000,
        "max_people": 15,
    },
    "Medium": {
        "model": "openai/gpt-5",
        "search_context_size": "medium",
        "effort": "medium",
        "max_steps": 7,
        "people_tokens": 16000,
        "max_people": 30,
    },
    "High": {
        "model": "openai/gpt-5.5",
        "search_context_size": "high",
        "effort": "high",
        "max_steps": 10,
        "people_tokens": 28000,
        "max_people": 50,
    },
}

PERSONA_OPTIONS = [
    "C-Suite / Executives",
    "VPs & Directors",
    "Managers",
    "Technical leads",
    "Senior departmental members",
]
DEFAULT_PERSONAS = ["Managers", "Technical leads"]

# CSV export columns. Adjust later to match your outreach tracker.
CSV_COLUMNS = [
    "Company", "Name", "Title", "Location", "LinkedIn URL",
    "Background notes", "Hook notes", "Email status", "Date contacted", "Notes",
]

CONSULTANT_NAME = "Sara"  # BCC reminder target in the drafting chat.

log = logging.getLogger("outreach_researcher")


# --------------------------------------------------------------------------- #
# Stage 1+2: Perplexity discovery
# --------------------------------------------------------------------------- #
def build_research_query(company: str, personas: list[str], context: str) -> str:
    """One natural-language research instruction combining all the form inputs."""
    persona_txt = ", ".join(personas) if personas else "relevant employees"
    parts = [
        f"Find real, currently-employed people at {company}.",
        f"Target these kinds of people: {persona_txt}.",
    ]
    if context.strip():
        parts.append(f"Additional constraints from the user: {context.strip()}")
    parts.append(
        "Only include people you can find concrete evidence for. Capture each "
        "person's full name, current job title, location, a short background "
        "summary, and their LinkedIn profile URL where available."
    )
    return " ".join(parts)


def research_instructions(company: str, max_people: int) -> str:
    """System-level instructions for the Perplexity agent. Forces strict JSON so
    the result parses cleanly into the candidate table. `max_people` is the
    requested result count (depth-dependent); it is the only cap on how many
    people come back, so it is passed in rather than hard-coded."""
    return (
        "You are a people-research assistant for a job-search outreach workflow. "
        f"Use the people_search and web_search tools to find REAL, named people "
        f"who currently work at {company} and match the user's requested personas "
        "and constraints. Verify with searches; never invent people or URLs. "
        "Run multiple searches and keep going until you have found as many "
        f"matching people as you can, up to {max_people}.\n\n"
        "Return your final answer as STRICT JSON ONLY: a single JSON array, with "
        "no prose and no markdown code fences. Each element must be an object with "
        "exactly these keys:\n"
        '  "name", "title", "location", "background", "linkedin_url"\n'
        "Use an empty string for any field you could not establish. Keep "
        f'"background" to one or two factual sentences. Return up to {max_people} '
        "people, best matches first. If you genuinely find no one, return an "
        "empty array []."
    )


def call_perplexity(api_key: str, depth: str, instructions: str, query: str) -> dict:
    """POST to the Perplexity Agent API and return the parsed JSON response."""
    cfg = DEPTH_CONFIG[depth]
    body = {
        "model": cfg["model"],
        "instructions": instructions,
        "input": query,
        "reasoning": {"effort": cfg["effort"]},
        "max_steps": cfg["max_steps"],
        "tools": [
            {
                "type": "people_search",
                "max_tokens": cfg["people_tokens"],
                "max_tokens_per_page": 1000,
            },
            {
                "type": "web_search",
                "search_context_size": cfg["search_context_size"],
            },
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Deep research can take a while; give it a generous timeout.
    resp = requests.post(PPLX_ENDPOINT, headers=headers, json=body, timeout=240)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Perplexity API error (HTTP {resp.status_code}): {resp.text[:600]}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Perplexity returned non-JSON: {resp.text[:400]}") from exc


def _output_text(resp: dict) -> str:
    """Pull the assistant's final text out of the Agent API response shape:
    output[] -> {type: 'message', content: [{type: 'output_text', text: ...}]}.
    Falls back to the convenience top-level `output_text` string if present."""
    direct = resp.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    chunks: list[str] = []
    for item in resp.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for c in item.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    if c.get("text"):
                        chunks.append(str(c["text"]))
    return "\n".join(chunks)


def _first_json_array(text: str) -> str | None:
    """Return the first balanced [...] block in text, or None."""
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_people_json(text: str) -> list[dict]:
    """Extract the people array from the model's text, tolerating code fences and
    surrounding prose."""
    if not text:
        return []
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    for candidate in (t, _first_json_array(t)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            # Some models wrap as {"people": [...]} — take the first list value.
            for v in data.values():
                if isinstance(v, list):
                    return [d for d in v if isinstance(d, dict)]
    return []


def _gather_raw_sources(resp: dict) -> list[dict]:
    """Flatten every people_search / web_search result entry the agent returned,
    so we can backfill a missing LinkedIn URL by name."""
    out: list[dict] = []
    for item in resp.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        results = item.get("results")
        if isinstance(results, list):
            for r in results:
                if isinstance(r, dict):
                    out.append(r)
    # Some shapes expose a top-level search_results object too.
    sr = resp.get("search_results")
    if isinstance(sr, list):
        out.extend(r for r in sr if isinstance(r, dict))
    return out


def _normalise_person(p: dict, raw_sources: list[dict]) -> dict:
    """Coerce a model person object into our canonical candidate dict and, where
    the LinkedIn URL is missing, try to recover it from the raw search results."""
    name = str(p.get("name") or p.get("full_name") or "").strip()
    title = str(p.get("title") or p.get("role") or p.get("headline") or "").strip()
    location = str(p.get("location") or "").strip()
    background = str(
        p.get("background") or p.get("summary") or p.get("bio") or ""
    ).strip()
    url = str(
        p.get("linkedin_url") or p.get("linkedin") or p.get("url")
        or p.get("profile_url") or ""
    ).strip()

    if not url and name:
        # Backfill: find a LinkedIn source whose title mentions this person.
        first = name.split()[0].lower() if name.split() else ""
        for r in raw_sources:
            r_url = str(r.get("url") or "").strip()
            r_title = str(r.get("title") or "").lower()
            if "linkedin.com/in/" in r_url.lower() and first and first in r_title:
                url = r_url
                break

    return {
        "name": name,
        "title": title,
        "location": location,
        "background": background,
        "linkedin_url": url,
    }


def discover_people(api_key: str, company: str, personas: list[str],
                    depth: str, context: str) -> tuple[list[dict], str]:
    """Run discovery. Returns (candidates, raw_assistant_text)."""
    instructions = research_instructions(company, DEPTH_CONFIG[depth]["max_people"])
    query = build_research_query(company, personas, context)
    resp = call_perplexity(api_key, depth, instructions, query)
    text = _output_text(resp)
    raw_sources = _gather_raw_sources(resp)
    people = _parse_people_json(text)
    candidates = [_normalise_person(p, raw_sources) for p in people]
    # Drop entries with no name at all; keep order (best matches first).
    candidates = [c for c in candidates if c["name"]]
    return candidates, text


# --------------------------------------------------------------------------- #
# Stage 4: Apify deep scrape (selected people only) — reuses the proven pipeline
# --------------------------------------------------------------------------- #
def _role_from_profile(profile: dict | None, fallback_title: str,
                       fallback_location: str) -> str:
    if not profile:
        return " | ".join(x for x in (fallback_title, fallback_location) if x)
    headline = fmt_field(profile, "headline", "occupation")
    location = fmt_field(profile, "location.linkedinText",
                         "location.parsed.text", "location")
    return (" | ".join(x for x in (headline, location) if x)
            or " | ".join(x for x in (fallback_title, fallback_location) if x))


# --- Robust profile rendering ---------------------------------------------- #
# The shared render_markdown/render_profile_section in research_person.py reads
# only `experience` / `currentPosition` / `about`. If the harvestapi actor's
# output key names drift, that section silently goes blank. The renderer below
# tries every plausible key spelling so career history survives such drift.
def _first_nonempty_list(profile: dict, *keys: str) -> list:
    for k in keys:
        v = profile.get(k)
        if isinstance(v, list) and v:
            return v
    return []


def _exp_entry_md(e: dict) -> list[str]:
    title = fmt_field(e, "position", "title", "role", "jobTitle") or "(role n/a)"
    company = fmt_field(e, "companyName", "company", "company.name",
                        "organisation", "organization")
    head = title + (f" — {company}" if company else "")
    lines = [f"### {head}"]
    meta = " · ".join(x for x in (
        _exp_dates(e) or fmt_field(e, "dateRange", "duration", "period"),
        fmt_field(e, "employmentType", "employment_type"),
        fmt_field(e, "location", "locationName"),
    ) if x)
    if meta:
        lines += ["", f"_{meta}_"]
    desc = fmt_field(e, "description", "summary")
    if desc:
        lines += ["", desc.strip()]
    lines.append("")
    return lines


def _edu_entry_md(e: dict) -> str:
    school = fmt_field(e, "schoolName", "school", "school.name", "institution",
                       "title") or "(school n/a)"
    degree = " ".join(x for x in (
        fmt_field(e, "degree", "degreeName"),
        fmt_field(e, "fieldOfStudy", "field"),
    ) if x)
    dates = _exp_dates(e) or fmt_field(e, "dateRange", "period")
    tail = " · ".join(x for x in (degree, dates) if x)
    return f"- **{school}**" + (f" — {tail}" if tail else "")


def render_profile_section_md(profile: dict | None) -> list[str]:
    """Markdown lines for current role + about + full career history + education,
    tolerant of varied Apify output key names."""
    if not profile:
        return []
    experience = _first_nonempty_list(
        profile, "experience", "experiences", "positions",
        "workExperience", "positionHistory", "jobs")
    education = _first_nonempty_list(
        profile, "education", "educations", "schools", "educationHistory")
    current = _first_nonempty_list(
        profile, "currentPosition", "currentPositions", "current")
    about = fmt_field(profile, "about", "summary", "description", "bio")

    lines = ["## Profile & career history", ""]
    cur = current[0] if current else (experience[0] if experience else None)
    if cur:
        title = fmt_field(cur, "position", "title", "role", "jobTitle")
        company = fmt_field(cur, "companyName", "company", "company.name")
        cur_line = " at ".join(x for x in (title, company) if x)
        if cur_line:
            lines += [f"**Current role:** {cur_line}", ""]
    if about:
        lines += ["**About:**", "", about.strip(), ""]
    if experience:
        lines += ["**Career history:**", ""]
        for e in experience:
            lines += _exp_entry_md(e)
    if education:
        lines += ["**Education:**", ""]
        for e in education:
            lines.append(_edu_entry_md(e))
        lines.append("")
    if len(lines) == 2:  # header only -> nothing renderable was found
        lines += ["_No structured career history found in the scrape result._", ""]
    lines += ["---", ""]
    return lines


def build_report_md(name: str, role: str, profile_url: str, since, rows,
                    profile: dict | None, window_note: str | None) -> str:
    """Self-contained report: header + robust profile section + recent activity.
    Mirrors the Activity Researcher layout but uses the robust profile section so
    career history is not lost to output-key drift."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Public activity: {name}", "",
        f"- **Profile:** {profile_url}",
        f"- **Current role:** {role or 'n/a'}",
    ]
    if window_note:
        lines.append(f"- **{window_note}**")
    lines += [
        f"- **Window scanned:** since {since.date().isoformat()} (newest first)",
        f"- **Items found:** {len(rows)}",
        f"- **Generated:** {now}",
        "",
        "> Source: LinkedIn via Apify (harvestapi). Profile/career history plus "
        "posts and reposts by the profile.",
        "", "---", "",
    ]
    lines += render_profile_section_md(profile)
    lines += ["## Recent activity", ""]
    if not rows:
        lines.append(f"_{window_note or 'No posts or reposts found.'}_")
        return "\n".join(lines)
    for dt, it in rows:
        date_str = dt.date().isoformat() if dt else "date n/a"
        lines += [
            f"## {date_str} - {_item_type(it)}", "",
            _text(it).strip(), "",
            f"- **Engagement:** {_engagement(it)}",
            f"- **URL:** {_url(it) or 'n/a'}",
            "", "---", "",
        ]
    return "\n".join(lines)


def deep_scrape_person(apify: ApifyClient, person: dict) -> dict:
    """Deep-scrape one selected person with the SAME actors the Activity
    Researcher uses. Returns a result dict with a rendered markdown report.

    If the discovery step did not yield a LinkedIn URL, we first resolve one by
    name (cheap search actor) before scraping — reusing the existing resolve
    helper so no new actor/config is invented."""
    name = person.get("name", "")
    url = (person.get("linkedin_url") or "").strip()

    if not url:
        first, last = split_name(name)
        location = person.get("location") or None
        try:
            items = resolve_candidates(apify, first, last, location)
        except ApifyError:
            items = []
        if items:
            url = candidate_view(items[0]).get("url", "")

    if not url:
        return {"name": name, "ok": False,
                "error": "No LinkedIn URL found (discovery and name-resolve both empty)."}

    clean_url = url.rstrip("/")

    # 1) Full profile: identity, full career history, about. (confirm actor)
    profile = None
    try:
        profile = scrape_profile(apify, clean_url)
    except ApifyError as exc:
        # Non-fatal: we can still report posts even if the profile call failed.
        profile = None
        profile_err = str(exc)
    else:
        profile_err = ""

    # Log the raw Apify profile response so we can confirm whether career history
    # is present in the scrape (and under which keys) vs lost during formatting.
    if profile is not None:
        try:
            log.info("Raw Apify profile for %s — top-level keys: %s",
                     name, sorted(profile.keys()))
            log.info("Raw Apify profile JSON for %s: %s",
                     name, json.dumps(profile)[:4000])
        except Exception:
            pass

    # 2) Recent posts/reposts, widening the window if empty. (posts actor)
    try:
        rows, used_days, since = scrape_activity_tiered(apify, clean_url, MAX_POSTS)
    except ApifyError as exc:
        return {"name": name, "ok": False, "url": clean_url,
                "error": f"Apify error scraping posts: {exc}"}

    note = activity_note(used_days)
    display_name = (fmt_field(profile or {}, "name", "fullName") or name)
    role = _role_from_profile(profile, person.get("title", ""),
                              person.get("location", ""))
    md = build_report_md(display_name, role, clean_url, since, rows,
                         profile=profile, window_note=note)

    return {
        "name": display_name,
        "ok": True,
        "url": clean_url,
        "role": role,
        "report_md": md,
        "post_count": len(rows),
        "note": note,
        "profile_err": profile_err,
        "raw_profile": profile,   # kept for the in-app debug view
    }


# --------------------------------------------------------------------------- #
# Stage 5: CSV
# --------------------------------------------------------------------------- #
def build_csv_df(company: str, candidates: list[dict],
                 scraped: dict[str, dict]) -> pd.DataFrame:
    """Combine discovery results + scraped detail into the tracker schema."""
    rows = []
    for c in candidates:
        s = scraped.get(c["name"])
        bg = c.get("background", "")
        if s and s.get("ok"):
            extra = f"Scraped: {s.get('role', '')} | {s.get('post_count', 0)} recent posts"
            bg = (bg + "  ||  " + extra).strip(" |")
        rows.append({
            "Company": company,
            "Name": c.get("name", ""),
            "Title": c.get("title", ""),
            "Location": c.get("location", ""),
            "LinkedIn URL": (s.get("url") if s and s.get("ok") else c.get("linkedin_url", "")),
            "Background notes": bg,
            "Hook notes": "",       # blank — to fill in after the chat
            "Email status": "",
            "Date contacted": "",
            "Notes": "",
        })
    return pd.DataFrame(rows, columns=CSV_COLUMNS)


# --------------------------------------------------------------------------- #
# Stage 6: Embedded Claude drafting chat
# --------------------------------------------------------------------------- #
DRAFT_SYSTEM = f"""You are helping with New Zealand job-search outreach. Here is \
who the person you are helping is, in their own words — use this for the "who I \
am" line in every email:

- An operations and AI automation professional.
- Background in personal lines insurance underwriting and compliance at NFU \
Mutual in the UK (in-house agents, not external brokers).
- Startup operations experience.
- Builds AI automation workflows and their own automation tools.

YOUR JOB:
Help them identify ONE specific, genuine hook from each person's profile — a \
recent post, a role change, a project, something concrete and real, never \
generic. Then help them draft a short, warm outreach email asking for a ten \
minute virtual coffee chat.

EMAIL RULES (all non-negotiable):
- Open with "Kia ora [first name]".
- One specific congratulations or observation that is the hook.
- One and a half lines on who I am.
- The hook question.
- A ten minute korero request, never fifteen.
- Sign off with "Ngā mihi".
- 90 to 130 words total.
- No em dashes anywhere. No hyphens in the body copy.
- UK and New Zealand spelling throughout.
- No corporate filler words. Never use "leveraging", "genuinely", "toolkit", \
or "shipping".
- Informal, warm tone.
- Never use the phrase "I came across your profile".
- Never mention jobs, a visa, or relocation.
- End every draft with a reminder to me to BCC my consultant {CONSULTANT_NAME} \
on the send.

The scraped profile data for the people in play is provided below as your source \
material. Base every hook on something concrete in it; if the data does not \
support a genuine hook for someone, say so plainly rather than inventing one."""


def draft_context(scraped: dict[str, dict], candidates: list[dict]) -> str:
    """Assemble the source material for the drafting chat. Prefer the rich
    scraped reports; fall back to the discovery summaries if nothing is scraped
    yet, so the chat is still useful."""
    blocks: list[str] = []
    for name, s in scraped.items():
        if s.get("ok") and s.get("report_md"):
            blocks.append(s["report_md"])
    if blocks:
        return "\n\n".join(blocks)
    # Fallback: discovery-only context.
    lines = ["(No deep scrapes yet — discovery summaries only.)", ""]
    for c in candidates:
        lines.append(
            f"## {c['name']} — {c.get('title', '')}\n"
            f"- Location: {c.get('location', '')}\n"
            f"- LinkedIn: {c.get('linkedin_url', '')}\n"
            f"- Background: {c.get('background', '')}"
        )
    return "\n".join(lines)


def run_draft_chat(ai, prompt: str, scraped: dict, candidates: list[dict]) -> str:
    """One turn of the drafting chat. Returns the assistant's reply text."""
    system = DRAFT_SYSTEM + "\n\n=== SCRAPED PROFILE DATA (your source) ===\n" \
        + draft_context(scraped, candidates)
    history = list(st.session_state.get("or_chat", []))
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": prompt})
    resp = ai.messages.create(
        model=CHAT_MODEL,
        max_tokens=1500,
        system=system,
        messages=messages,
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


# --------------------------------------------------------------------------- #
# Persistent research history (linkedin_research/outreach_history.json)
# --------------------------------------------------------------------------- #
# A single JSON file holding a LIST of session entries — survives page refreshes
# and app sleep cycles (st.session_state alone does not). Mirrors the Activity
# Researcher's history pattern, but as one append-only file rather than one file
# per session.
HISTORY_PATH = os.path.join(_HERE, "outreach_history.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_ts(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        return ""


# --- Optional cloud sync (Supabase Storage) -------------------------------- #
# Streamlit Community Cloud wipes the container disk on reboot/redeploy, so the
# local file alone does not survive. If SUPABASE_URL + SUPABASE_KEY are set in
# secrets, the history is synced to a private Supabase Storage bucket (the source
# of truth), with the local file kept as a fast cache + offline fallback. With
# no Supabase secrets, behaviour is exactly as before: local file only.
# Uses the Storage REST API via `requests` — no extra dependency.
OBJECT_NAME = "outreach_history.json"


def _sb_config() -> tuple[str, str, str] | None:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    bucket = os.environ.get("SUPABASE_BUCKET", "").strip() or "outreach-history"
    return url, key, bucket


def cloud_status() -> str:
    return ("☁️ Synced to Supabase" if _sb_config()
            else "💾 Local only — set SUPABASE_URL/KEY to sync across reboots")


def _sb_endpoint(url: str, bucket: str) -> str:
    return f"{url}/storage/v1/object/{bucket}/{OBJECT_NAME}"


def _sb_download() -> tuple[bool, list[dict]]:
    """(ok, sessions). ok=False means configured but the fetch failed (so the
    caller should fall back to the local cache rather than assume empty)."""
    cfg = _sb_config()
    if not cfg:
        return False, []
    url, key, bucket = cfg
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    try:
        r = requests.get(_sb_endpoint(url, bucket), headers=headers, timeout=20)
    except Exception:
        return False, []
    if r.status_code == 200:
        try:
            data = r.json()
        except ValueError:
            return True, []
        return True, (data if isinstance(data, list) else [])
    if r.status_code in (400, 404):
        return True, []  # object not created yet -> genuinely empty
    return False, []     # auth/other error -> let caller use local cache


def _sb_upload(sessions: list[dict]) -> bool:
    cfg = _sb_config()
    if not cfg:
        return False
    url, key, bucket = cfg
    headers = {
        "Authorization": f"Bearer {key}", "apikey": key,
        "Content-Type": "application/json", "x-upsert": "true",
    }
    payload = json.dumps(sessions, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        r = requests.post(_sb_endpoint(url, bucket), headers=headers,
                          data=payload, timeout=20)
        return r.status_code in (200, 201)
    except Exception:
        return False


def _read_local() -> list[dict]:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _write_local(sessions: list[dict]) -> None:
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
            json.dump(sessions, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass  # a persistence hiccup must never break the page


def load_history() -> list[dict]:
    """All saved sessions. Cloud is source of truth when configured; otherwise
    the local file. Never raises."""
    if _sb_config():
        ok, data = _sb_download()
        if ok:
            _write_local(data)   # refresh the local cache
            return data
        return _read_local()     # cloud unreachable -> use cache
    return _read_local()


def _write_history(sessions: list[dict]) -> None:
    _write_local(sessions)       # always keep a local cache
    if _sb_config():
        _sb_upload(sessions)     # and push to the durable cloud store


def upsert_session(entry: dict) -> None:
    """Append a new session, or update the existing one with the same id."""
    sessions = load_history()
    for i, s in enumerate(sessions):
        if s.get("id") == entry.get("id"):
            sessions[i] = entry
            break
    else:
        sessions.append(entry)
    _write_history(sessions)


def delete_history_session(sid: str) -> None:
    _write_history([s for s in load_history() if s.get("id") != sid])


def persist_current_session() -> None:
    """Snapshot the active research into the history file (append or update).
    Called once after a Perplexity run and again after each Apify scrape."""
    ss = st.session_state
    if not ss.get("or_session_id"):
        return
    upsert_session({
        "id": ss.or_session_id,
        "company": ss.get("or_company", ""),
        "timestamp": ss.get("or_created_at") or _now_iso(),
        "personas": ss.get("or_personas", []),
        "depth": ss.get("or_depth", "Medium"),
        "context": ss.get("or_context", ""),
        "candidates": ss.get("or_candidates", []),   # full discovery rows
        "scraped": ss.get("or_scraped", {}),          # name -> scrape result
        "raw_text": ss.get("or_raw_text", ""),
    })


def load_session_into_state(sid: str) -> bool:
    """Restore a saved session into the page exactly as it was. The form input
    keys (or_in_*) are set too, so the form repopulates on the next run."""
    for s in load_history():
        if s.get("id") != sid:
            continue
        ss = st.session_state
        ss.or_session_id = s.get("id", "")
        ss.or_created_at = s.get("timestamp", "")
        ss.or_company = s.get("company", "")
        ss.or_personas = s.get("personas", [])
        ss.or_depth = s.get("depth", "Medium")
        ss.or_context = s.get("context", "")
        ss.or_candidates = s.get("candidates", [])
        ss.or_scraped = s.get("scraped", {})
        ss.or_raw_text = s.get("raw_text", "")
        ss.or_chat = []  # fresh drafting chat for the loaded company
        # Repopulate the form inputs (widget-backed keys).
        ss.or_in_company = ss.or_company
        ss.or_in_personas = ss.or_personas
        ss.or_in_depth = ss.or_depth if ss.or_depth in ("Low", "Medium", "High") else "Medium"
        ss.or_in_context = ss.or_context
        return True
    return False


def clear_active_view() -> None:
    """Reset the open research view (used when the active session is deleted)."""
    ss = st.session_state
    ss.or_session_id = ""
    ss.or_created_at = ""
    ss.or_candidates = []
    ss.or_scraped = {}
    ss.or_raw_text = ""


def new_search() -> None:
    """Clear all current session state back to a blank form for a new company.
    Does NOT touch the history store — past sessions stay listed in the sidebar."""
    ss = st.session_state
    clear_active_view()
    ss.or_chat = []
    ss.or_pending_delete = None
    # Logical form state.
    ss.or_company = ""
    ss.or_personas = list(DEFAULT_PERSONAS)
    ss.or_depth = "Medium"
    ss.or_context = ""
    # Widget-backed form keys (so the form renders blank/defaults on rerun).
    ss.or_in_company = ""
    ss.or_in_personas = list(DEFAULT_PERSONAS)
    ss.or_in_depth = "Medium"
    ss.or_in_context = ""


def render_history_sidebar() -> None:
    """Scrollable list of past sessions (company + date) with open + delete."""
    ss = st.session_state
    with st.sidebar:
        if st.button("✨ New search", use_container_width=True,
                     key="or_new_search"):
            new_search()
            st.rerun()
        st.header("🗂️ Research history")
        st.caption(cloud_status())
        sessions = sorted(load_history(),
                          key=lambda s: s.get("timestamp", ""), reverse=True)
        if not sessions:
            st.caption("No saved research yet. Run a search to start.")
            return
        box = st.container(height=420)  # fixed-height -> scrollable list
        with box:
            for s in sessions:
                sid = s.get("id")
                active = sid == ss.get("or_session_id")
                label = (f"{s.get('company') or 'Untitled'}  ·  "
                         f"{_fmt_ts(s.get('timestamp', ''))}")
                open_col, del_col = st.columns([0.8, 0.2])
                with open_col:
                    if st.button(label, key=f"or_open_{sid}",
                                 use_container_width=True,
                                 type="primary" if active else "secondary"):
                        load_session_into_state(sid)
                        ss.or_pending_delete = None
                        st.rerun()
                with del_col:
                    if st.button("🗑", key=f"or_del_{sid}",
                                 use_container_width=True,
                                 help="Delete this session"):
                        ss.or_pending_delete = sid
                        st.rerun()
                if ss.get("or_pending_delete") == sid:
                    st.caption(f"Delete “{s.get('company') or 'Untitled'}” research?")
                    yes_col, no_col = st.columns(2)
                    with yes_col:
                        if st.button("✅ Delete", key=f"or_delyes_{sid}",
                                     use_container_width=True):
                            delete_history_session(sid)
                            if active:
                                clear_active_view()
                            ss.or_pending_delete = None
                            st.rerun()
                    with no_col:
                        if st.button("✖ Cancel", key=f"or_delno_{sid}",
                                     use_container_width=True):
                            ss.or_pending_delete = None
                            st.rerun()


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
def init_state() -> None:
    ss = st.session_state
    ss.setdefault("or_company", "")
    ss.setdefault("or_candidates", [])      # list of discovery dicts
    ss.setdefault("or_raw_text", "")        # raw assistant text (debug aid)
    ss.setdefault("or_scraped", {})         # name -> deep_scrape_person result
    ss.setdefault("or_chat", [])            # [{role, content}]
    # Persistent-history bookkeeping.
    ss.setdefault("or_session_id", "")
    ss.setdefault("or_created_at", "")
    ss.setdefault("or_personas", list(DEFAULT_PERSONAS))
    ss.setdefault("or_depth", "Medium")
    ss.setdefault("or_context", "")
    ss.setdefault("or_pending_delete", None)
    # Form-input widget keys (so loaded sessions repopulate the form).
    ss.setdefault("or_in_company", "")
    ss.setdefault("or_in_personas", list(DEFAULT_PERSONAS))
    ss.setdefault("or_in_depth", "Medium")
    ss.setdefault("or_in_context", "")


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def render_copy_button(text: str, key: str) -> None:
    """Small inline 'copy raw text' button. Copies the underlying raw report via
    navigator.clipboard.writeText(); falls back to execCommand for sandboxed
    iframes. No raw text is shown on the page."""
    payload = json.dumps(text)          # safe JS string literal
    safe_key = re.sub(r"\W+", "_", key) or "x"
    bid = f"orcopy_{safe_key}"
    html = f"""
    <button id="{bid}" style="font-size:12px;padding:3px 10px;border:1px solid #ccc;
        border-radius:6px;background:#f6f6f6;cursor:pointer;">📋 Copy raw text</button>
    <span id="{bid}_m" style="font-size:12px;margin-left:8px;color:#1a7f37;"></span>
    <script>
    (function() {{
      const btn = document.getElementById("{bid}");
      const msg = document.getElementById("{bid}_m");
      const text = {payload};
      btn.addEventListener("click", async function() {{
        try {{
          await navigator.clipboard.writeText(text);
          msg.textContent = "Copied!";
        }} catch (e) {{
          const ta = document.createElement("textarea");
          ta.value = text; document.body.appendChild(ta); ta.select();
          try {{ document.execCommand("copy"); msg.textContent = "Copied!"; }}
          catch (_) {{ msg.textContent = "Copy failed"; }}
          document.body.removeChild(ta);
        }}
      }});
    }})();
    </script>
    """
    components.html(html, height=38)


def main() -> None:
    st.set_page_config(page_title="NZ Outreach Researcher", page_icon="🇳🇿",
                       layout="wide")
    load_secrets_into_env()  # bridge st.secrets -> os.environ (Cloud + local)
    init_state()
    ss = st.session_state

    render_history_sidebar()  # persistent past-sessions list (left sidebar)

    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    apify_token = os.environ.get("APIFY_TOKEN", "").strip()
    anthropic_ready = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    st.title("NZ Outreach Researcher")
    st.caption(
        f"Perplexity {'✅' if pplx_key else '❌'}  ·  "
        f"Apify {'✅' if apify_token else '❌'}  ·  "
        f"Claude {'✅' if anthropic_ready else '❌'}"
    )

    # ------------------------------------------------------------------ #
    # Input form
    # ------------------------------------------------------------------ #
    # Inputs are bound to session_state keys (or_in_*) so loading a past session
    # repopulates the form. Initial values are seeded in init_state().
    with st.form("research_form"):
        company = st.text_input("Company name", key="or_in_company",
                                placeholder="e.g. Tower Insurance")
        personas = st.multiselect("Target personas", PERSONA_OPTIONS,
                                  key="or_in_personas")
        depth = st.selectbox(
            "Research depth", ["Low", "Medium", "High"], key="or_in_depth",
            help="Maps to Perplexity search context size. Low = fastest/cheapest, "
                 "High = deepest.",
        )
        context = st.text_area(
            "Additional context", key="or_in_context",
            placeholder="e.g. Only New Zealand based employees. Focus on "
                        "underwriting, transformation, operations, and AI roles. "
                        "Managers and senior departmental members only.",
        )
        submitted = st.form_submit_button("🔎 Research prospects",
                                          use_container_width=True)

    if submitted:
        if not company.strip():
            st.error("Enter a company name first.")
        elif not pplx_key:
            st.error("PERPLEXITY_API_KEY is not set. Add it to Streamlit secrets "
                     "and reload.")
        else:
            ss.or_company = company.strip()
            ss.or_personas = personas
            ss.or_depth = depth
            ss.or_context = context
            with st.spinner(f"Researching prospects at {company.strip()} via "
                            f"Perplexity ({depth} depth)…"):
                try:
                    candidates, raw_text = discover_people(
                        pplx_key, company.strip(), personas, depth, context)
                    ss.or_candidates = candidates
                    ss.or_raw_text = raw_text
                    ss.or_scraped = {}   # fresh discovery -> clear old scrapes
                    # New session per run; save it to the persistent history file.
                    ss.or_session_id = uuid.uuid4().hex
                    ss.or_created_at = _now_iso()
                    persist_current_session()
                except Exception as exc:
                    st.error(f"Discovery failed: {exc}")
            if ss.or_candidates:
                st.success(f"Found {len(ss.or_candidates)} candidate(s).")
            elif ss.or_raw_text:
                st.warning("No structured candidates parsed from the response. "
                           "See the raw response below and try refining your "
                           "context.")
                with st.expander("Raw Perplexity response"):
                    st.write(ss.or_raw_text)

    # ------------------------------------------------------------------ #
    # Stage 3: shortlist (data_editor with checkbox)
    # ------------------------------------------------------------------ #
    selected_people: list[dict] = []
    if ss.or_candidates:
        st.subheader("Shortlist")
        st.caption("Tick the people you want to deep-scrape, then scrape.")

        base = pd.DataFrame(ss.or_candidates)
        display = pd.DataFrame({
            "Select": [False] * len(base),
            "Name": base["name"],
            "Title": base["title"],
            "Location": base["location"],
            "Background": base["background"],
            "LinkedIn URL": base["linkedin_url"],
        })
        edited = st.data_editor(
            display,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Select": st.column_config.CheckboxColumn("Select", default=False),
                "LinkedIn URL": st.column_config.LinkColumn("LinkedIn URL"),
                "Background": st.column_config.TextColumn("Background", width="large"),
            },
            # Per-session key so switching sessions gives a fresh, correct editor.
            key=f"or_editor_{ss.or_session_id or 'new'}",
        )

        # Save the current session (discovery only, no scrape needed) so it can
        # be reopened later to scrape specific people.
        if st.button("💾 Save to history", use_container_width=True,
                     key="or_save_btn"):
            if not ss.or_session_id:
                ss.or_session_id = uuid.uuid4().hex
                ss.or_created_at = _now_iso()
            persist_current_session()
            st.success("Saved to history.")

        # Map the ticked rows back to the original candidate dicts (by position).
        for i, keep in enumerate(edited["Select"].tolist()):
            if keep:
                selected_people.append(ss.or_candidates[i])

        if st.button(f"🧲 Scrape selected profiles ({len(selected_people)})",
                     use_container_width=True, disabled=not selected_people):
            if not apify_token:
                st.error("APIFY_TOKEN is not set. Add it to Streamlit secrets "
                         "and reload.")
            else:
                apify = ApifyClient(token=apify_token)
                progress = st.progress(0.0, text="Starting deep scrape…")
                for n, person in enumerate(selected_people, 1):
                    progress.progress(
                        (n - 1) / len(selected_people),
                        text=f"Scraping {person['name']} ({n}/{len(selected_people)})…",
                    )
                    try:
                        result = deep_scrape_person(apify, person)
                    except Exception as exc:
                        result = {"name": person["name"], "ok": False,
                                  "error": f"Unexpected error: {exc}"}
                    ss.or_scraped[person["name"]] = result
                progress.progress(1.0, text="Deep scrape complete.")
                # Update this session's record with the freshly scraped profiles.
                persist_current_session()

    # ------------------------------------------------------------------ #
    # Stage 4 display: scraped profiles in expanders
    # ------------------------------------------------------------------ #
    if ss.or_scraped:
        st.subheader("Scraped profiles")
        for name, s in ss.or_scraped.items():
            ok = s.get("ok")
            label = f"{'📄' if ok else '⚠️'}  {name}"
            if ok:
                label += f"  ·  {s.get('post_count', 0)} recent posts"
            with st.expander(label, expanded=False):
                if ok:
                    if s.get("profile_err"):
                        st.caption(f"(profile step note: {s['profile_err']})")
                    report = s.get("report_md", "(no content)")
                    # Small, unobtrusive copy button (no raw markdown on the page).
                    render_copy_button(report, key=name)
                    # Clean, formatted report (headers, bold, bullets) — only this.
                    st.markdown(report)
                    # Debug: raw Apify profile JSON, to confirm scrape contents.
                    if s.get("raw_profile"):
                        with st.expander("🔧 Raw scrape data (debug)",
                                         expanded=False):
                            st.json(s["raw_profile"])
                else:
                    st.error(s.get("error", "Scrape failed."))

    # ------------------------------------------------------------------ #
    # Stage 5: CSV download
    # ------------------------------------------------------------------ #
    if ss.or_candidates:
        st.subheader("Export")
        df = build_csv_df(ss.or_company, ss.or_candidates, ss.or_scraped)
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        fname = f"outreach-{slugify(ss.or_company or 'company')}-" \
                f"{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
        st.download_button("⬇️  Download CSV", data=csv_bytes, file_name=fname,
                           mime="text/csv", use_container_width=True)

    # ------------------------------------------------------------------ #
    # Stage 6: embedded Claude drafting chat
    # ------------------------------------------------------------------ #
    st.subheader("✍️  Hook + email drafting (Claude)")
    if not anthropic_ready:
        st.info("Add ANTHROPIC_API_KEY to Streamlit secrets to enable the "
                "drafting chat.")
        return
    if not ss.or_scraped and not ss.or_candidates:
        st.caption("Research and scrape some people first, then draft here.")

    ai = get_anthropic()
    if ai is None:
        st.warning("Anthropic client unavailable (check the key / SDK install).")
        return

    st.caption("The scraped profile data is available to Claude as context. Ask "
               "it to find a hook and draft the email for a specific person.")
    for m in ss.or_chat:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    prompt = st.chat_input("e.g. Find a hook for Jane Doe and draft her email")
    if prompt:
        ss.or_chat.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Drafting…"):
                try:
                    reply = run_draft_chat(ai, prompt, ss.or_scraped,
                                           ss.or_candidates)
                except Exception as exc:
                    reply = f"⚠️ Chat error: {exc}"
            st.markdown(reply)
        ss.or_chat.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
