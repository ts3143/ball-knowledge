-- Current spells are not weak (T): an open-ended spell that started at or after current_from_year
-- (the latest competition season minus the tunable window) has no 'open_ended' weak reason.
-- A view cannot take that cutoff, so stints and stint_seasons become functions; the generator renders
-- the cutoff as a literal into each stored answer_query. Same columns as the views, plus date_quality
-- on stint_seasons. club_seasons stays a view.

drop view stint_seasons;
drop view stints;

create function stints(current_from_year int)
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
         then 'open_ended' end,
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

-- Stints that possibly cover a season of the same club. edge = the stint only possibly
-- (not certainly) covers it, e.g. a year-precision stint's first and last seasons.
create function stint_seasons(current_from_year int)
returns table (
  fact_id bigint, person_id bigint, club_id bigint, predicate text, competition_id bigint,
  season_start_year int, season_fact_id bigint, edge boolean, date_quality text, weak_reasons text[]
)
language sql stable as $$
select
  s.fact_id,
  s.person_id,
  s.club_id,
  s.predicate,
  cs.competition_id,
  cs.season_start_year,
  cs.fact_id as season_fact_id,
  not (s.latest_start <= cs.season_end and s.earliest_end >= cs.season_start) as edge,
  s.date_quality,
  s.weak_reasons
from stints(current_from_year) s
join club_seasons cs on cs.club_id = s.club_id
where s.earliest_start <= cs.season_end and s.latest_end >= cs.season_start
$$;

-- Functions are executable by PUBLIC by default and Supabase exposes public functions over RPC.
-- Answer data must never reach the browser.
revoke execute on function stints(int), stint_seasons(int) from public, anon, authenticated;
