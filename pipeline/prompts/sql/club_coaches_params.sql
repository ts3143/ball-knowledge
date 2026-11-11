-- Clubs of the competition with at least one coached stint.
select cl.wikidata_id as club
from entities cl
where cl.type = 'club'
  and exists (
    select 1 from club_seasons cs
    join entities comp on comp.id = cs.competition_id and comp.wikidata_id = %(competition)s
    where cs.club_id = cl.id)
  and exists (select 1 from stints(%(current_from_year)s) s where s.club_id = cl.id and s.predicate = 'coached')
order by substr(cl.wikidata_id, 2)::bigint
