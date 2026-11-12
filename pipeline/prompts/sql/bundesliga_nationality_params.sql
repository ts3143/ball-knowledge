-- Countries with at least one citizen who played a dated season in the competition.
-- Excluded countries (tunables prompts.excluded_countries) never become a parameter.
select co.wikidata_id as country
from entities co
where co.type = 'country'
  and co.wikidata_id <> all(%(excluded_countries)s::text[])
  and exists (
    select 1
    from facts n
    join stint_seasons(%(current_from_year)s) ss on ss.person_id = n.subject_id
    join entities comp on comp.id = ss.competition_id and comp.wikidata_id = %(competition)s
    where n.object_id = co.id and n.predicate = 'nationality' and not n.stale
      and ss.predicate = 'played_for' and ss.date_quality <> 'undated'
      and not ('no_start' = any(ss.weak_reasons)))
order by substr(co.wikidata_id, 2)::bigint
