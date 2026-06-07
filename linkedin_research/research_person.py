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
# Phase 1: resolve
# --------------------------------------------------------------------------- #
def cmd_resolve(args: argparse.Namespace, client: ApifyClient) -> None:
    actor = REGISTRY["resolve"]
    first, last = split_name(args.name)

    run_input: dict = {
        "firstName": first,
        "lastName": last,
        "profileScraperMode": "Short",   # cheapest mode, enough to identify
        "maxPages": 1,
        "maxItems": args.max_candidates,
        "strictSearch": not args.loose,
    }
    if args.location:
        run_input["locations"] = [args.location]
    if args.current_company and args.current_company.startswith("http"):
        run_input["currentCompanies"] = [args.current_company]
    if args.past_company and args.past_company.startswith("http"):
        run_input["pastCompanies"] = [args.past_company]

    print(f"\n== RESOLVE (cheap search) ==")
    print(f"Actor : {actor.human_name}")
    print(f"        {actor.notes}")
    print(f"Query : {first} {last}"
          + (f" | location={args.location}" if args.location else "")
          + (f" | past={args.past_company}" if args.past_company else "")
          + (f" | current={args.current_company}" if args.current_company else ""))
    print(f"Pull  : up to {args.max_candidates} short profiles, 1 page "
          f"(~$0.004 for the page). No activity scraped.\n")

    items = client.run_actor(actor.actor_id, run_input)
    if not items:
        print("No candidates returned. Try --loose, or widen/relax the filters.")
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
    print(f"\n== CONFIRM (1 profile) ==")
    print(f"Actor : {confirm_actor.human_name}")
    print(f"Pull  : 1 profile to lock in current role/headline (~$0.004).\n")
    role = headline = location = name = ""
    try:
        prof = client.run_actor(
            confirm_actor.actor_id,
            {"profileScraperMode": "Profile details no email ($4 per 1k)",
             "queries": [url]},
        )
        if prof:
            p = prof[0]
            name = fmt_field(p, "name", "fullName")
            headline = fmt_field(p, "headline", "occupation", "info")
            company = fmt_field(p, "currentCompany.name", "company")
            location = fmt_field(p, "location.linkedinText", "location",
                                 "locationName")
            role = " | ".join(x for x in (headline, company, location) if x)
            print(f"Confirmed: {name or '(name n/a)'}")
            if role:
                print(f"           {role}")
    except ApifyError as exc:
        _eprint(f"(confirm step skipped: {exc})")
    print()

    # --- scrape posts + reposts ------------------------------------------- #
    posts_actor = REGISTRY["posts"]
    run_input = {
        "targetUrls": [url],
        "maxPosts": args.max_posts,
        "postedLimitDate": since.date().isoformat(),
        "includeReposts": True,
        "includeQuotePosts": True,
        "scrapeComments": False,
        "scrapeReactions": False,
    }
    print(f"== SCRAPE ACTIVITY ==")
    print(f"Actor : {posts_actor.human_name}")
    print(f"        {posts_actor.notes}")
    print(f"Pull  : up to {args.max_posts} posts/reposts since "
          f"{since.date().isoformat()} (~${args.max_posts * 0.002:.2f} worst case).\n")

    items = client.run_actor(posts_actor.actor_id, run_input)

    rows = []
    for it in items:
        dt = parse_item_date(it)
        if dt and dt < since:
            continue
        rows.append((dt, it))
    rows.sort(key=lambda r: (r[0] is None, -(r[0].timestamp() if r[0] else 0)))

    person_label = name or args.name
    report = render_markdown(person_label, role, url, since, rows)
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


def render_markdown(name, role, profile_url, since, rows) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Public activity: {name}",
        "",
        f"- **Profile:** {profile_url}",
        f"- **Current role:** {role or 'n/a'}",
        f"- **Window:** since {since.date().isoformat()} (newest first)",
        f"- **Items found:** {len(rows)}",
        f"- **Generated:** {now}",
        "",
        "> Source: LinkedIn via Apify (harvestapi). Posts and reposts by the "
        "profile. Comments the person left on *other* people's content are not "
        "reliably exposed and are not included.",
        "",
        "---",
        "",
    ]
    if not rows:
        lines.append("_No posts or reposts found in this window._")
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
    p.add_argument("--days", type=int, default=60,
                   help="Lookback window if --since omitted (default 60).")
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
