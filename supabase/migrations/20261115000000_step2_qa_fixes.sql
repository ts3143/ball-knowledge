-- Step 2 QA fixes.
--
-- 1. stints(): a spell that ends at a known date but has no start year gets the weak reason 'no_start'.
--    Its start is treated as -infinity, which made it a certain, unflagged cover of every earlier season.
--    Club templates still count such spells (flagged weak); season templates skip them, as they skip
--    undated ones. Same signature and columns.
-- 2. prompts: uniqueness includes the competition, so a second competition can reuse template + params.
-- 3. club_seasons: a season still running (no end date) ends at infinity, so stint_seasons keeps it.
-- 4. Sequences: Supabase's default ACL grants them to anon/authenticated; revoke, and for future ones too.

create or replace function stints(current_from_year int)
returns table (
  fact_id bigint, person_id bigint, club_id bigint, predicate text, source text,
  start_year int, end_year int, apps int, date_quality text,
  earliest_start date, latest_start date, earliest_end date, latest_end date, weak_reasons text[]
)
language sql stable as $$
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
    case when f.date_quality = 'open_ended' and (f.start_year is null or f.start_year < current_from_year)
              and not (f.source = 'dewiki' or f.qualifiers ? 'dewiki_current')
         then 'open_ended' end,
    case when p.sp is null and p.ep is not null then 'no_start' end,
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
where f.predicate in ('played_for', 'coached') and not f.stale
$$;


revoke execute on function stints(int) from public, anon, authenticated;

alter table prompts drop constraint prompts_template_params_key;
alter table prompts add constraint prompts_competition_template_params_key unique (competition_id, template, params);

create or replace view club_seasons with (security_invoker = true) as
select
  f.id as fact_id,
  f.subject_id as club_id,
  f.object_id as competition_id,
  f.start_year as season_start_year,
  f.end_year as season_end_year,
  f.start_date as season_start,
  coalesce(f.end_date, 'infinity'::date) as season_end,
  f.qualifiers ->> 'season' as season_qid
from facts f
where f.predicate = 'competed_in' and not f.stale;

revoke all on all sequences in schema public from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
