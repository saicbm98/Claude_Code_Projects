# LinkedIn Activity Tracker

Personal tool that tracks LinkedIn posts from a small list of people. Fully separate
from the Outreach researcher in `linkedin_research/`: its own Supabase project, its own
secrets, and its own `TRACKER_`-prefixed variables.

- **Scraper:** an n8n workflow fills `posts` daily (lives in n8n, not in this repo).
- **Website:** `app.py`, a password-protected Streamlit app titled "Reading list".

## Backend (Supabase project: `linkedin-activity-tracker`)

- `people`: who to track. Set `active = false` to pause someone; don't delete them.
- `posts`: one row per scraped post, deduped on `post_url`. `person_id` uses
  `ON DELETE RESTRICT`, so a person with posts can't be deleted by accident.
  `post_type` is free text (post, repost, quote repost, article, ...). Reposts also
  carry `original_author`, `original_text` and `original_url`.

Fresh setup: run `schema.sql` in the project's SQL Editor.

A database built from an older `schema.sql` also needs the migrations, in order:

1. `migrations/001_restrict_fk_and_free_post_type.sql` (cascade to restrict, drop `post_type` check)
2. `migrations/002_add_repost_columns.sql` (the three `original_*` columns)

## Website

Reads posts and people over the Supabase REST API with plain `requests`. It only
writes to `people` (add a person, pause/resume). It never deletes anyone and never
writes to `posts`.

- Password screen first: nothing is loaded or shown until `APP_PASSWORD` is entered.
- Feed newest first, 25 at a time with "Show more"; "New" badge for the last 3 days.
- Reposts show "Reposted from ...", the person's own comment, and the original in a quote block.
- Sidebar: filter by person (tick "Include paused people" to see paused people's old
  posts), search, date range, Refresh, "Add person" and pause/resume toggles.
- Summary: total posts, posts in the last 7 days, and how long ago the last scrape was.
- Times are shown in NZ time. Data is cached for 5 minutes; Refresh reloads it.

### Secrets

All config comes from Streamlit secrets. See `.streamlit/secrets.toml.example`:

| Key | What |
|---|---|
| `TRACKER_SUPABASE_URL` | `https://<project-ref>.supabase.co` |
| `TRACKER_SUPABASE_KEY` | The project's `sb_secret_...` key (sent only in the `apikey` header) |
| `APP_PASSWORD` | Password for the unlock screen |

`linkedin-activity-tracker/.streamlit/secrets.toml` is git-ignored. Never commit real values.

### Run locally

```
cd linkedin-activity-tracker
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill in real values
streamlit run app.py
```

### Deploy on Streamlit Community Cloud

1. Push this branch (or merge it into `main`).
2. Go to https://share.streamlit.io, sign in with GitHub, and click **Create app**.
3. Choose **Deploy a public app from GitHub** and fill in:
   - Repository: `saicbm98/Claude_Code_Projects`
   - Branch: `main` (or the branch you want to run)
   - Main file path: `linkedin-activity-tracker/app.py`
   - App URL: pick something neutral (no names, no "linkedin").
4. Open **Advanced settings**:
   - Python version: 3.12 or newer.
   - Secrets: paste the three keys from `.streamlit/secrets.toml.example` with real values.
5. Click **Deploy**. Cloud installs `linkedin-activity-tracker/requirements.txt`
   (the file next to `app.py` takes priority over the repo root's).
6. Optional: in the app's **Settings > Sharing**, make it private to your own account
   for a second lock on top of the password.

To change secrets later: app menu > **Settings > Secrets**, save, and the app restarts.
