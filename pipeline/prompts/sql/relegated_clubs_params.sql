-- Event-year range of the relegations the data can decide. One row, or none if there are no relegations.
-- A relegation in event year e (the end of season e - 1) is decidable only when seasons e - 1 and e are both
-- fully listed (exactly teams_per_season clubs). The range is the latest run of consecutive decidable years
-- ending at the most recent one, so a short season in the middle cuts the range there.
with seasons as (
  select cs.club_id, cs.season_start_year as y
  from club_seasons cs
  join entities comp on comp.id = cs.competition_id and comp.wikidata_id = %(competition)s
),
listed as (
  select y from seasons group by y having count(distinct club_id) = %(teams_per_season)s
),
decidable as (
  select l.y + 1 as e from listed l where l.y + 1 in (select y from listed)
),
runs as (
  select e, e - row_number() over (order by e) as grp from decidable
),
latest as (
  select min(e) as from_year, max(e) as to_year from runs
  where grp = (select grp from runs order by e desc limit 1)
)
select %(competition)s as competition, from_year, to_year
from latest
where exists (
  select 1 from seasons a
  where a.y + 1 between latest.from_year and latest.to_year
    and not exists (select 1 from seasons b where b.club_id = a.club_id and b.y = a.y + 1))
