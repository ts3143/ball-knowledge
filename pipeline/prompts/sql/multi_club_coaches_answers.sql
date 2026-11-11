-- Coaches with a dated coached season at two or more distinct clubs of the competition.
-- Undated spells are skipped. edge_season: fewer than two clubs have a non-edge season.
-- weak_stint: fewer than two clubs have a non-weak season.
with per_club as (
  select ss.person_id, ss.club_id, array_agg(distinct ss.fact_id) as fact_ids,
         bool_or(not ss.edge) as has_certain, bool_or(cardinality(ss.weak_reasons) = 0) as has_strong
  from stint_seasons(%(current_from_year)s) ss
  join entities comp on comp.id = ss.competition_id and comp.wikidata_id = %(competition)s
  where ss.predicate = 'coached' and ss.date_quality <> 'undated'
  group by ss.person_id, ss.club_id
)
select person_id as entity_id,
       (select array_agg(f order by f) from per_club p2, unnest(p2.fact_ids) f
        where p2.person_id = per_club.person_id) as evidence_fact_ids,
       array_remove(array[case when count(*) filter (where has_certain) < 2 then 'edge_season' end,
                          case when count(*) filter (where has_strong) < 2 then 'weak_stint' end], null) as flags
from per_club
group by person_id
having count(*) >= 2
order by person_id
