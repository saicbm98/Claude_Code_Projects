#!/usr/bin/env python3
"""Research a person's recent public professional activity (LinkedIn-first).

Two-phase, credit-safe workflow:

  Phase 1 - RESOLVE (cheap):
    python research_person.py "Karina Mazur" \
        --location "United Kingdom" --past-company Migreats --current-company Borderless

    Runs a cheap search actor, prints candidate profiles (name, headline,
    company, location, URL) and STOPS. No activity is scraped yet.

  Phase 2 - SCRAPE (after you confirm the right person):
    python research_person.py "Karina Mazur" \
        --confirm https://www.linkedin.com/in/karinamazur/ --since 2026-04-01

    Confirms the profile's current role, then pulls posts + reposts since the
    window, prints them newest-first, and writes a markdown report.

The Apify token is read from the APIFY_TOKEN environment variable.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from actors import REGISTRY, ApifyClient, ApifyError


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def get_token() -> str:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        _eprint(
            "ERROR: APIFY_TOKEN is not set.\n"
            "  Set it before running, e.g.\n"
            "    PowerShell:  $env:APIFY_TOKEN = 'apify_api_...'\n"
            "    bash:        export APIFY_TOKEN='apify_api_...'\n"
        )
        sys.exit(2)
    return token


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    if len(parts) < 2:
        return full_name, ""
    return parts[0], " ".join(parts[1:])


def parse_since(value: str | None, days: int) -> datetime:
    if value:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            _eprint(f"ERROR: --since '{value}' is not YYYY-MM-DD.")
            sys.exit(2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return datetime.now(timezone.utc) - timedelta(days=days)


def slugify(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name).strip().lower()
    return re.sub(r"[\s_-]+", "-", slug) or "person"


def titlecase(text: str) -> str:
    """Normalise a free-text field to title case for the search query.

    Capitalises the first letter of each word but leaves words that already
    contain an uppercase letter alone, so acronyms and McNames survive:
    'maj hallwass' -> 'Maj Hallwass', 'borderless' -> 'Borderless',
    'auckland new zealand' -> 'Auckland New Zealand', 'NZ'/'McKinsey' kept.
    """
    if not text:
        return text
    words = []
    for w in text.split():
        words.append(w if any(c.isupper() for c in w) else w[:1].upper() + w[1:])
    return " ".join(words)


def _resolve_input(first, last, max_candidates, strict, *,
                   location=None, current_company=None, past_company=None) -> dict:
    """Build a harvestapi search-by-name input. Company filters are only sent
    when given as a LinkedIn company URL (the actor's only company filter)."""
    ri: dict = {
        "firstName": first,
        "lastName": last,
        "profileScraperMode": "Short",   # cheapest mode, enough to identify
        "maxPages": 1,
        "maxItems": max_candidates,
        "strictSearch": strict,
    }
    if location:
        ri["locations"] = [location]
    if current_company and current_company.startswith("http"):
        ri["currentCompanies"] = [current_company]
    if past_company and past_company.startswith("http"):
        ri["pastCompanies"] = [past_company]
    return ri


def fmt_field(item: dict, *keys: str) -> str:
    """Return the first non-empty value among keys (supports a.b dot paths)."""
    for key in keys:
        cur: object = item
        for part in key.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
                break
        if cur:
            return str(cur).strip()
    return ""


def parse_item_date(item: dict) -> datetime | None:
    raw = fmt_field(
        item, "postedAt.date", "postedAtISO", "postedDate", "date",
        "publishedAt", "postedAt.timestamp", "time",
    )
    if not raw:
        return None
    # Numeric epoch (seconds or ms)?
    if re.fullmatch(r"\d{10,13}", raw):
        ts = int(raw)
        if ts > 10_000_000_000:  # ms
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    txt = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        try:
            dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# Posts scraping with tiered time-window fallback
# --------------------------------------------------------------------------- #
POST_WINDOWS = [180, 365, 730]   # default starts at 6 months, then widens
WINDOW_LABEL = {60: "last 2 months", 180: "last 6 months",
                365: "last 1 year", 730: "last 2 years"}
# Messages shown when a window returns nothing and we widen to the next.
EXPAND_MSG = [
    "No activity in the last 6 months. Expanding search to 1 year...",
    "Nothing in 1 year. Trying 2 years...",
]
NO_ACTIVITY_MSG = ("No posts or reposts found in the last 2 years. Profile and "
                   "career history have been captured above.")


def scrape_posts(client: ApifyClient, url: str, since, max_posts: int) -> list[tuple]:
    """Scrape posts/reposts since `since`, filtered + sorted newest-first."""
    actor = REGISTRY["posts"]
    items = client.run_actor(actor.actor_id, {
        "targetUrls": [url],
        "maxPosts": max_posts,
        "postedLimitDate": since.date().isoformat(),
        "includeReposts": True,
        "includeQuotePosts": True,
        "scrapeComments": False,
        "scrapeReactions": False,
    })
    rows = []
    for it in items:
        dt = parse_item_date(it)
        if dt and dt < since:
            continue
        rows.append((dt, it))
    rows.sort(key=lambda r: (r[0] is None, -(r[0].timestamp() if r[0] else 0)))
    return rows


def scrape_activity_tiered(client: ApifyClient, url: str, max_posts: int,
                           on_message=None, windows=POST_WINDOWS):
    """Scrape posts, widening the window (60d -> 180d -> 365d -> 730d) until
    results appear. Announces each expansion via on_message(text).
    Returns (rows, used_days_or_None, used_since)."""
    since = None
    for i, days in enumerate(windows):
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = scrape_posts(client, url, since, max_posts)
        if rows:
            return rows, days, since
        if on_message and i < len(windows) - 1:
            on_message(EXPAND_MSG[i])
    return [], None, since


def activity_note(used_days: int | None) -> str:
    """One-line status of which window returned results (for report + chat)."""
    if used_days is None:
        return NO_ACTIVITY_MSG
    return f"Activity found: showing posts from the {WINDOW_LABEL[used_days]}."


# --------------------------------------------------------------------------- #
# Phase 1: resolve
# --------------------------------------------------------------------------- #
def cmd_resolve(args: argparse.Namespace, client: ApifyClient) -> None:
    actor = REGISTRY["resolve"]

    # FIX 1: normalise name/location/company to title case before the API call.
    name = titlecase(args.name)
    location = titlecase(args.location) if args.location else None
    current = (args.current_company if (args.current_company or "").startswith("http")
               else titlecase(args.current_company) if args.current_company else None)
    past = (args.past_company if (args.past_company or "").startswith("http")
            else titlecase(args.past_company) if args.past_company else None)
    first, last = split_name(name)
    strict = not args.loose
    mc = args.max_candidates

    # FIX 2: progressive fallback ladder (most specific -> least). Duplicate
    # input sets are collapsed so we never fire the same query twice.
    ladder = [
        ("name + location + company",
         _resolve_input(first, last, mc, strict, location=location,
                        current_company=current, past_company=past)),
        ("name + location (dropped company)",
         _resolve_input(first, last, mc, strict, location=location)),
        ("name only (dropped location)",
         _resolve_input(first, last, mc, strict)),
    ]
    attempts: list[tuple[str, dict]] = []
    for label, ri in ladder:
        if all(ri != prev for _, prev in attempts):
            attempts.append((label, ri))

    print(f"\n== RESOLVE (cheap search) ==")
    print(f"Actor : {actor.human_name}")
    print(f"        {actor.notes}")
    print(f"Query : {first} {last}"
          + (f" | location={location}" if location else "")
          + (f" | past={past}" if past else "")
          + (f" | current={current}" if current else ""))
    print(f"Pull  : up to {mc} short profiles, 1 page "
          f"(~$0.004 for the page). No activity scraped.\n")

    items: list[dict] = []
    for i, (label, run_input) in enumerate(attempts):
        if i > 0:
            print(f"No candidates — retrying with {label}…")
        items = client.run_actor(actor.actor_id, run_input)
        if items:
            if i > 0:
                print(f"(matched on fallback: {label})")
            break

    if not items:
        print("No candidates returned after all fallbacks. Try --loose.")
        return

    print(f"Found {len(items)} candidate(s):\n")
    for i, it in enumerate(items, 1):
        name = fmt_field(it, "name", "fullName", "firstName")
        headline = fmt_field(it, "headline", "occupation", "subtitle")
        company = fmt_field(it, "currentCompany.name", "company", "companyName")
        location = fmt_field(it, "location", "locationName", "geo")
        url = fmt_field(it, "linkedinUrl", "url", "profileUrl", "publicProfileUrl")
        print(f"[{i}] {name or '(name n/a)'}")
        if headline:
            print(f"    Headline : {headline}")
        if company:
            print(f"    Company  : {company}")
        if location:
            print(f"    Location : {location}")
        print(f"    URL      : {url or '(url n/a)'}\n")

    print("Next: pick the right person and re-run to scrape their activity:")
    print(f'  python research_person.py "{args.name}" '
          f'--confirm <profile-url> --since {args.since or "YYYY-MM-DD"}')


# --------------------------------------------------------------------------- #
# Phase 2: confirm + scrape
# --------------------------------------------------------------------------- #
def cmd_scrape(args: argparse.Namespace, client: ApifyClient) -> None:
    since = parse_since(args.since, args.days)
    url = args.confirm.rstrip("/")

    # --- confirm identity / current role (cheap, 1 profile) ---------------- #
    confirm_actor = REGISTRY["confirm"]
    print(f"\n== PROFILE (1 profile: identity + full career history) ==")
    print(f"Actor : {confirm_actor.human_name}")
    print(f"Pull  : 1 full profile — role, career history, about (~$0.004).\n")
    role = name = ""
    profile: dict | None = None
    try:
        prof = client.run_actor(
            confirm_actor.actor_id,
            {"profileScraperMode": "Profile details no email ($4 per 1k)",
             "queries": [url]},
        )
        if prof:
            profile = prof[0]
            name = (fmt_field(profile, "name", "fullName")
                    or " ".join(x for x in (fmt_field(profile, "firstName"),
                                            fmt_field(profile, "lastName")) if x))
            headline = fmt_field(profile, "headline", "occupation", "info")
            location = fmt_field(profile, "location.linkedinText",
                                 "location.parsed.text", "location")
            role = " | ".join(x for x in (headline, location) if x)
            print(f"Confirmed: {name or '(name n/a)'}")
            if role:
                print(f"           {role}")
    except ApifyError as exc:
        _eprint(f"(profile step skipped: {exc})")
    print()

    # --- scrape posts + reposts (tiered time-window fallback) -------------- #
    posts_actor = REGISTRY["posts"]
    print(f"== SCRAPE ACTIVITY ==")
    print(f"Actor : {posts_actor.human_name}")
    print(f"        {posts_actor.notes}")

    if args.since or args.days != 180:
        # Explicit window requested -> single attempt, no fallback ladder.
        since = parse_since(args.since, args.days)
        print(f"Pull  : posts/reposts since {since.date().isoformat()}.\n")
        rows = scrape_posts(client, url, since, args.max_posts)
        note = (f"Activity window: posts since {since.date().isoformat()}."
                if rows else
                f"No posts or reposts found since {since.date().isoformat()}.")
    else:
        # Default flow: 6 months, expanding to 1 year, then 2 years.
        print(f"Pull  : posts/reposts, starting at 6 months and widening "
              f"if empty.\n")
        rows, used_days, since = scrape_activity_tiered(
            client, url, args.max_posts, on_message=print)
        note = activity_note(used_days)

    print(note + "\n")

    person_label = name or titlecase(args.name)
    report = render_markdown(person_label, role, url, since, rows,
                             profile=profile, window_note=note)
    out_path = os.path.join(
        os.getcwd(), f"{slugify(person_label)}.md"
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)

    print_terminal(person_label, since, rows)
    print(f"\nSaved markdown report -> {out_path}")


def _engagement(it: dict) -> str:
    reactions = fmt_field(it, "engagement.likes", "engagement.reactions",
                          "numLikes", "likes", "reactionsCount", "likesCount")
    comments = fmt_field(it, "engagement.comments", "numComments",
                         "commentsCount")
    reposts = fmt_field(it, "engagement.shares", "numShares", "repostsCount",
                       "sharesCount")
    parts = []
    if reactions:
        parts.append(f"{reactions} reactions")
    if comments:
        parts.append(f"{comments} comments")
    if reposts:
        parts.append(f"{reposts} reposts")
    return ", ".join(parts) or "n/a"


def _item_type(it: dict) -> str:
    # harvestapi marks a reshare with a `repost` object / `repostId`.
    has_repost = bool(it.get("repost") or it.get("repostId")
                      or fmt_field(it, "repostedContent", "resharedPost"))
    own_text = bool(_text(it) and _text(it) != "(no text)")
    if has_repost:
        return "repost (quote)" if own_text else "repost"
    t = fmt_field(it, "type", "postType").lower()
    if "comment" in t:
        return "comment"
    return "post"


def _text(it: dict) -> str:
    return fmt_field(it, "content", "text", "postText", "commentary",
                     "description") or "(no text)"


def _url(it: dict) -> str:
    return fmt_field(it, "url", "postUrl", "linkedinUrl", "link")


def _exp_dates(item: dict) -> str:
    """Human date range for one experience entry, e.g. 'Jan 2026 – Present · 6 mos'."""
    start = fmt_field(item, "startDate.text")
    end = fmt_field(item, "endDate.text")
    duration = fmt_field(item, "duration")
    rng = ""
    if start or end:
        rng = f"{start or '?'} – {end or '?'}"
    parts = [p for p in (rng, duration) if p]
    return " · ".join(parts)


def render_profile_section(profile: dict | None) -> list[str]:
    """Markdown lines for current role + full career history + about/bio."""
    if not profile:
        return []
    experience = profile.get("experience") or []
    current = profile.get("currentPosition") or []
    about = fmt_field(profile, "about", "summary")

    lines = ["## Profile & career history", ""]

    cur = current[0] if current else (experience[0] if experience else None)
    if cur:
        title = fmt_field(cur, "position", "title")
        company = fmt_field(cur, "companyName", "company")
        cur_line = " at ".join(x for x in (title, company) if x)
        if cur_line:
            lines += [f"**Current role:** {cur_line}", ""]

    if about:
        lines += ["**About:**", "", about.strip(), ""]

    if experience:
        lines += ["**Career history:**", ""]
        for e in experience:
            title = fmt_field(e, "position", "title") or "(role n/a)"
            company = fmt_field(e, "companyName", "company")
            head = title + (f" — {company}" if company else "")
            lines.append(f"### {head}")
            meta = " · ".join(x for x in (
                _exp_dates(e),
                fmt_field(e, "employmentType"),
                fmt_field(e, "location"),
            ) if x)
            if meta:
                lines += ["", f"_{meta}_"]
            desc = fmt_field(e, "description")
            if desc:
                lines += ["", desc.strip()]
            lines.append("")

    lines += ["---", ""]
    return lines


def render_markdown(name, role, profile_url, since, rows, profile=None,
                    window_note=None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Public activity: {name}",
        "",
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
        "posts and reposts by the profile. Comments the person left on *other* "
        "people's content are not reliably exposed and are not included.",
        "",
        "---",
        "",
    ]

    # Profile + full career history, before the activity section.
    lines += render_profile_section(profile)

    lines += ["## Recent activity", ""]
    if not rows:
        lines.append(f"_{window_note or 'No posts or reposts found.'}_")
        return "\n".join(lines)

    for dt, it in rows:
        date_str = dt.date().isoformat() if dt else "date n/a"
        lines += [
            f"## {date_str} - {_item_type(it)}",
            "",
            _text(it).strip(),
            "",
            f"- **Engagement:** {_engagement(it)}",
            f"- **URL:** {_url(it) or 'n/a'}",
            "",
            "---",
            "",
        ]
    return "\n".join(lines)


def print_terminal(name, since, rows) -> None:
    print(f"\n========== {name} ==========")
    print(f"Window: since {since.date().isoformat()}  |  {len(rows)} item(s), newest first\n")
    for dt, it in rows:
        date_str = dt.date().isoformat() if dt else "date n/a"
        text = _text(it).strip().replace("\n", " ")
        if len(text) > 280:
            text = text[:277] + "..."
        print(f"[{date_str}] ({_item_type(it)})")
        print(f"  {text}")
        print(f"  engagement: {_engagement(it)}")
        print(f"  url: {_url(it) or 'n/a'}\n")

    counts: dict[str, int] = {}
    for _, it in rows:
        counts[_item_type(it)] = counts.get(_item_type(it), 0) + 1
    summary = ", ".join(f"{v} {k}{'s' if v != 1 else ''}" for k, v in counts.items())
    print(f"SUMMARY: {len(rows)} item(s)" + (f" ({summary})" if summary else ""))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Research a person's recent public LinkedIn activity via Apify.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("name", help="Full name, e.g. \"Karina Mazur\".")
    p.add_argument("--location", help='LinkedIn location, e.g. "United Kingdom".')
    p.add_argument("--current-company", help="Current company (name or LinkedIn URL).")
    p.add_argument("--past-company", help="Former company (name or LinkedIn URL).")
    p.add_argument("--confirm", metavar="PROFILE_URL",
                   help="Confirmed LinkedIn profile URL -> scrape activity (Phase 2).")
    p.add_argument("--since", help="Start date YYYY-MM-DD (default: --days back).")
    p.add_argument("--days", type=int, default=180,
                   help="Lookback window if --since omitted (default 180 = 6 months).")
    p.add_argument("--max-posts", type=int, default=40,
                   help="Cap on posts/reposts to pull (default 40).")
    p.add_argument("--max-candidates", type=int, default=5,
                   help="Cap on resolve candidates (default 5).")
    p.add_argument("--loose", action="store_true",
                   help="Relax strict first+last name matching in resolve.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = get_token()
    client = ApifyClient(token=token)
    try:
        if args.confirm:
            cmd_scrape(args, client)
        else:
            cmd_resolve(args, client)
    except ApifyError as exc:
        _eprint(f"\nApify error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
