-- Exception to answers append-only, approved by T 2026-11-12: local test dives only.
--
-- Test dives (dives.is_test, virtual dates before 2000) are disposable: `python -m dives purge-test` removes a
-- test dive with its attempts and answers so its prompts can be used again. Real answers stay append-only.
-- A DELETE on answers is allowed only when ALL hold:
--   * the session flag bk.purge_test_dives is 'on' (the purge sets it, transaction-local);
--   * the role is a superuser or `postgres` (the pipeline's role). Local Supabase's postgres is not a true
--     superuser, hence the name check. bk_app and every other role can never do it, flag or not: after
--     `set role bk_app` current_user is bk_app;
--   * the row's attempt belongs to a test dive.
-- UPDATE and TRUNCATE stay blocked unconditionally.

create or replace function answers_append_only() returns trigger
language plpgsql set search_path = public as $$
begin
  if tg_op = 'DELETE'
     and coalesce(current_setting('bk.purge_test_dives', true), '') = 'on'
     and (current_setting('is_superuser') = 'on' or current_user = 'postgres')
     and exists (select 1 from attempts a join dives d on d.id = a.dive_id
                 where a.id = old.attempt_id and d.is_test) then
    return old;
  end if;
  raise exception 'answers is append-only: % is not allowed (only `python -m dives purge-test` may delete the '
                  'answers of test dives)', tg_op
    using errcode = 'restrict_violation';
end $$;

revoke execute on function answers_append_only() from public, anon, authenticated;

-- One row per purge, so a removal is never silent.
create table test_purges (
  id          bigint generated always as identity primary key,
  purged_at   timestamptz not null default now(),
  purged_by   text not null default session_user,
  dive_dates  date[] not null,
  counts      jsonb not null
);

alter table test_purges enable row level security;
revoke all on test_purges from anon, authenticated, bk_app;
