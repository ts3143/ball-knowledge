-- Step 3 QA fix: prompts.rarity is SQL NULL when a prompt is not rated, never JSON null.
--
-- The generator used to write a JSON null for prompts that fail the size gate. JSON null passes
-- `rarity is not null`, so such a prompt could be approved without tiers. This clears the stray JSON nulls and
-- makes the checks say what they mean: rarity is null or an object, and an approved prompt has an object.
-- No new tables, functions or columns: the existing RLS and revokes are unchanged.

update prompts set rarity = null where jsonb_typeof(rarity) = 'null';

alter table prompts
  drop constraint prompts_approved_has_rarity,
  add constraint prompts_approved_has_rarity check (status <> 'approved' or jsonb_typeof(rarity) = 'object'),
  add constraint prompts_rarity_is_object check (rarity is null or jsonb_typeof(rarity) = 'object');
