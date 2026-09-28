-- Adds the repost columns that were first created by hand in the SQL Editor,
-- so the repo matches the live database. Safe to re-run.

alter table public.posts
  add column if not exists original_author text,
  add column if not exists original_text   text,
  add column if not exists original_url    text;
