-- Step 5 (chunk 1): `bk_app`, the least-privilege role the Next.js server connects as (hard rule 2: answer sets
-- never reach the browser; the app has no service-role key). NOLOGIN here: `npm run db:app-role` (chunk 2+) sets
-- its login password from APP_DATABASE_URL. Roles survive `supabase db reset`, hence the guard.
--
-- RLS applies to bk_app, so every table it touches has a policy for it; the grants decide what it may do.
-- It has no access at all to facts, entity_pageviews and the stints views.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'bk_app') then
    create role bk_app nologin nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
  end if;
end $$;

-- Lets `postgres` (the pipeline's role, and the tests) `set role bk_app`.
grant bk_app to postgres;
grant usage on schema public to bk_app;

-- Reads.
grant select on entities, entity_aliases, prompts, prompt_answers, users, dives, dive_prompts, attempts, answers,
  effective_answers, review_queue, ratings, rating_history, prompt_ratings to bk_app;
-- Players and their attempts.
grant insert, update on users, attempts to bk_app;
-- Answers are append-only (and triggers enforce it); a leftover for review is inserted or its count bumped.
grant insert on answers to bk_app;
grant insert on review_queue to bk_app;
grant update (occurrences, last_seen_at) on review_queue to bk_app;
-- Settling a dive.
grant update (settled_at) on dives to bk_app;
-- The rating caches are rebuilt freely.
grant insert, update, delete on ratings, rating_history, prompt_ratings to bk_app;

create policy bk_app_all on entities for all to bk_app using (true) with check (true);
create policy bk_app_all on entity_aliases for all to bk_app using (true) with check (true);
create policy bk_app_all on prompts for all to bk_app using (true) with check (true);
create policy bk_app_all on prompt_answers for all to bk_app using (true) with check (true);
create policy bk_app_all on users for all to bk_app using (true) with check (true);
create policy bk_app_all on dives for all to bk_app using (true) with check (true);
create policy bk_app_all on dive_prompts for all to bk_app using (true) with check (true);
create policy bk_app_all on attempts for all to bk_app using (true) with check (true);
create policy bk_app_all on answers for all to bk_app using (true) with check (true);
create policy bk_app_all on review_queue for all to bk_app using (true) with check (true);
create policy bk_app_all on ratings for all to bk_app using (true) with check (true);
create policy bk_app_all on rating_history for all to bk_app using (true) with check (true);
create policy bk_app_all on prompt_ratings for all to bk_app using (true) with check (true);
