-- Players with citizenship of the country and a played_for stint at the club.
with stints_at as (
  select s.person_id, array_agg(s.fact_id order by s.fact_id) as fact_ids,
         bool_and(cardinality(s.weak_reasons) > 0) as weak
  from stints(%(current_from_year)s) s
  join entities c on c.id = s.club_id and c.wikidata_id = %(club)s
  where s.predicate = 'played_for'
  group by s.person_id
),
citizen as (
  select n.subject_id as person_id, array_agg(n.id order by n.id) as fact_ids
  from facts n
  join entities co on co.id = n.object_id and co.wikidata_id = %(country)s
  where n.predicate = 'nationality' and not n.stale
  group by n.subject_id
)
select s.person_id as entity_id,
       s.fact_ids || c.fact_ids as evidence_fact_ids,
       case when s.weak then array['weak_stint'] else array[]::text[] end as flags
from stints_at s
join citizen c on c.person_id = s.person_id
order by s.person_id
