-- Event-year range of all promotions the data can decide. One row, or none if there are no promotions.
-- A promotion counts only when the previous season is fully listed (teams_per_season clubs).
with seasons as (
  select cs.club_id, cs.season_start_year as y
  from club_seasons cs
  join entities comp on comp.id = cs.competition_id and comp.wikidata_id = %(competition)s
),
listed as (
  select y from seasons group by y having count(distinct club_id) >= %(teams_per_season)s
)
select %(competition)s as competition, min(a.y) as from_year, max(a.y) as to_year
from seasons a
where a.y - 1 in (select y from listed)
  and not exists (select 1 from seasons b where b.club_id = a.club_id and b.y = a.y - 1)
having count(*) > 0
