-- Reference data: entities, their aliases, structured facts, and raw pageviews.
-- Written only by the offline pipeline (direct Postgres connection as `postgres`).
-- Never exposed to the browser: facts are the source of every answer set (hard rule 2).

create type entity_type as enum ('person', 'club', 'competition', 'country', 'position');

create table entities (
  id             bigint generated always as identity primary key,
  type           entity_type not null,
  wikidata_id    text not null unique check (wikidata_id ~ '^Q[0-9]+$'),
  canonical_name text not null,
  description    text,
  dewiki_title   text,
  enwiki_title   text,
  -- Computed in step 3 from pageviews, tie and recency. Null until then.
  fame_score     double precision,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  last_seen_at   timestamptz not null default now()
);

create index entities_type_idx on entities (type);

create table entity_aliases (
  id           bigint generated always as identity primary key,
  entity_id    bigint not null references entities (id) on delete cascade,
  alias        text not null,
  alias_norm   text not null check (alias_norm <> ''),
  kind         text not null check (kind in ('label', 'alt_label', 'short_name', 'nickname', 'family_name', 'translit')),
  lang         text,
  last_seen_at timestamptz not null default now(),
  stale        boolean not null default false,
  unique (entity_id, alias_norm)
);

create index entity_aliases_norm_idx on entity_aliases (alias_norm);

create table facts (
  id              bigint generated always as identity primary key,
  subject_id      bigint not null references entities (id) on delete cascade,
  predicate       text not null check (predicate in ('played_for', 'coached', 'competed_in', 'won', 'nationality', 'position', 'born')),
  object_id       bigint references entities (id) on delete cascade,
  value_date      date,
  -- Years are what prompts use. Dates and Wikidata precisions (9 = year, 10 = month, 11 = day) are kept raw.
  start_year      int,
  end_year        int,
  start_date      date,
  start_precision smallint,
  end_date        date,
  end_precision   smallint,
  -- dated: end year known (or season facts). open_ended: start only. undated: neither.
  date_quality    text check (date_quality in ('dated', 'open_ended', 'undated')),
  apps            int,
  goals           int,
  qualifiers      jsonb not null default '{}'::jsonb,
  source          text not null default 'wikidata',
  -- Wikidata statement GUID, or a synthetic key such as season:<Q>:club:<Q>.
  source_ref      text not null unique,
  last_seen_at    timestamptz not null default now(),
  stale           boolean not null default false
);

create index facts_subject_idx on facts (subject_id, predicate);
create index facts_object_idx on facts (object_id, predicate);

create table entity_pageviews (
  entity_id bigint not null references entities (id) on delete cascade,
  wiki      text not null check (wiki in ('de', 'en')),
  month     date not null check (extract(day from month) = 1),
  views     int not null check (views > 0),
  primary key (entity_id, wiki, month)
);

-- Lock the tables away from the Data API roles. RLS with no policies denies everything;
-- the explicit revokes cover Supabase's default grants on new public tables.
alter table entities enable row level security;
alter table entity_aliases enable row level security;
alter table facts enable row level security;
alter table entity_pageviews enable row level security;

revoke all on entities, entity_aliases, facts, entity_pageviews from anon, authenticated;
