-- Step 5 (chunk 1): the rating caches, to the step-4 contract (~/.claude/plans/step4-rating-design.md section 5).
-- They are caches: a recompute empties them and rebuilds them from `answers` with the Glicko-2 engine.
-- params_hash is computed only in TypeScript. Written by the app as `bk_app`; never exposed through PostgREST.
-- Step 6 adds an owner-only select on ratings and rating_history; prompt_ratings stays server-only.

create table ratings (
  id             bigint generated always as identity primary key,
  user_id        bigint not null references users (id) on delete cascade,
  scope_type     text not null check (scope_type in ('sport', 'competition', 'club')),
  -- The sport's name is its id; a sport scope has no entity.
  sport          text not null,
  entity_id      bigint references entities (id),
  rating         float8 not null,
  rd             float8 not null,
  volatility     float8 not null,
  games          integer not null check (games >= 0),
  last_period    date,
  engine_version text not null,
  params_hash    text not null,
  updated_at     timestamptz not null default now(),
  check ((scope_type = 'sport') = (entity_id is null)),
  unique nulls not distinct (user_id, scope_type, sport, entity_id)
);

create table rating_history (
  id                bigint generated always as identity primary key,
  user_id           bigint not null references users (id) on delete cascade,
  attempt_id        bigint not null references attempts (id) on delete cascade,
  scope_type        text not null check (scope_type in ('sport', 'competition', 'club')),
  sport             text not null,
  entity_id         bigint references entities (id),
  -- The dive date (the rating period).
  period            date not null,
  rating_before     float8 not null,
  rd_before         float8 not null,
  volatility_before float8 not null,
  rating_after      float8 not null,
  rd_after          float8 not null,
  volatility_after  float8 not null,
  -- Games and points / scale of the period so far (see sim/rating/types.py HistoryRow).
  games             integer not null check (games >= 0),
  score_sum         float8 not null,
  engine_version    text not null,
  params_hash       text not null,
  check ((scope_type = 'sport') = (entity_id is null)),
  unique nulls not distinct (attempt_id, scope_type, sport, entity_id)
);

create index rating_history_user_idx on rating_history (user_id, period);

create table prompt_ratings (
  prompt_id      bigint primary key references prompts (id) on delete restrict,
  seed_rating    float8 not null,
  seed_rd        float8 not null,
  rating         float8 not null,
  rd             float8 not null,
  volatility     float8 not null,
  games          integer not null check (games >= 0),
  locked         boolean not null default false,
  last_period    date,
  engine_version text not null,
  params_hash    text not null,
  updated_at     timestamptz not null default now()
);

alter table ratings enable row level security;
alter table rating_history enable row level security;
alter table prompt_ratings enable row level security;

revoke all on ratings, rating_history, prompt_ratings from anon, authenticated;
