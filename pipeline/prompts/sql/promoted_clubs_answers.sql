-- Clubs promoted for season s (event year = s), s - 1 being fully listed.
-- Evidence: the club's competed_in fact for the season it came up in.
with seasons as (
  select cs.club_id, cs.season_start_year as y, cs.fact_id
  from club_seasons cs
  join entities comp on comp.id = cs.competition_id and comp.wikidata_id = %(competition)s
),
listed as (
  select y from seasons group by y having count(distinct club_id) = %(teams_per_season)s
)
select a.club_id as entity_id,
       array_agg(a.fact_id order by a.y) as evidence_fact_ids,
       array[]::text[] as flags
from seasons a
where a.y - 1 in (select y from listed)
  and not exists (select 1 from seasons b where b.club_id = a.club_id and b.y = a.y - 1)
  and a.y between %(from_year)s and %(to_year)s
group by a.club_id
order by a.club_id
