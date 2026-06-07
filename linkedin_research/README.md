# People Research CLI (LinkedIn-first, Apify-backed)

Researches a person's recent public professional activity. LinkedIn is the
primary source, via the Apify API. Credit-safe: it resolves and confirms the
right profile **before** spending credits on activity.

## Setup

```bash
pip install -r requirements.txt   # stdlib only; nothing to install really
export APIFY_TOKEN="apify_api_..."        # bash
$env:APIFY_TOKEN = "apify_api_..."        # PowerShell
```

The token is read only from `APIFY_TOKEN`. It is never hardcoded.

## Workflow

### Phase 1 — resolve (cheap, no activity scraped)

```bash
python research_person.py "Karina Mazur" \
    --location "United Kingdom" \
    --past-company Migreats \
    --current-company Borderless
```

Prints candidate profiles (name, headline, company, location, URL). Pick the
right one.

### Phase 2 — scrape (after you confirm)

```bash
python research_person.py "Karina Mazur" \
    --confirm https://www.linkedin.com/in/<handle>/ \
    --since 2026-04-01
```

Confirms the current role, pulls posts + reposts since the window, prints them
newest-first, and writes `karina-mazur.md`.

## Streamlit chat UI (`chat_researcher.py`)

A WhatsApp-style chat front-end over the same pipeline.

```bash
pip install -r requirements.txt
export APIFY_TOKEN="apify_api_..."          # required
export ANTHROPIC_API_KEY="sk-ant-..."       # optional: enables extraction + Q&A
streamlit run chat_researcher.py
```

**Phase 1 — research.** Type naturally, e.g. `Lauren Peate, Multitudes, Auckland
New Zealand`. The app extracts name/company/location (Claude API if available,
else comma parsing), resolves candidate profiles, and shows them. Reply `yes` or
a number to confirm, then it scrapes posts + reposts (newest first) and writes
`<name>.md`.

**Phase 2 — Q&A.** After scraping, ask questions about the activity
("what has she been posting about", "summarise her activity", "any career
themes"). Answered by Claude (`claude-sonnet-4-20250514`) using **only** the
scraped markdown as context — chat history is not resent, keeping tokens lean.

Q&A requires `ANTHROPIC_API_KEY`; without it, extraction falls back to comma
parsing and Q&A is disabled. `APIFY_TOKEN` is read from the environment as in
the CLI.

## Actors (auto-selected per job, verified live)

| Job | Actor | Why |
|-----|-------|-----|
| resolve | `harvestapi/linkedin-profile-search-by-name` | cheap search, returns headline/company/location/URL |
| confirm | `harvestapi/linkedin-profile-scraper` | locks in current role |
| posts | `harvestapi/linkedin-profile-posts` | posts + reposts + engagement |
| web (fallback) | `apify/google-search-scraper` | other public activity |

Swap any actor in one place: `actors.py` -> `REGISTRY`.

## Options

- `--since YYYY-MM-DD` start date (default: `--days 60` back)
- `--max-posts N` cap activity pulled (default 40)
- `--max-candidates N` cap resolve results (default 5)
- `--loose` relax strict name matching

## Note on comments

The posts actor returns **posts and reposts** by the profile. Comments the
person left on *other* people's content are not reliably exposed by LinkedIn
to logged-out scrapers, so they are not included.
