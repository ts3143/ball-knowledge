-- Players with a played_for stint at both clubs. weak_stint: at either club, every stint is weak.
with at_a as (
  select s.person_id, array_agg(s.fact_id order by s.fact_id) as fact_ids,
         bool_and(cardinality(s.weak_reasons) > 0) as weak
  from stints(%(current_from_year)s) s
  join entities c on c.id = s.club_id and c.wikidata_id = %(club_a)s
  where s.predicate = 'played_for'
  group by s.person_id
),
at_b as (
  select s.person_id, array_agg(s.fact_id order by s.fact_id) as fact_ids,
         bool_and(cardinality(s.weak_reasons) > 0) as weak
  from stints(%(current_from_year)s) s
  join entities c on c.id = s.club_id and c.wikidata_id = %(club_b)s
  where s.predicate = 'played_for'
  group by s.person_id
)
select a.person_id as entity_id,
       a.fact_ids || b.fact_ids as evidence_fact_ids,
       case when a.weak or b.weak then array['weak_stint'] else array[]::text[] end as flags
from at_a a
join at_b b on b.person_id = a.person_id
order by a.person_id
