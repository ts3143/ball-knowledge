-- Step 5 (chunk 1): answers (append-only, hard rule 4) and the review queue.
-- All ratings must be recomputable from answers, so a row is never changed or removed. The effective answer of a
-- position is its latest row by id: a retroactive credit (a data gap fixed after review) is an appended correction
-- row with origin 'review', not an update.
-- Written by the app as `bk_app` (insert only). Never exposed through PostgREST.

create table answers (
  id                 bigint generated always as identity primary key,
  attempt_id         bigint not null references attempts (id) on delete restrict,
  position           smallint not null check (position >= 1),
  prompt_id          bigint not null references prompts (id) on delete restrict,
  origin             text not null default 'play' check (origin in ('play', 'review')),
  corrects_answer_id bigint references answers (id) on delete restrict,
  -- What the player typed, as typed (null on timeout).
  raw_text           text check (char_length(raw_text) <= 200),
  input_norm         text,
  verdict            text not null
    check (verdict in ('matched', 'wrong', 'unmatched_entity', 'blank', 'late', 'timeout')),
  matched_entity_id  bigint references entities (id) on delete restrict,
  match_kind         text check (match_kind in ('full', 'surname', 'partial', 'fuzzy', 'review')),
  edit_distance      smallint check (edit_distance >= 0),
  ambiguous          boolean not null default false,
  -- Copied from the frozen prompt_answers row at match time.
  tier               smallint check (tier between 1 and 5),
  hipster_pick       boolean not null default false,
  points             smallint not null check (points >= 0),
  served_at          timestamptz not null,
  answered_at        timestamptz,
  response_ms        integer check (response_ms >= 0),
  matcher_version    text,
  created_at         timestamptz not null default now(),
  -- A correction names the row it corrects; a play row does not.
  constraint answers_correction_iff_review check ((origin = 'review') = (corrects_answer_id is not null)),
  -- A credit names its entity, tier and match kind; everything else scores 0.
  constraint answers_matched_has_credit check (
    case when verdict = 'matched'
      then matched_entity_id is not null and tier is not null and match_kind is not null
      else points = 0 end),
  -- A timeout has no answer time and no text; every other verdict was answered.
  constraint answers_timeout_shape check ((verdict = 'timeout') = (answered_at is null)
    and (verdict <> 'timeout' or raw_text is null))
);

-- One play row per position of an attempt (a double submit conflicts instead of double counting).
create unique index answers_one_play_per_position on answers (attempt_id, position) where origin = 'play';
create index answers_attempt_idx on answers (attempt_id, position, id);
create index answers_prompt_idx on answers (prompt_id);
create index answers_corrects_idx on answers (corrects_answer_id) where corrects_answer_id is not null;

create function answers_append_only() returns trigger
language plpgsql set search_path = public as $$
begin
  raise exception 'answers is append-only: % is not allowed (append a correction row instead)', tg_op
    using errcode = 'restrict_violation';
end $$;

create trigger answers_no_update_delete before update or delete on answers
  for each row execute function answers_append_only();
create trigger answers_no_truncate before truncate on answers
  for each statement execute function answers_append_only();

-- A correction describes the same attempt, position and prompt as the row it corrects.
create function answers_correction_guard() returns trigger
language plpgsql set search_path = public as $$
declare
  target answers%rowtype;
begin
  if new.origin = 'review' then
    select * into target from answers where id = new.corrects_answer_id;
    if (target.attempt_id, target.position, target.prompt_id) is distinct from
       (new.attempt_id, new.position, new.prompt_id) then
      raise exception 'answer % corrects answer %, which is another attempt, position or prompt', new.id, target.id
        using errcode = 'check_violation';
    end if;
  end if;
  return new;
end $$;

create trigger answers_correction_guard before insert on answers
  for each row execute function answers_correction_guard();

-- The effective answer per position: the latest row by id.
create view effective_answers with (security_invoker = true) as
select distinct on (attempt_id, position) *
from answers
order by attempt_id, position, id desc;

-- Answers that matching could not settle but whose input is a real entity (or similar): each unique leftover per
-- prompt is checked once, and the row is the cache. The Haiku check (later) fills the llm_* columns.
create table review_queue (
  id                   bigint generated always as identity primary key,
  prompt_id            bigint not null references prompts (id) on delete restrict,
  input_norm           text not null check (input_norm <> ''),
  sample_raw           text,
  candidate_entity_ids bigint[] not null default '{}',
  first_answer_id      bigint references answers (id) on delete restrict,
  occurrences          integer not null default 1 check (occurrences >= 1),
  last_seen_at         timestamptz not null default now(),
  status               text not null default 'pending' check (status in ('pending', 'llm_checked', 'resolved')),
  llm_model            text,
  llm_verdict          jsonb,
  llm_checked_at       timestamptz,
  resolution           text check (resolution in ('data_gap_fixed', 'not_valid')),
  resolved_at          timestamptz,
  note                 text,
  unique (prompt_id, input_norm),
  constraint review_queue_resolved_has_resolution check ((status = 'resolved') = (resolution is not null))
);

alter table answers enable row level security;
alter table review_queue enable row level security;

revoke all on answers, effective_answers, review_queue from anon, authenticated;
revoke execute on function answers_append_only(), answers_correction_guard() from public, anon, authenticated;
