-- Step 3: the rarity model's output, stored by `prompts generate` (the only writer).
--
-- prompt_answers gets the raw features the model used, its score and predicted share, the tier before
-- (model_tier) and after (tier) the cap on weak answers, and the points. prompts gets one jsonb with the model
-- version and parameters, so any stored tier can be reproduced (`python -m rarity check`).
--
-- Security: no new tables or functions. prompts and prompt_answers already have RLS enabled and all privileges
-- revoked from anon and authenticated (20261112000000_prompts.sql), which covers the new columns too: answer
-- sets, shares and tiers never reach the browser (hard rule 2).

alter table prompt_answers
  add column views_de bigint,
  add column views_en bigint,
  add column tie_amount float8,
  add column seasons_since float8,
  add column score float8,
  add column predicted_share float8,
  -- The tier the model gives; tier is the effective one (a weak answer is capped) and points follow it.
  add column model_tier smallint check (model_tier between 1 and 5),
  add column tier smallint check (tier between 1 and 5),
  add column points smallint,
  add column hipster_pick boolean not null default false,
  -- Rated or not at all: a prompt that fails the size gate has none of these.
  add constraint prompt_answers_rarity_all_or_none check (
    num_nonnulls(views_de, views_en, tie_amount, seasons_since, score, predicted_share, model_tier, tier, points)
      in (0, 9)),
  add constraint prompt_answers_tier_capped check (tier is null or tier <= model_tier);

-- model_version, params, reference_season, pageviews_through, expected_points, log_recall_mass.
alter table prompts
  add column rarity jsonb,
  -- Nothing is approved without its rarity (hard rule 6: tiers freeze at approval).
  add constraint prompts_approved_has_rarity check (status <> 'approved' or rarity is not null);
