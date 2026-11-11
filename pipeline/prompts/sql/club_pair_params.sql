-- Club pairs (canonical order: lower numeric QID first) with at least one shared player.
with comp_clubs as (
  select c.id, c.wikidata_id, substr(c.wikidata_id, 2)::bigint as qid_num
  from entities c
  join club_seasons cs on cs.club_id = c.id
  join entities comp on comp.id = cs.competition_id and comp.wikidata_id = %(competition)s
  where c.type = 'club'
  group by c.id
),
played as (
  select distinct s.person_id, cc.wikidata_id, cc.qid_num
  from stints(%(current_from_year)s) s
  join comp_clubs cc on cc.id = s.club_id
  where s.predicate = 'played_for'
)
select a.wikidata_id as club_a, b.wikidata_id as club_b
from played a
join played b on b.person_id = a.person_id and b.qid_num > a.qid_num
group by a.wikidata_id, a.qid_num, b.wikidata_id, b.qid_num
order by a.qid_num, b.qid_num
