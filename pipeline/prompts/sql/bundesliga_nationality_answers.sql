-- Players with citizenship of the country and a played_for season in the competition.
-- Undated spells say nothing about which season, so they are skipped.
-- edge_season: every season row is an edge. weak_stint: every season row has a weak reason.
with seasons as (
  select ss.person_id, ss.fact_id, ss.edge, cardinality(ss.weak_reasons) > 0 as weak
  from stint_seasons(%(current_from_year)s) ss
  join entities comp on comp.id = ss.competition_id and comp.wikidata_id = %(competition)s
  where ss.predicate = 'played_for' and ss.date_quality <> 'undated'
),
played as (
  select person_id, array_agg(distinct fact_id order by fact_id) as fact_ids,
         bool_and(edge) as all_edge, bool_and(weak) as all_weak
  from seasons
  group by person_id
),
citizen as (
  select n.subject_id as person_id, array_agg(n.id order by n.id) as fact_ids
  from facts n
  join entities co on co.id = n.object_id and co.wikidata_id = %(country)s
  where n.predicate = 'nationality' and not n.stale
  group by n.subject_id
)
select p.person_id as entity_id,
       p.fact_ids || c.fact_ids as evidence_fact_ids,
       array_remove(array[case when p.all_edge then 'edge_season' end,
                          case when p.all_weak then 'weak_stint' end], null) as flags
from played p
join citizen c on c.person_id = p.person_id
order by p.person_id
