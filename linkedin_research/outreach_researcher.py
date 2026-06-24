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
import time
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

# --- Bright Data (PRIMARY source for deep profile data) -------------------- #
# Bright Data's Web Scraper API is asynchronous: trigger -> poll progress ->
# fetch snapshot. It is the primary source for career history / education /
# about / current position. Apify harvestapi remains the SOLE source for posts,
# and is also the automatic fallback for profile data when Bright Data fails or
# times out on a given profile.
BRIGHTDATA_TRIGGER_URL = "https://api.brightdata.com/datasets/v3/trigger"
BRIGHTDATA_PROGRESS_URL = "https://api.brightdata.com/datasets/v3/progress"
BRIGHTDATA_SNAPSHOT_URL = "https://api.brightdata.com/datasets/v3/snapshot"
# Bright Data "LinkedIn people profiles" dataset id. VERIFY this against your
# Bright Data dashboard under Web Scraper IDE and update it here if your account
# shows a different ID.
BRIGHTDATA_PROFILE_DATASET_ID = "gd_l1viktl72bvl7bjuj0"
BRIGHTDATA_TIMEOUT_S = 90        # give up polling after ~90s -> Apify fallback
BRIGHTDATA_POLL_INTERVAL_S = 5   # seconds between progress checks
# Note: Bright Data also offers a separate LinkedIn *Posts* dataset that could
# serve as a future fallback for the posts section. Not built now — Apify
# harvestapi stays the sole posts source.

