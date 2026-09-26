-- LinkedIn Activity Tracker: schema
-- Supabase project: linkedin-activity-tracker (separate from every other project)
-- Safe to re-run: tables use IF NOT EXISTS and the seed skips existing URLs.

create table if not exists public.people (
  id           bigint generated always as identity primary key,
  name         text not null,
  linkedin_url text not null unique,
  active       boolean not null default true,   -- set false to pause tracking
  created_at   timestamptz not null default now()
);

create table if not exists public.posts (
  id          bigint generated always as identity primary key,
  person_id   bigint not null references public.people(id) on delete restrict,  -- keep past posts
  post_url    text not null unique,          -- dedupe key
  posted_at   timestamptz,
  text        text,
  reactions   integer,
  comments    integer,
  reposts     integer,
  post_type   text,                          -- e.g. 'post', 'repost'; free text so new types never break a run
  scraped_at  timestamptz not null default now()
);

create index if not exists posts_person_posted_idx
  on public.posts (person_id, posted_at desc);

-- Lock the tables down from the public API: no policies = anon key sees nothing.
-- The tracker uses the service_role key, which bypasses RLS.
alter table public.people enable row level security;
alter table public.posts  enable row level security;

-- Seed people (safe to re-run)
insert into public.people (name, linkedin_url) values
  ('Lauren Peate',        'https://www.linkedin.com/in/lpeate'),
  ('Mike Skeens',         'https://www.linkedin.com/in/michael-skeens-b87483123'),
  ('Sara Airoldi',        'https://www.linkedin.com/in/sarairoldi'),
  ('Maj Hallwass',        'https://www.linkedin.com/in/maj-hallwass'),
  ('Dr Carsten Hallwass', 'https://www.linkedin.com/in/dr-carsten-hallwass-6816b55a')
on conflict (linkedin_url) do nothing;
