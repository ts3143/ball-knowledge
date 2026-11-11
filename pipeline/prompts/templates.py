"""Prompt templates. Each one names its wording, a query that enumerates parameter combinations
(only those with at least one answer) and a query that returns the answer set as rows of
(entity_id, evidence_fact_ids, flags). The SQL lives in sql/ with psycopg named parameters.

Parameters are Wikidata QIDs (stable across re-ingests) or plain values such as event years.
Both queries also receive `competition` (QID) and `teams_per_season` from the run's settings.
"""

from dataclasses import dataclass
from pathlib import Path

SQL_DIR = Path(__file__).parent / "sql"


@dataclass(frozen=True)
class Template:
    id: str
    answer_type: str  # 'person' | 'club'
    # Filled from the params (QID params as entity names), plus {start_year} and {competition}.
    wording: str
    # Params that hold entity QIDs; their names fill the wording.
    qid_params: tuple[str, ...]
    # Subset of qid_params that are clubs: they become the prompt's club_ids tag.
    club_params: tuple[str, ...]

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
        wording="Name a player with {country} citizenship who played for {club} since {start_year}",
        qid_params=("country", "club"), club_params=("club",),
    ),
    Template(
        id="club_coaches", answer_type="person",
        wording="Name a head coach of {club} since {start_year} (interim coaches count)",
        qid_params=("club",), club_params=("club",),
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
