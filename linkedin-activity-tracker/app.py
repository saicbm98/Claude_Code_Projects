"""Reading list: a private feed of posts from the people I track.

Reads the tracker's Supabase project over plain REST. Reads posts, writes only to people
(add, pause/resume). Nothing is fetched or shown until the password screen is passed.

Run locally from this folder:  streamlit run app.py
"""

import hmac
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import streamlit as st

st.set_page_config(page_title="Reading list", page_icon="📚")

TZ = ZoneInfo("Pacific/Auckland")
PAGE_SIZE = 25
PREVIEW_CHARS = 500
NEW_DAYS = 3
FETCH_BATCH = 1000  # Supabase's default max rows per request
PROFILE_PREFIX = "https://www.linkedin.com/in/"
TIMEOUT = 30


# --- Secrets & password gate ------------------------------------------------ #

def _secret(name: str) -> str:
    try:
        return str(st.secrets[name])
    except Exception:  # no secrets file, or key missing
        return ""


def password_gate() -> None:
    """Render only the unlock form until the right password is entered."""
    if st.session_state.get("unlocked"):
        return
    st.title("Reading list")
    expected = _secret("APP_PASSWORD")
    if not expected:
        st.error("APP_PASSWORD is not set in the app's secrets.")
        st.stop()
    with st.form("unlock"):
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Unlock")
    if submitted:
        if hmac.compare_digest(entered.encode(), expected.encode()):
            st.session_state["unlocked"] = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()


# --- Supabase REST ---------------------------------------------------------- #
# The key is a new-format sb_secret_ key, not a JWT: send it in `apikey` only.

def _rest() -> tuple[str, dict]:
    url = _secret("TRACKER_SUPABASE_URL").strip().rstrip("/")
    key = _secret("TRACKER_SUPABASE_KEY").strip()
    if not url or not key:
        raise RuntimeError("TRACKER_SUPABASE_URL / TRACKER_SUPABASE_KEY are not set in secrets.")
    return f"{url}/rest/v1", {"apikey": key}


def _get(table: str, params: dict) -> list[dict]:
    base, headers = _rest()
    r = requests.get(f"{base}/{table}", params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=300, show_spinner="Loading posts…")
def fetch_posts() -> list[dict]:
    rows, offset = [], 0
    while True:
        batch = _get("posts", {
            "select": "*,people(name,linkedin_url)",
            "order": "posted_at.desc.nullslast,id.desc",
            "limit": FETCH_BATCH,
            "offset": offset,
        })
        rows.extend(batch)
        if len(batch) < FETCH_BATCH:
            return rows
        offset += FETCH_BATCH


@st.cache_data(ttl=300, show_spinner=False)
def fetch_people() -> list[dict]:
    return _get("people", {"select": "id,name,linkedin_url,active", "order": "name.asc"})


def insert_person(name: str, linkedin_url: str) -> bool:
    """Insert a person. Returns False if the URL is already tracked."""
    base, headers = _rest()
    r = requests.post(
        f"{base}/people",
        json={"name": name, "linkedin_url": linkedin_url},
        headers={**headers, "Prefer": "return=minimal"},
        timeout=TIMEOUT,
    )
    if r.status_code == 409 or "23505" in r.text:  # unique_violation
        return False
    r.raise_for_status()
    return True


