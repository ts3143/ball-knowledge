-- Spells German Wikipedia confirms as current are not weak (T, 2026-11-11). An open-ended spell gets
-- no 'open_ended' reason when it started at or after current_from_year (as before), or when the infobox
-- shows it as the person's current club: a dewiki fact with no end (open rows are only read for the
-- current club) or a Wikidata spell the ingest marked `dewiki_current`. Same signature and columns.

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
