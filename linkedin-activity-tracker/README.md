# LinkedIn Activity Tracker

Personal tool that tracks LinkedIn posts from a small list of people. Fully separate
from the Outreach researcher in `linkedin_research/`: its own Supabase project, its own
`.env`, and its own `TRACKER_`-prefixed variables.

## Backend (Supabase project: `linkedin-activity-tracker`)

- `people`: who to track. Set `active = false` to pause someone; don't delete them.
- `posts`: one row per scraped post, deduped on `post_url`. `person_id` uses
  `ON DELETE RESTRICT`, so a person with posts can't be deleted by accident.
  `post_type` is free text (post, repost, quote repost, article, ...).

Fresh setup: run `schema.sql` in the project's SQL Editor.

A database created from the first version of `schema.sql` (cascade delete plus a
`post_type` check) also needs `migrations/001_restrict_fk_and_free_post_type.sql`.

## Config

```
cp .env.example .env   # then fill in TRACKER_SUPABASE_URL and TRACKER_SUPABASE_KEY
```

`.env` is git-ignored by the repo's root `.gitignore`.
