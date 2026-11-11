-- Clubs with at least one won fact in the competition.
select cl.wikidata_id as club
from entities cl
where cl.type = 'club'
  and exists (
    select 1 from facts w
    join entities comp on comp.id = w.object_id and comp.wikidata_id = %(competition)s
    where w.subject_id = cl.id and w.predicate = 'won' and not w.stale)
order by substr(cl.wikidata_id, 2)::bigint
