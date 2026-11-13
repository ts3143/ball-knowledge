-- Step 5 (chunk 1): a prompt freezes when a dive publishes it (hard rule 6: tiers freeze at publication).
-- Scheduling is publishing. For a prompt referenced by dive_prompts:
--   * its prompt_answers rows cannot be updated or deleted (inserts stay allowed, for data-gap fixes);
--   * its text, query, tags, sport, rarity, gate results and status cannot change.
-- `dives unschedule` (before the dive opens, no attempts) removes the reference and so lifts the freeze.

create function prompts_frozen_guard() returns trigger
language plpgsql set search_path = public as $$
begin
  if exists (select 1 from dive_prompts where prompt_id = old.id)
     and (old.template, old.params, old.text, old.answer_type, old.answer_query, old.sport, old.competition_id,
          old.club_ids, old.status, old.gate_results, old.answer_count, old.rarity)
         is distinct from
         (new.template, new.params, new.text, new.answer_type, new.answer_query, new.sport, new.competition_id,
          new.club_ids, new.status, new.gate_results, new.answer_count, new.rarity) then
    raise exception 'prompt % is in a dive and frozen', old.id using errcode = 'restrict_violation';
  end if;
  return new;
end $$;

create trigger prompts_frozen_guard before update on prompts
  for each row execute function prompts_frozen_guard();

create function prompt_answers_frozen_guard() returns trigger
language plpgsql set search_path = public as $$
begin
  if tg_op = 'TRUNCATE' then
    if exists (select 1 from dive_prompts) then
      raise exception 'prompt_answers cannot be truncated while dives reference prompts'
        using errcode = 'restrict_violation';
    end if;
    return null;
  end if;
  if exists (select 1 from dive_prompts where prompt_id = old.prompt_id)
     or (tg_op = 'UPDATE' and exists (select 1 from dive_prompts where prompt_id = new.prompt_id)) then
    raise exception 'prompt % is in a dive: its answer set is frozen (inserts only)', old.prompt_id
      using errcode = 'restrict_violation';
  end if;
  return case when tg_op = 'DELETE' then old else new end;
end $$;

create trigger prompt_answers_frozen_guard before update or delete on prompt_answers
  for each row execute function prompt_answers_frozen_guard();
create trigger prompt_answers_frozen_truncate before truncate on prompt_answers
  for each statement execute function prompt_answers_frozen_guard();

revoke execute on function prompts_frozen_guard(), prompt_answers_frozen_guard() from public, anon, authenticated;
