-- (country, club) combinations with at least one player of that citizenship at the club.
-- Excluded countries (tunables prompts.excluded_countries) never become a parameter.
select co.wikidata_id as country, cl.wikidata_id as club
from facts n
join entities co on co.id = n.object_id and co.type = 'country'
                and co.wikidata_id <> all(%(excluded_countries)s::text[])
join stints(%(current_from_year)s) s on s.person_id = n.subject_id and s.predicate = 'played_for'
join entities cl on cl.id = s.club_id and cl.type = 'club'
where n.predicate = 'nationality' and not n.stale
  and exists (
    select 1 from club_seasons cs
    join entities comp on comp.id = cs.competition_id and comp.wikidata_id = %(competition)s
    where cs.club_id = cl.id)
group by co.wikidata_id, cl.wikidata_id
order by substr(co.wikidata_id, 2)::bigint, substr(cl.wikidata_id, 2)::bigint
