-- Follow-up to 20261117000000: a CHECK passes when its expression is NULL, and jsonb_typeof(NULL) is NULL, so
-- the approval check let a prompt with SQL-NULL rarity be approved. Make it false in that case.

alter table prompts
  drop constraint prompts_approved_has_rarity,
  add constraint prompts_approved_has_rarity check (status <> 'approved' or coalesce(jsonb_typeof(rarity) = 'object', false));
