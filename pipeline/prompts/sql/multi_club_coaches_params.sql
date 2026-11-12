-- One row if any coach has dated coached seasons at two or more clubs of the competition.
select %(competition)s as competition
where exists (
  select 1
  from stint_seasons(%(current_from_year)s) ss
  join entities comp on comp.id = ss.competition_id and comp.wikidata_id = %(competition)s
  where ss.predicate = 'coached' and ss.date_quality <> 'undated'
    and not ('no_start' = any(ss.weak_reasons))
  group by ss.person_id
  having count(distinct ss.club_id) >= 2)
