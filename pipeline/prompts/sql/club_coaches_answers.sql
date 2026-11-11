-- Head coaches (interim included) of the club. weak_stint: every coached stint there is weak.
select s.person_id as entity_id,
       array_agg(s.fact_id order by s.fact_id) as evidence_fact_ids,
       case when bool_and(cardinality(s.weak_reasons) > 0) then array['weak_stint'] else array[]::text[] end as flags
from stints(%(current_from_year)s) s
join entities c on c.id = s.club_id and c.wikidata_id = %(club)s
where s.predicate = 'coached'
group by s.person_id
order by s.person_id
