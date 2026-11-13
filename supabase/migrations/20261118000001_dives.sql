-- Step 5 (chunk 1): users, dives, the prompts of each dive, and attempts.
-- Written by the pipeline (dives, dive_prompts: `python -m dives`, as `postgres`) and by the app as `bk_app`
-- (users, attempts; see 20261118000005_app_role.sql). Never exposed through PostgREST: RLS on, all revoked.

create table users (
  id           bigint generated always as identity primary key,
  -- Supabase auth user (anonymous at first). Linking Google or email later keeps this row (step 6).
  auth_user_id uuid not null unique references auth.users (id) on delete restrict,
  created_at   timestamptz not null default now()
);

create table dives (
  id             bigint generated always as identity primary key,
  competition_id bigint not null references entities (id),
  sport          text not null,
  -- The calendar date in the reset time zone, or for a test dive a virtual date before 2000 (never a real one).
  dive_date      date not null,
  -- Computed in Python (zoneinfo) from tunables [dive]; the server needs no time zone code.
  opens_at       timestamptz not null,
  closes_at      timestamptz not null,
  -- Test dives (`python -m dives open-now`) are for the local database only.
  is_test        boolean not null default false,
  settled_at     timestamptz,
  created_at     timestamptz not null default now(),
  unique (competition_id, dive_date),
  check (closes_at > opens_at),
  -- Real and test dates cannot collide: the test range is below 2000-01-01 (tunables [dive] keeps inside it).
  constraint dives_test_date_range check (is_test = (dive_date < date '2000-01-01'))
);

create table dive_prompts (
  dive_id   bigint not null references dives (id) on delete cascade,
  position  smallint not null check (position >= 1),
  prompt_id bigint not null references prompts (id) on delete restrict,
  primary key (dive_id, position),
  -- Each prompt is rated on exactly one date (the per-user replay in step 5 relies on it).
  unique (prompt_id)
);

create table attempts (
  id                bigint generated always as identity primary key,
  user_id           bigint not null references users (id) on delete restrict,
  dive_id           bigint not null references dives (id) on delete restrict,
  rated             boolean not null,
  status            text not null default 'in_progress' check (status in ('in_progress', 'finished', 'abandoned')),
  started_at        timestamptz not null default now(),
  -- The position last served (0 = none yet) and when; the server timer runs from current_served_at.
  current_position  smallint not null default 0 check (current_position >= 0),
  current_served_at timestamptz,
  finished_at       timestamptz,
  -- A cache of the effective answers' points.
  score             smallint check (score >= 0),
  check (status <> 'finished' or finished_at is not null)
);

-- One rated attempt per user per dive (hard rule 5). Archive plays (rated = false) are unlimited.
create unique index attempts_one_rated_per_dive on attempts (user_id, dive_id) where rated;
create index attempts_dive_idx on attempts (dive_id);

-- A dive's prompts must be approved and belong to the dive's competition and sport, and the list of a dive
-- somebody has already started cannot change.
create function dive_prompts_guard() returns trigger
language plpgsql set search_path = public as $$
declare
  p prompts%rowtype;
  d dives%rowtype;
  target_dive bigint := case when tg_op = 'DELETE' then old.dive_id else new.dive_id end;
begin
  if exists (select 1 from attempts where dive_id = target_dive) then
    raise exception 'dive % has attempts: its prompts cannot change', target_dive using errcode = 'check_violation';
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  select * into p from prompts where id = new.prompt_id;
  select * into d from dives where id = new.dive_id;
  if p.status <> 'approved' then
    raise exception 'prompt % is %: only approved prompts can be scheduled', p.id, p.status
      using errcode = 'check_violation';
  end if;
  if p.competition_id <> d.competition_id or p.sport <> d.sport then
    raise exception 'prompt % belongs to another competition or sport than dive %', p.id, d.id
      using errcode = 'check_violation';
  end if;
  return new;
end $$;

create trigger dive_prompts_guard before insert or update or delete on dive_prompts
  for each row execute function dive_prompts_guard();

alter table users enable row level security;
alter table dives enable row level security;
alter table dive_prompts enable row level security;
alter table attempts enable row level security;

revoke all on users, dives, dive_prompts, attempts from anon, authenticated;
revoke execute on function dive_prompts_guard() from public, anon, authenticated;
