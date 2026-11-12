-- Players with a played_for season at the club in a season the club won the competition.
-- Undated and end-only (no_start) spells are skipped. edge_season / weak_stint: every such title-season row is an edge / weak.
with title_rows as (
  select ss.person_id, ss.fact_id, w.id as won_fact_id, ss.edge, cardinality(ss.weak_reasons) > 0 as weak
  from stint_seasons(%(current_from_year)s) ss
  join entities c on c.id = ss.club_id and c.wikidata_id = %(club)s
  join entities comp on comp.id = ss.competition_id and comp.wikidata_id = %(competition)s
  join facts w on w.subject_id = ss.club_id and w.object_id = ss.competition_id
              and w.predicate = 'won' and not w.stale and w.start_year = ss.season_start_year
  where ss.predicate = 'played_for' and ss.date_quality <> 'undated'
    and not ('no_start' = any(ss.weak_reasons))
)
select person_id as entity_id,
       array_agg(distinct fact_id order by fact_id) || array_agg(distinct won_fact_id order by won_fact_id)
         as evidence_fact_ids,
       array_remove(array[case when bool_and(edge) then 'edge_season' end,
                          case when bool_and(weak) then 'weak_stint' end], null) as flags
from title_rows
group by person_id
order by person_id
