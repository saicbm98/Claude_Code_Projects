-- Brings a database created from the first version of schema.sql in line
-- with the current one:
--   1. posts.person_id: ON DELETE CASCADE -> ON DELETE RESTRICT
--   2. posts.post_type: drop the ('post', 'repost') check constraint
-- Safe to re-run.

begin;

alter table public.posts drop constraint if exists posts_person_id_fkey;
alter table public.posts
  add constraint posts_person_id_fkey
  foreign key (person_id) references public.people(id) on delete restrict;

alter table public.posts drop constraint if exists posts_post_type_check;

commit;

-- Check: should return exactly one row, the foreign key, with "ON DELETE RESTRICT".
select conname, pg_get_constraintdef(oid)
from pg_constraint
where conrelid = 'public.posts'::regclass
  and contype in ('f', 'c');
