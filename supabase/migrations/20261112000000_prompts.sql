-- Prompts and their answer sets, plus the views prompt templates query.
-- Written only by the offline pipeline (direct Postgres connection as `postgres`).
-- Answer sets never reach the browser (hard rule 2): no policies, no grants for anon/authenticated.

create table prompts (
  id             bigint generated always as identity primary key,
  template       text not null,
  params         jsonb not null,
  -- Wording rendered from the template; the LLM only rephrases it later.
  text           text not null,
  answer_type    entity_type not null check (answer_type in ('person', 'club')),
  -- The rendered SQL whose result is the answer set (hard rule 1).
  answer_query   text not null,
  sport          text not null default 'football',
  competition_id bigint not null references entities (id),
  club_ids       bigint[] not null default '{}',
  status         text not null default 'candidate' check (status in ('candidate', 'gate_failed', 'gate_passed', 'approved')),
  gate_results   jsonb not null default '{}'::jsonb,
  answer_count   int not null default 0,
  -- max(facts.last_seen_at) when the answer set was generated.
  facts_as_of    timestamptz not null,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (template, params)
);

-- Step 3 adds share, tier, points and hipster columns.
create table prompt_answers (
  prompt_id         bigint not null references prompts (id) on delete cascade,
  -- Restrict: an approved answer set must not silently lose rows.
  entity_id         bigint not null references entities (id) on delete restrict,
  -- The facts that make this answer correct.
  evidence_fact_ids bigint[] not null,
  flags             text[] not null default '{}',
  primary key (prompt_id, entity_id)
);

-- One row per club-season in a competition.
create view club_seasons with (security_invoker = true) as
select
  f.id as fact_id,
  f.subject_id as club_id,
  f.object_id as competition_id,
  f.start_year as season_start_year,
  f.end_year as season_end_year,
  f.start_date as season_start,
  f.end_date as season_end,
  f.qualifiers ->> 'season' as season_qid
from facts f
where f.predicate = 'competed_in' and not f.stale;

-- One row per player or coach stint with date bounds derived from precision.
-- Unknown bounds get the benefit of the doubt (start -infinity, end infinity); the missing
-- data is recorded in weak_reasons, not in the season edge flag.
-- A known year with null precision (dewiki rows) counts as year precision.
create view stints with (security_invoker = true) as
select
  f.id as fact_id,
  f.subject_id as person_id,
  f.object_id as club_id,
  f.predicate,
  f.source,
  f.start_year,
  f.end_year,
  f.apps,
  f.date_quality,
  case p.sp
    when 11 then f.start_date
    when 10 then date_trunc('month', f.start_date)::date
    when 9 then make_date(f.start_year, 1, 1)
    else '-infinity'::date
  end as earliest_start,
  case p.sp
    when 11 then f.start_date
    when 10 then (date_trunc('month', f.start_date) + interval '1 month - 1 day')::date
    when 9 then make_date(f.start_year, 12, 31)
    else '-infinity'::date
  end as latest_start,
  case p.ep
    when 11 then f.end_date
    when 10 then date_trunc('month', f.end_date)::date
    when 9 then make_date(f.end_year, 1, 1)
    else 'infinity'::date
  end as earliest_end,
  case p.ep
    when 11 then f.end_date
    when 10 then (date_trunc('month', f.end_date) + interval '1 month - 1 day')::date
    when 9 then make_date(f.end_year, 12, 31)
    else 'infinity'::date
  end as latest_end,
  array_remove(array[
    case when f.date_quality = 'undated' then 'undated' end,
    case when f.date_quality = 'open_ended' then 'open_ended' end,
    case when f.predicate = 'played_for' and f.apps is null then 'no_apps' end,
    case when f.predicate = 'played_for' and f.apps = 0 then 'zero_apps' end
  ], null) as weak_reasons
from facts f
cross join lateral (
  select
    case
      when f.start_precision = 11 and f.start_date is not null then 11
      when f.start_precision = 10 and f.start_date is not null then 10
      when coalesce(f.start_precision, 9) <= 9 and f.start_year is not null then 9
    end as sp,
    case
      when f.end_precision = 11 and f.end_date is not null then 11
      when f.end_precision = 10 and f.end_date is not null then 10
      when coalesce(f.end_precision, 9) <= 9 and f.end_year is not null then 9
    end as ep
) p
where f.predicate in ('played_for', 'coached') and not f.stale;

-- Stints that possibly cover a season of the same club. edge = the stint only possibly
-- (not certainly) covers it, e.g. a year-precision stint's first and last seasons.
create view stint_seasons with (security_invoker = true) as
select
  s.fact_id,
  s.person_id,
  s.club_id,
  s.predicate,
  cs.competition_id,
  cs.season_start_year,
  cs.fact_id as season_fact_id,
  not (s.latest_start <= cs.season_end and s.earliest_end >= cs.season_start) as edge,
  s.weak_reasons
from stints s
join club_seasons cs on cs.club_id = s.club_id
where s.earliest_start <= cs.season_end and s.latest_end >= cs.season_start;

alter table prompts enable row level security;
alter table prompt_answers enable row level security;

revoke all on prompts, prompt_answers, club_seasons, stints, stint_seasons from anon, authenticated;