def update_active(person_id: int, active: bool) -> None:
    base, headers = _rest()
    r = requests.patch(
        f"{base}/people",
        params={"id": f"eq.{person_id}"},
        json={"active": active},
        headers={**headers, "Prefer": "return=minimal"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()


def clear_caches() -> None:
    fetch_posts.clear()
    fetch_people.clear()
    for key in [k for k in st.session_state if k.startswith("active_")]:
        del st.session_state[key]


# --- Helpers ----------------------------------------------------------------- #

def normalise_profile_url(raw: str) -> str | None:
    """https://www.linkedin.com/in/<slug>, minus tracking params, or None if invalid."""
    url = re.split(r"[?#]", raw.strip(), maxsplit=1)[0]
    if not url.startswith(PROFILE_PREFIX):
        return None
    slug = url[len(PROFILE_PREFIX):].split("/")[0].strip().lower()
    return PROFILE_PREFIX + slug if slug else None


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}{'' if n == 1 else 's'} ago"


def relative(dt: datetime, now: datetime) -> str:
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return _plural(int(secs // 60), "minute")
    if secs < 86400:
        return _plural(int(secs // 3600), "hour")
    days = int(secs // 86400)
    if days == 1:
        return "yesterday"
    if days < 7:
        return _plural(days, "day")
    if days < 30:
        return _plural(days // 7, "week")
    if days < 365:
        return _plural(days // 30, "month")
    return _plural(days // 365, "year")


def nz_date(dt: datetime) -> date:
    return dt.astimezone(TZ).date()


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$:])")


def md_text(text: str) -> str:
    """Scraped text shown literally: escape Markdown, keep line breaks."""
    return _MD_SPECIAL.sub(r"\\\1", text).replace("\n", "  \n")


def md_quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in md_text(text).split("\n"))


def split_preview(text: str) -> tuple[str, str]:
    if len(text) <= PREVIEW_CHARS:
        return text, ""
    cut = text.rfind(" ", 0, PREVIEW_CHARS)
    if cut < PREVIEW_CHARS * 0.8:
        cut = PREVIEW_CHARS
    return text[:cut].rstrip(), text[cut:].lstrip()


def safe_link(url: str | None) -> str | None:
    return url if url and url.startswith(("https://", "http://")) else None


def show_text(text: str, quote: bool = False) -> None:
    fmt = md_quote if quote else md_text
    preview, rest = split_preview(text)
    st.markdown(fmt(preview + (" …" if rest else "")))
    if rest:
        with st.expander("Read more"):
            st.markdown(fmt(rest))


def _count(value) -> str:
    return f"{value:,}" if isinstance(value, int) else "–"


# --- UI pieces ---------------------------------------------------------------- #

def render_card(post: dict, names: dict, now: datetime) -> None:
    person = post.get("people") or {}
    name = person.get("name") or names.get(post.get("person_id"), "Unknown")
    posted = post["_posted"]
    ptype = (post.get("post_type") or "post").strip()
    original_text = (post.get("original_text") or "").strip()
    original_author = (post.get("original_author") or "").strip()
    original_url = safe_link(post.get("original_url"))
    is_repost = ptype.lower() == "repost" or bool(original_text or original_author or original_url)
    own_text = (post.get("text") or "").strip()

    with st.container(border=True):
        badge = " :green-badge[New]" if posted and now - posted <= timedelta(days=NEW_DAYS) else ""
        st.markdown(f"**{md_text(name)}**{badge}")
        local = posted.astimezone(TZ) if posted else None
        when = (f"{local.day} {local:%b %Y} · {relative(posted, now)}"
                if local else "Date unknown")
        st.caption(f"{when} · {md_text(ptype)}")

        if is_repost:
            label = f"Reposted from {original_author}" if original_author else "Reposted"
            st.markdown(f"🔁 *{md_text(label)}*")
            if own_text:
                show_text(own_text)
            if original_text:
                show_text(original_text, quote=True)
            if original_url:
                st.markdown(f"> [Open original ↗]({original_url})")
        elif own_text:
            show_text(own_text)

        st.caption(f"👍 {_count(post.get('reactions'))} · 💬 {_count(post.get('comments'))}"
                   f" · 🔁 {_count(post.get('reposts'))}")
        link = safe_link(post.get("post_url"))
        if link:
            st.link_button("Open post ↗", link)


def _add_person_cb() -> None:
    name = st.session_state.get("new_name", "").strip()
    url = normalise_profile_url(st.session_state.get("new_url", ""))
    if not name:
        st.session_state["flash"] = ("warning", "Please enter a name.")
        return
    if not url:
        st.session_state["flash"] = ("warning", f"The profile URL must start with {PROFILE_PREFIX}")
        return
    try:
        known = {p["linkedin_url"].lower(): p["name"] for p in fetch_people()}
        if url in known:
            st.session_state["flash"] = ("info", f"Already tracking that profile ({known[url]}).")
            return
        if not insert_person(name, url):
            st.session_state["flash"] = ("info", "That profile is already being tracked.")
            return
    except (requests.RequestException, RuntimeError) as exc:
        st.session_state["flash"] = ("error", f"Couldn't add {name}: {exc}")
        return
    clear_caches()
    st.session_state["new_name"] = ""
    st.session_state["new_url"] = ""
    st.session_state["flash"] = ("success", f"Added {name}. Their posts will appear after the next run.")


def _toggle_cb(person_id: int, name: str) -> None:
    key = f"active_{person_id}"
    active = st.session_state[key]
    try:
        update_active(person_id, active)
    except (requests.RequestException, RuntimeError) as exc:
        st.session_state[key] = not active
        st.session_state["flash"] = ("error", f"Couldn't update {name}: {exc}")
        return
    fetch_people.clear()
    st.session_state["flash"] = ("success", f"{'Resumed' if active else 'Paused'} {name}.")


def _show_more_cb() -> None:
    st.session_state["shown"] = st.session_state.get("shown", PAGE_SIZE) + PAGE_SIZE


# --- App ----------------------------------------------------------------------- #

def main() -> None:
    password_gate()

    try:
        people = fetch_people()
        posts = fetch_posts()
    except (requests.RequestException, RuntimeError) as exc:
        st.title("Reading list")
        st.error(f"Couldn't load data: {exc}")
        if st.button("Try again"):
            clear_caches()
            st.rerun()
        st.stop()

    now = datetime.now(timezone.utc)
    names = {p["id"]: p["name"] for p in people}
    for post in posts:
        post["_posted"] = parse_ts(post.get("posted_at"))

    # Sidebar: filters
    sb = st.sidebar
    sb.button("↻ Refresh", on_click=clear_caches, width="stretch")
    include_paused = sb.checkbox("Include paused people", value=False)
    choices = [p for p in people if p["active"] or include_paused]
    selected = sb.multiselect(
        "People",
        options=[p["id"] for p in choices],
        default=[p["id"] for p in choices],
        format_func=lambda pid: names.get(pid, "?") + ("" if any(
            p["id"] == pid and p["active"] for p in people) else " (paused)"),
    )
    query = sb.text_input("Search").strip().lower()

    dated = [nz_date(p["_posted"]) for p in posts if p["_posted"]]
    start = end = None
    full_range = True
    if dated:
        lo, hi = min(dated), max(dated)
        picked = sb.date_input("Date range", value=(lo, hi), min_value=lo, max_value=hi,
                               format="DD/MM/YYYY")
        if isinstance(picked, (tuple, list)):
            start = picked[0] if len(picked) > 0 else None
            end = picked[1] if len(picked) > 1 else None
        full_range = start in (None, lo) and end in (None, hi)

    # Sidebar: people management
    sb.divider()
    flash = st.session_state.pop("flash", None)
    if flash:
        getattr(sb, flash[0])(flash[1])
    with sb.form("add_person"):
        st.markdown("**Add person**")
        st.text_input("Name", key="new_name")
        st.text_input("Profile URL", key="new_url", placeholder=PROFILE_PREFIX + "…")
        st.form_submit_button("Add", on_click=_add_person_cb)
    with sb.expander("Tracked people"):
        for p in people:
            key = f"active_{p['id']}"
            if key not in st.session_state:
                st.session_state[key] = bool(p["active"])
            st.toggle(p["name"], key=key, on_change=_toggle_cb, args=(p["id"], p["name"]))
        st.caption("Off = paused. Nobody is ever deleted.")

    # Summary (all data, ignores filters)
    st.title("Reading list")
    scraped = [t for t in (parse_ts(p.get("scraped_at")) for p in posts) if t]
    week = sum(1 for p in posts if p["_posted"] and now - p["_posted"] <= timedelta(days=7))
    c1, c2, c3 = st.columns(3)
    c1.metric("Total posts", f"{len(posts):,}")
    c2.metric("Last 7 days", f"{week:,}")
    c3.metric("Latest scrape", relative(max(scraped), now) if scraped else "–")

    # Filter
    chosen = set(selected)
    visible = []
    for p in posts:
        if p.get("person_id") not in chosen:
            continue
        if p["_posted"]:
            d = nz_date(p["_posted"])
            if (start and d < start) or (end and d > end):
                continue
        elif not full_range:
            continue
        if query:
            person = p.get("people") or {}
            haystack = " ".join(str(x or "") for x in (
                p.get("text"), p.get("original_text"), p.get("original_author"),
                person.get("name"), names.get(p.get("person_id")))).lower()
            if query not in haystack:
                continue
        visible.append(p)

    signature = (tuple(sorted(chosen)), query, start, end, include_paused)
    if st.session_state.get("filter_sig") != signature:
        st.session_state["filter_sig"] = signature
        st.session_state["shown"] = PAGE_SIZE
    shown = st.session_state.get("shown", PAGE_SIZE)

    if not chosen:
        st.info("Pick at least one person in the sidebar.")
        return
    if not visible:
        st.info("No posts match these filters.")
        return

    st.caption(f"Showing {min(shown, len(visible))} of {len(visible)} posts")
    for post in visible[:shown]:
        render_card(post, names, now)
    if shown < len(visible):
        st.button("Show more", on_click=_show_more_cb, width="stretch")


main()
