"""Prompt templates. Each one names its wording, a query that enumerates parameter combinations
(only those with at least one answer) and a query that returns the answer set as rows of
(entity_id, evidence_fact_ids, flags). The SQL lives in sql/ with psycopg named parameters.

Parameters are Wikidata QIDs (stable across re-ingests) or plain values such as event years.
Both queries also receive `competition` (QID), `teams_per_season` and `current_from_year` (open-ended
spells starting at or after it are current, not weak) from the run's settings.
"""

from dataclasses import dataclass
from pathlib import Path

SQL_DIR = Path(__file__).parent / "sql"


@dataclass(frozen=True)
class Template:
    id: str
    answer_type: str  # 'person' | 'club'
    # Filled from the params (QID params as display names), plus {start_year} and {competition} (the name in
    # tunables [competitions.*], else the entity's display name).
    wording: str
    # Params that hold entity QIDs; their display names fill the wording. A country param also gives {demonym}.
    qid_params: tuple[str, ...]
    # Subset of qid_params that are clubs: they become the prompt's club_ids tag.
    club_params: tuple[str, ...]
    # Used instead of `wording` when the country has no English demonym: "Name a player from {country} ...".
    wording_without_demonym: str | None = None

    @property
    def params_sql(self) -> str:
        return (SQL_DIR / f"{self.id}_params.sql").read_text()

    @property
    def answers_sql(self) -> str:
        return (SQL_DIR / f"{self.id}_answers.sql").read_text()


TEMPLATES: dict[str, Template] = {t.id: t for t in [
    Template(
        id="club_pair", answer_type="person",
        wording="Name a player who played for both {club_a} and {club_b} since {start_year}",
        qid_params=("club_a", "club_b"), club_params=("club_a", "club_b"),
    ),
    Template(
        id="club_nationality", answer_type="person",
        wording="Name {a_demonym} player who played for {club} since {start_year}",
        wording_without_demonym="Name a player from {country} who played for {club} since {start_year}",
        qid_params=("country", "club"), club_params=("club",),
    ),
    Template(
        id="club_coaches", answer_type="person",
        wording="Name a head coach of {club} since {start_year} (interim coaches count)",
        qid_params=("club",), club_params=("club",),
    ),
    Template(
        id="bundesliga_nationality", answer_type="person",
        wording="Name {a_demonym} player who played in the {competition} since {start_year}",
        wording_without_demonym="Name a player from {country} who played in the {competition} since {start_year}",
        qid_params=("country",), club_params=(),
    ),
    Template(
        id="club_title_winners", answer_type="person",
        wording="Name a player who won the {competition} with {club} since {start_year}",
        qid_params=("club",), club_params=("club",),
    ),
    Template(
        id="multi_club_coaches", answer_type="person",
        wording="Name a coach who managed at least two {competition} clubs since {start_year}",
        qid_params=(), club_params=(),
    ),
    Template(
        id="relegated_clubs", answer_type="club",
        wording="Name a club relegated from the {competition} between {from_year} and {to_year}",
        qid_params=(), club_params=(),
    ),
    Template(
        id="promoted_clubs", answer_type="club",
        wording="Name a club promoted to the {competition} between {from_year} and {to_year}",
        qid_params=(), club_params=(),
    ),
]}
