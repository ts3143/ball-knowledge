-- Wording polish (T, 2026-11-13): shipped prompts must read naturally without manual edits.
--
--   * entities.display_name: how a human is shown the entity. The English Wikipedia article title without a
--     trailing "(disambiguation)", else the Wikidata label (canonical_name stays as the label).
--   * entities.demonym: for countries, the English demonym from Wikidata P1549 ("Dutch"), chosen by the ingest:
--     preferred rank first, then the first English value in statement order. Null when there is none.
--   * entity_aliases.kind 'enwiki_title': the stripped English Wikipedia title as a matching alias, so "Ailton" or
--     "Takuma Asano" match. When it normalizes to the same text as another alias of the entity, the first kind in
--     priority order (label, enwiki_title, family_name, alt_label, short_name, nickname, translit) keeps the row.
--
-- Security: no new tables. entities keeps its RLS and revokes; bk_app's select grant on entities covers new columns.

alter table entities
  add column display_name text,
  add column demonym text;

alter table entity_aliases
  drop constraint entity_aliases_kind_check,
  add constraint entity_aliases_kind_check
    check (kind in ('label', 'alt_label', 'short_name', 'nickname', 'family_name', 'translit', 'enwiki_title'));