# Research depth -> Perplexity behaviour. Depth drives the web_search
# `search_context_size` (low/medium/high, the documented token budgets), and we
# also scale the model tier, reasoning effort, agent steps, the people_search
# token budget, and `max_people` (the requested number of results) so "High"
# really is deeper AND returns more people. `max_people` is NOT an API limit —
# the Agent API has no max_results param; it is the count we ask the model for in
# the instructions (see research_instructions). All people_search models below
# are from the live docs' supported list; tweak freely.
# `max_output_tokens` is the RESPONSE generation budget (distinct from the
# people_search/web_search context budgets). It must be large enough for the full
# JSON array to finish: each person object is ~80-150 tokens, plus the model's
# reasoning tokens count against this budget too, so we size it generously per
# tier — otherwise the array gets truncated mid-object (the bug this fixes).
DEPTH_CONFIG = {
    "Low": {
        "model": "openai/gpt-5-mini",
        "search_context_size": "low",
        "effort": "low",
        "max_steps": 4,
        "people_tokens": 8000,
        "max_people": 15,
        "max_output_tokens": 8000,
    },
    "Medium": {
        "model": "openai/gpt-5",
        "search_context_size": "medium",
        "effort": "medium",
        "max_steps": 7,
        "people_tokens": 16000,
        "max_people": 30,
        "max_output_tokens": 16000,
    },
    "High": {
        "model": "openai/gpt-5.5",
        "search_context_size": "high",
        "effort": "high",
        "max_steps": 10,
        "people_tokens": 28000,
        "max_people": 50,
        "max_output_tokens": 32000,
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
        # Response generation budget — must fit the full JSON array (see config).
        "max_output_tokens": cfg["max_output_tokens"],
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


def _strip_fences(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    return t


def _strict_people_json(text: str) -> list[dict]:
    """Parse a COMPLETE, well-formed people array (or {key: [...]} wrapper).
    Returns [] if the JSON does not fully parse."""
    t = _strip_fences(text)
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


def _recover_objects(text: str) -> list[dict]:
    """Salvage every COMPLETE top-level {...} object from a (possibly truncated)
    string, ignoring the final incomplete one. String-aware brace matching so
    braces/quotes inside string values don't confuse the scan."""
    objs: list[dict] = []
    depth = 0
    start: int | None = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    frag = text[start:i + 1]
                    try:
                        d = json.loads(frag)
                        if isinstance(d, dict):
                            objs.append(d)
                    except Exception:
                        pass
                    start = None
    return objs


def parse_people(text: str) -> tuple[list[dict], bool]:
    """Return (people, partial). `partial` is True when the response could not be
    parsed as complete JSON and we recovered whatever individual objects did
    finish (i.e. the response was cut short)."""
    if not text:
        return [], False
    people = _strict_people_json(text)
    if people:
        return people, False
    # Truncated / malformed: salvage the complete objects that did come through.
    recovered = _recover_objects(_strip_fences(text))
    return recovered, bool(recovered)


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
    """Run discovery. Returns (candidates, raw_assistant_text, partial).
    `partial` is True when the response was cut short and we recovered only the
    candidates that fully completed."""
    instructions = research_instructions(company, DEPTH_CONFIG[depth]["max_people"])
    query = build_research_query(company, personas, context)
    resp = call_perplexity(api_key, depth, instructions, query)
    text = _output_text(resp)
    raw_sources = _gather_raw_sources(resp)
    people, partial = parse_people(text)
    candidates = [_normalise_person(p, raw_sources) for p in people]
    # Drop entries with no name at all; keep order (best matches first).
    candidates = [c for c in candidates if c["name"]]
    return candidates, text, partial


# --------------------------------------------------------------------------- #
# Stage 4: Apify deep scrape (selected people only) — reuses the proven pipeline
# --------------------------------------------------------------------------- #
def _role_from_profile(profile: dict | None, fallback_title: str,
                       fallback_location: str) -> str:
    if not profile:
        return " | ".join(x for x in (fallback_title, fallback_location) if x)
    # `position`/`city` cover the Bright Data shape; the rest cover Apify.
    headline = fmt_field(profile, "headline", "occupation", "position")
    location = fmt_field(profile, "location.linkedinText",
                         "location.parsed.text", "location", "city")
    return (" | ".join(x for x in (headline, location) if x)
            or " | ".join(x for x in (fallback_title, fallback_location) if x))


# --- Robust profile rendering ---------------------------------------------- #
# The shared render_markdown/render_profile_section in research_person.py reads
# only `experience` / `currentPosition` / `about`. The renderer below tries every
# plausible key spelling so career history survives both harvestapi (Apify) AND
# Bright Data output shapes (whose field names differ), and any future drift.
def _first_nonempty_list(profile: dict, *keys: str) -> list:
    for k in keys:
        v = profile.get(k)
        if isinstance(v, list) and v:
            return v
    return []


def _has_career_history(profile: dict | None) -> bool:
    """True only when the profile dict contains meaningful career content.
    Intentionally excludes Bright Data's current_company dict and bare
    position string — both are present even on null-career-history responses
    and were causing false positives that suppressed the Apify fallback."""
    if not profile:
        return False
    has_exp = bool(_first_nonempty_list(
        profile, "experience", "experiences", "positions",
        "workExperience", "positionHistory", "jobs"))
    has_edu = bool(_first_nonempty_list(
        profile, "education", "educations", "schools", "educationHistory"))
    has_about = bool(fmt_field(profile, "about", "summary", "bio"))
    # currentPosition from Apify is a full structured array — genuine career data.
    # current_company from Bright Data is a name-only dict — excluded deliberately.
    has_cur_pos_array = bool(_first_nonempty_list(
        profile, "currentPosition", "currentPositions", "current"))
    return has_exp or has_edu or has_about or has_cur_pos_array


def _entry_dates(e: dict) -> str:
    """Date range for one experience/education entry, across shapes:
    Apify (startDate.text/endDate.text), Bright Data (start_date/end_date or
    start_year/end_year), plus a pre-formatted duration/period if present."""
    apify = _exp_dates(e)  # Apify's startDate.text – endDate.text · duration
    if apify:
        return apify
    start = fmt_field(e, "start_date", "startDate", "start_year", "starts_at")
    end = fmt_field(e, "end_date", "endDate", "end_year", "ends_at")
    duration = fmt_field(e, "duration", "dateRange", "period")
    rng = f"{start or '?'} – {end or 'Present'}" if (start or end) else ""
    return " · ".join(x for x in (rng, duration) if x)


def _exp_entry_md(e: dict) -> list[str]:
    title = fmt_field(e, "position", "title", "role", "jobTitle") or "(role n/a)"
    company = fmt_field(e, "companyName", "company", "company.name",
                        "organisation", "organization")
    head = title + (f" — {company}" if company else "")
    lines = [f"### {head}"]
    meta = " · ".join(x for x in (
        _entry_dates(e),
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
    dates = _entry_dates(e)
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
    # Current role: Apify exposes a currentPosition list; Bright Data exposes a
    # `position` string plus a `current_company` object — handle both, then fall
    # back to the most recent experience entry.
    cur_title = cur_company = ""
    if current:
        cur_title = fmt_field(current[0], "position", "title", "role", "jobTitle")
        cur_company = fmt_field(current[0], "companyName", "company", "company.name")
    if not (cur_title or cur_company):
        cur_title = fmt_field(profile, "position")
        cc = profile.get("current_company")
        if isinstance(cc, dict):
            cur_company = fmt_field(cc, "name", "company_name")
            cur_title = cur_title or fmt_field(cc, "title")
        cur_company = cur_company or fmt_field(profile, "current_company_name")
    if not (cur_title or cur_company) and experience:
        cur_title = fmt_field(experience[0], "position", "title", "role", "jobTitle")
        cur_company = fmt_field(experience[0], "companyName", "company", "company.name")
    cur_line = " at ".join(x for x in (cur_title, cur_company) if x)
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


def render_extras_md(profile: dict | None) -> list[str]:
    """Certifications + recommendations from Bright Data (which returns these
    well even when its career fields are null). Tolerant of field-name variants;
    renders nothing when absent."""
    if not profile:
        return []
    lines: list[str] = []
    certs = _first_nonempty_list(profile, "certifications", "certification",
                                 "licenses_and_certifications", "licenses")
    if certs:
        lines += ["## Certifications", ""]
        for c in certs:
            if not isinstance(c, dict):
                lines.append(f"- {c}")
                continue
            title = fmt_field(c, "title", "name", "subtitle") or "(certification)"
            issuer = fmt_field(c, "subtitle", "issuer", "organization",
                               "authority", "company")
            meta = fmt_field(c, "meta", "date", "issued", "credential_id")
            tail = " · ".join(x for x in (issuer, meta) if x and x != title)
            lines.append(f"- **{title}**" + (f" — {tail}" if tail else ""))
        lines += ["", "---", ""]

    recs = _first_nonempty_list(profile, "recommendations",
                                "recommendations_received")
    rec_count = fmt_field(profile, "recommendations_count")
    if recs or rec_count:
        lines += ["## Recommendations", ""]
        if rec_count:
            lines.append(f"_{rec_count} recommendation(s) received._")
            lines.append("")
        for r in (recs or [])[:5]:
            if isinstance(r, dict):
                txt = fmt_field(r, "text", "recommendation", "description", "body")
                who = fmt_field(r, "name", "author", "recommender", "recommender_name")
                if txt:
                    lines.append(f"> {txt}" + (f"  \n> — {who}" if who else ""))
            elif isinstance(r, str) and r.strip():
                lines.append(f"> {r.strip()}")
        lines += ["", "---", ""]
    return lines


def build_report_md(name: str, role: str, profile_url: str, since, rows,
                    profile: dict | None, window_note: str | None,
                    profile_source: str | None = None,
                    extras_profile: dict | None = None) -> str:
    """Self-contained report: header + robust profile section + recent activity.
    Mirrors the Activity Researcher layout but uses the robust profile section so
    career history is not lost to output-key drift. `profile_source` names which
    backend served the profile data (Bright Data or Apify fallback)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Public activity: {name}", "",
        f"- **Profile:** {profile_url}",
        f"- **Current role:** {role or 'n/a'}",
    ]
    if profile_source:
        lines.append(f"- **Profile data:** via {profile_source}")
    if window_note:
        lines.append(f"- **{window_note}**")
    src_note = (f"Profile/career history via {profile_source}. "
                if profile_source else "")
    lines += [
        f"- **Window scanned:** since {since.date().isoformat()} (newest first)",
        f"- **Items found:** {len(rows)}",
        f"- **Generated:** {now}",
        "",
        "> Source: LinkedIn. " + src_note
        + "Posts and reposts via Apify (harvestapi).",
        "", "---", "",
    ]
    lines += render_profile_section_md(profile)
    # Certifications + recommendations come from Bright Data (extras_profile)
    # even when career history was sourced from Apify.
    lines += render_extras_md(extras_profile)
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


# --- Bright Data async profile fetch (PRIMARY) ----------------------------- #
# NOTE on null career fields (experience/education/about/position)
# -----------------------------------------------------------------
# Observed: dataset gd_l1viktl72bvl7bjuj0 sometimes returns null for
# experience / education / about / position while still returning activity,
# certifications and recommendations. What was investigated/tried:
#   1. Dataset id — gd_l1viktl72bvl7bjuj0 is the correct "LinkedIn people
#      profile, collect by URL" dataset and its schema DOES include experience,
#      education, about and position (per Bright Data docs). It is not a
#      "basic vs full" split — there is no separate full-profile dataset id.
#   2. Trigger parameters — Bright Data exposes no "include_work_experience"
#      (or similar) flag on this dataset. The trigger body is simply
#      [{"url": ...}]; the dataset returns every field it manages to scrape.
#      (A separate "discover by name" trigger exists but takes names, not URLs,
#      and returns the same record schema.)
#   3. URL format — the docs' canonical form is https://www.linkedin.com/in/<id>
#      (https, www, no query string, no country subdomain). Perplexity/Apify can
#      hand us nz.linkedin.com/..., http://, or ?trk=... URLs, which can make the
#      deep fields come back null. So we now normalise the URL to the canonical
#      form before triggering (see _brightdata_profile_url).
# Conclusion: when Bright Data still returns null career history for a profile,
# Apify is used as the career-history source (see deep_scrape_person), while
# Bright Data stays primary for the data it returns well (activity,
# certifications, recommendations). Re-evaluate if Bright Data changes the
# dataset behaviour.
def _brightdata_profile_url(raw: str) -> str:
    """Canonicalise a LinkedIn profile URL to the form Bright Data expects:
    https://www.linkedin.com/in/<id> — strip query/fragment, force https + www,
    drop country subdomains (e.g. nz.linkedin.com)."""
    u = (raw or "").strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not u:
        return u
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    if not u.lower().startswith("https://"):
        u = "https://" + u.lstrip("/")
    # country subdomain (xx. / xxx.) or bare linkedin.com -> www.linkedin.com
    u = re.sub(r"https://(?:[a-z]{2,3}\.)?linkedin\.com",
               "https://www.linkedin.com", u, flags=re.I)
    return u


def brightdata_fetch_profile(profile_url: str) -> dict | None:
    """Fetch one LinkedIn profile via Bright Data's async Web Scraper API:
    trigger -> poll progress until 'ready' (or ~90s timeout) -> fetch snapshot.
    Returns the raw profile dict, or None on any failure/timeout so the caller
    falls back to Apify. Logs full HTTP status + body on every call."""
    key = os.environ.get("BRIGHTDATA_API_KEY", "").strip()
    if not key:
        log.info("Bright Data: BRIGHTDATA_API_KEY not set; using Apify for profile.")
        return None
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    target_url = _brightdata_profile_url(profile_url)
    log.info("Bright Data: requesting profile for normalised URL %s (from %s)",
             target_url, profile_url)

    # 1) Trigger a collection for this profile URL.
    try:
        r = requests.post(
            BRIGHTDATA_TRIGGER_URL,
            params={"dataset_id": BRIGHTDATA_PROFILE_DATASET_ID, "format": "json"},
            headers=headers, json=[{"url": target_url}], timeout=30)
    except Exception as exc:
        log.error("Bright Data TRIGGER raised for %s: %s", profile_url, exc)
        return None
    log.info("Bright Data TRIGGER %s -> HTTP %s: %s",
             profile_url, r.status_code, (r.text or "")[:500])
    if r.status_code not in (200, 201):
        return None
    try:
        snapshot_id = (r.json() or {}).get("snapshot_id")
    except Exception:
        snapshot_id = None
    if not snapshot_id:
        log.error("Bright Data TRIGGER returned no snapshot_id for %s", profile_url)
        return None

    # 2) Poll progress until ready, or give up after ~BRIGHTDATA_TIMEOUT_S.
    deadline = time.monotonic() + BRIGHTDATA_TIMEOUT_S
    status = None
    while time.monotonic() < deadline:
        try:
            p = requests.get(f"{BRIGHTDATA_PROGRESS_URL}/{snapshot_id}",
                             headers=headers, timeout=30)
        except Exception as exc:
            log.error("Bright Data PROGRESS raised for %s: %s", snapshot_id, exc)
            return None
        log.info("Bright Data PROGRESS %s -> HTTP %s: %s",
                 snapshot_id, p.status_code, (p.text or "")[:300])
        if p.status_code != 200:
            return None
        try:
            status = (p.json() or {}).get("status")
        except Exception:
            status = None
        if status == "ready":
            break
        if status in ("failed", "error"):
            log.error("Bright Data snapshot %s reported status=%s", snapshot_id, status)
            return None
        time.sleep(BRIGHTDATA_POLL_INTERVAL_S)
    if status != "ready":
        log.error("Bright Data snapshot %s not ready after %ss (last status=%s); "
                  "falling back to Apify.", snapshot_id, BRIGHTDATA_TIMEOUT_S, status)
        return None

    # 3) Fetch the snapshot results.
    try:
        s = requests.get(f"{BRIGHTDATA_SNAPSHOT_URL}/{snapshot_id}",
                         params={"format": "json"}, headers=headers, timeout=60)
    except Exception as exc:
        log.error("Bright Data SNAPSHOT raised for %s: %s", snapshot_id, exc)
        return None
    log.info("Bright Data SNAPSHOT %s -> HTTP %s: %s",
             snapshot_id, s.status_code, (s.text or "")[:500])
    if s.status_code != 200:
        return None
    try:
        data = s.json()
    except Exception as exc:
        log.error("Bright Data SNAPSHOT %s bad JSON: %s", snapshot_id, exc)
        return None
    # The snapshot is a list of records; take the first. Tolerate a {"data":[...]}
    # wrapper or a single object too.
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        if isinstance(data.get("data"), list) and data["data"]:
            return data["data"][0]
        return data
    return None


def perplexity_career_fallback(api_key: str, name: str, title: str,
                                company: str) -> dict | None:
    """Last-resort career history lookup via Perplexity when both Bright Data
    and Apify return no experience/education/about. Runs a cheap low-depth
    people_search + web_search to find a career narrative and returns a minimal
    profile dict with `about` populated, so _has_career_history returns True
    and render_profile_section_md renders the About block. Returns None on any
    failure so the caller can handle the total gap gracefully."""
    if not api_key or not name:
        return None
    who = " ".join(x for x in (name, title, company) if x)
    instructions = (
        f"You are a people-research assistant. Find the professional background "
        f"and career history of {name}"
        + (f", currently {title} at {company}" if (title or company) else "")
        + ". Summarise their previous roles, companies, and years of experience "
        "in 3 to 5 factual sentences. Return plain prose only — no invented "
        "details, no JSON, no bullet points, no headings."
    )
    body = {
        "model": "openai/gpt-5-mini",
        "instructions": instructions,
        "input": f"Career history and professional background of {who}",
        "reasoning": {"effort": "low"},
        "max_steps": 3,
        "max_output_tokens": 1000,
        "tools": [
            {"type": "people_search",
             "max_tokens": 4000, "max_tokens_per_page": 500},
            {"type": "web_search", "search_context_size": "low"},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    try:
        resp = requests.post(PPLX_ENDPOINT, headers=headers,
                             json=body, timeout=60)
        if resp.status_code >= 400:
            log.warning("Perplexity career fallback HTTP %s for %s: %s",
                        resp.status_code, name, (resp.text or "")[:300])
            return None
        text = _output_text(resp.json()).strip()
        if not text:
            return None
        log.info("Perplexity career fallback for %s returned %d chars", name, len(text))
        return {"name": name, "about": text}
    except Exception as exc:
        log.warning("Perplexity career fallback raised for %s: %s", name, exc)
        return None


def deep_scrape_person(apify: ApifyClient, person: dict) -> dict:
    """Deep-scrape one selected person.

    Bright Data is PRIMARY for profile data (and the data it returns well —
    certifications, recommendations). Career history specifically (experience /
    education / about / current position) falls back to the existing Apify
    deep-profile-scrape whenever Bright Data fails, times out, OR returns a
    record with those fields null (a known behaviour of dataset
    gd_l1viktl72bvl7bjuj0 — see the NOTE on brightdata_fetch_profile).

    Posts ALWAYS come from Apify (harvestapi) — untouched.

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

    # 1) Profile. Bright Data PRIMARY; Apify fills in career history when Bright
    #    Data lacks it (failure/timeout OR null career fields).
    bd_profile = brightdata_fetch_profile(clean_url)
    apify_profile = None
    profile_err = ""
    if not _has_career_history(bd_profile):
        try:
            apify_profile = scrape_profile(apify, clean_url)
        except ApifyError as exc:
            apify_profile = None
            profile_err = str(exc)

    # Choose the career-history source (the dict rendered for experience/
    # education/about/current). Prefer whichever actually has the data.
    if _has_career_history(apify_profile):
        career_profile, career_source = apify_profile, "Apify"
    elif _has_career_history(bd_profile):
        career_profile, career_source = bd_profile, "Bright Data"
    else:
        career_profile = bd_profile or apify_profile
        career_source = ("Bright Data" if bd_profile
                         else ("Apify" if apify_profile else None))

    # Third fallback: Perplexity career narrative when both BD and Apify fail.
    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not _has_career_history(career_profile) and pplx_key:
        pplx_career = perplexity_career_fallback(
            pplx_key, name,
            title=person.get("title", "")
                  or fmt_field(bd_profile or {}, "position") or "",
            company=person.get("company", "")
                    or fmt_field((bd_profile or {}).get("current_company") or {},
                                 "name") or "",
        )
        if _has_career_history(pplx_career):
            career_profile, career_source = pplx_career, "Perplexity"

    # Certifications/recommendations come from Bright Data when present.
    extras_profile = bd_profile
    extras_source = "Bright Data" if (bd_profile and render_extras_md(bd_profile)) else None

    for label, prof in (("Bright Data", bd_profile), ("Apify", apify_profile)):
        if prof is not None:
            try:
                log.info("Profile for %s via %s — keys: %s | has_career=%s",
                         name, label, sorted(prof.keys()), _has_career_history(prof))
                log.info("Raw %s profile JSON for %s: %s",
                         label, name, json.dumps(prof)[:4000])
            except Exception:
                pass

    # 2) Recent posts/reposts, widening the window if empty. ALWAYS Apify — this
    #    posts path is intentionally untouched. (Bright Data has its own Posts
    #    dataset that could be a future fallback here; not built now.)
    try:
        rows, used_days, since = scrape_activity_tiered(apify, clean_url, MAX_POSTS)
    except ApifyError as exc:
        return {"name": name, "ok": False, "url": clean_url,
                "error": f"Apify error scraping posts: {exc}"}

    note = activity_note(used_days)
    display_name = (fmt_field(career_profile or {}, "name", "fullName")
                    or fmt_field(bd_profile or {}, "name", "fullName") or name)
    role = _role_from_profile(career_profile, person.get("title", ""),
                              person.get("location", ""))
    md = build_report_md(display_name, role, clean_url, since, rows,
                         profile=career_profile, window_note=note,
                         profile_source=career_source, extras_profile=extras_profile)

    return {
        "name": display_name,
        "ok": True,
        "url": clean_url,
        "role": role,
        "report_md": md,
        "profile_source": career_source,   # career-history source (for the tag)
        "extras_source": extras_source,    # certs/recommendations source
        "post_count": len(rows),
        "note": note,
        "profile_err": profile_err,
        "raw_profile": bd_profile,            # Bright Data primary response
        "raw_profile_apify": apify_profile,   # Apify career-history fallback, if used
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


def _sb_object_url(url: str, bucket: str, name: str) -> str:
    return f"{url}/storage/v1/object/{bucket}/{name}"


def _sb_headers(key: str, extra: dict | None = None) -> dict:
    # Supabase needs BOTH apikey and Authorization. service_role key for both.
    h = {"Authorization": f"Bearer {key}", "apikey": key}
    if extra:
        h.update(extra)
    return h


def _sb_put_object(name: str, payload: bytes) -> tuple[bool, str]:
    """Create/overwrite a Storage object. POST with x-upsert; if that 4xx-fails
    (some setups don't honour x-upsert on POST for an existing key) retry with
    PUT. Logs the full HTTP status + body for every attempt. Returns (ok, detail)."""
    cfg = _sb_config()
    if not cfg:
        return False, "Supabase not configured"
    url, key, bucket = cfg
    target = _sb_object_url(url, bucket, name)
    headers = _sb_headers(key, {"Content-Type": "application/json",
                                "x-upsert": "true"})
    try:
        r = requests.post(target, headers=headers, data=payload, timeout=20)
    except Exception as exc:
        log.error("Supabase POST %s raised: %s", name, exc)
        return False, f"network error on POST: {exc}"
    log.info("Supabase POST %s (bucket=%s) -> HTTP %s: %s",
             name, bucket, r.status_code, (r.text or "")[:600])
    if r.status_code in (200, 201):
        return True, "ok"

    # Fallback: PUT (update an existing object).
    try:
        r2 = requests.put(target, headers=headers, data=payload, timeout=20)
    except Exception as exc:
        log.error("Supabase PUT %s raised: %s", name, exc)
        return False, f"network error on PUT: {exc}"
    log.info("Supabase PUT %s (bucket=%s) -> HTTP %s: %s",
             name, bucket, r2.status_code, (r2.text or "")[:600])
    if r2.status_code in (200, 201):
        return True, "ok"
    return False, (f"POST HTTP {r.status_code}: {(r.text or '')[:250]} | "
                   f"PUT HTTP {r2.status_code}: {(r2.text or '')[:250]}")


def _sb_get_object(name: str) -> tuple[int, str | None]:
    """(status_code, body). status_code 0 = not configured, -1 = network error."""
    cfg = _sb_config()
    if not cfg:
        return 0, None
    url, key, bucket = cfg
    try:
        r = requests.get(_sb_object_url(url, bucket, name),
                         headers=_sb_headers(key), timeout=20)
    except Exception as exc:
        log.error("Supabase GET %s raised: %s", name, exc)
        return -1, None
    log.info("Supabase GET %s (bucket=%s) -> HTTP %s", name, bucket, r.status_code)
    return r.status_code, r.text


def _sb_delete_object(name: str) -> tuple[int, str]:
    cfg = _sb_config()
    if not cfg:
        return 0, ""
    url, key, bucket = cfg
    try:
        r = requests.delete(_sb_object_url(url, bucket, name),
                            headers=_sb_headers(key), timeout=20)
    except Exception as exc:
        log.error("Supabase DELETE %s raised: %s", name, exc)
        return -1, str(exc)
    log.info("Supabase DELETE %s (bucket=%s) -> HTTP %s: %s",
             name, bucket, r.status_code, (r.text or "")[:300])
    return r.status_code, r.text or ""


def sb_health_check() -> tuple[bool, str]:
    """Real end-to-end write-test: write a tiny object, read it back, delete it.
    Returns (ok, detail). The sidebar status reflects THIS, not creds alone."""
    cfg = _sb_config()
    if not cfg:
        return False, "not configured"
    test_name = "__write_test__.json"
    token = uuid.uuid4().hex
    payload = json.dumps({"healthcheck": token}).encode("utf-8")

    ok, detail = _sb_put_object(test_name, payload)
    if not ok:
        return False, f"write failed — {detail}"

    code, body = _sb_get_object(test_name)
    if code != 200:
        return False, f"read-back failed — HTTP {code}: {(body or '')[:200]}"
    try:
        got = (json.loads(body) or {}).get("healthcheck")
    except Exception:
        got = None
    if got != token:
        return False, "read-back mismatch — object not persisting as written"

    # Cleanup is best-effort; a delete failure does not fail the verdict.
    _sb_delete_object(test_name)
    return True, "ok"


def get_sb_health(force: bool = False) -> tuple[bool, str]:
    """Cached per session so the round-trip test runs once on startup."""
    ss = st.session_state
    if force or "or_sb_health" not in ss:
        ss["or_sb_health"] = sb_health_check()
    return ss["or_sb_health"]


def _sb_download() -> tuple[bool, list[dict]]:
    """(ok, sessions). ok=False means configured but the fetch failed (so the
    caller should fall back to the local cache rather than assume empty)."""
    if not _sb_config():
        return False, []
    code, body = _sb_get_object(OBJECT_NAME)
    if code == 200:
        try:
            data = json.loads(body) if body else []
        except Exception:
            return True, []
        return True, (data if isinstance(data, list) else [])
    if code in (400, 404):
        return True, []  # object not created yet -> genuinely empty
    return False, []     # auth/other error -> let caller use local cache


def _sb_upload(sessions: list[dict]) -> bool:
    payload = json.dumps(sessions, ensure_ascii=False, indent=2).encode("utf-8")
    ok, detail = _sb_put_object(OBJECT_NAME, payload)
    if not ok:
        log.error("Supabase upload of %s FAILED: %s", OBJECT_NAME, detail)
    return ok


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
        if not _sb_upload(sessions):
            log.error("History NOT synced to Supabase (see HTTP log above); "
                      "kept local only.")


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
        # Sync status reflects a real write/read/delete round-trip, not just
        # whether credentials are present.
        if _sb_config():
            ok, detail = get_sb_health()
            if ok:
                st.caption("☁️ Synced to Supabase (write-test passed)")
            else:
                st.error(f"⚠️ Supabase sync FAILED: {detail}")
                st.caption("Saving local-only until fixed. Check the bucket name, "
                           "that the bucket exists, and that the service_role key "
                           "has Storage write access.")
                if st.button("↻ Recheck Supabase", key="or_sb_recheck",
                             use_container_width=True):
                    get_sb_health(force=True)
                    st.rerun()
        else:
            st.caption("💾 Local only — set SUPABASE_URL/KEY to sync across reboots")
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
            partial = False
            with st.spinner(f"Researching prospects at {company.strip()} via "
                            f"Perplexity ({depth} depth)…"):
                try:
                    candidates, raw_text, partial = discover_people(
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
                if partial:
                    st.warning(
                        "Response was cut short, showing the "
                        f"{len(ss.or_candidates)} candidate(s) that did come "
                        "through. Try a lower research depth for a complete list, "
                        "or scrape these and re-run for more.")
                else:
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
                    # Visibly tag which backend served the career history, and
                    # (when different) which served certs/recommendations.
                    src = s.get("profile_source")
                    extras = s.get("extras_source")
                    if src:
                        tag = f"📇 Career history: via {src}"
                        if extras and extras != src:
                            tag += f"  ·  certs/recommendations via {extras}"
                        st.caption(tag)
                    elif s.get("profile_err"):
                        st.caption("📇 Profile data: unavailable "
                                   "(Bright Data and Apify both failed)")
                    if s.get("profile_err"):
                        st.caption(f"(profile step note: {s['profile_err']})")
                    report = s.get("report_md", "(no content)")
                    # Small, unobtrusive copy button (no raw markdown on the page).
                    render_copy_button(report, key=name)
                    # Clean, formatted report (headers, bold, bullets) — only this.
                    st.markdown(report)
                    # Debug: raw responses, to confirm what each source returned.
                    if s.get("raw_profile") or s.get("raw_profile_apify"):
                        with st.expander("🔧 Raw scrape data (debug)",
                                         expanded=False):
                            if s.get("raw_profile"):
                                st.caption("Bright Data response")
                                st.json(s["raw_profile"])
                            if s.get("raw_profile_apify"):
                                st.caption("Apify profile response (career-history fallback)")
                                st.json(s["raw_profile_apify"])
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
