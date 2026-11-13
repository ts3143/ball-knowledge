"""Placeholder rows for the dive tests, added to a db_world.World inside its rolled-back transaction: prompts
in any status (with the frozen rarity features the seed difficulty needs), their answers, users, dives and
attempts. Nothing here is a sports fact: names are "Placeholder ...", numbers are arbitrary."""

from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

TIERS = [(1, 10), (2, 25), (3, 50), (4, 75), (5, 100)]  # (tier, points), as in [tiers]


def gate_results(n_eff: float = 10.0, passed: bool = True) -> dict:
    return {"size": {"n": 20, "min": 12, "max": 300, "pass": True},
            "tiers": {"n": 20, "pass": passed, "n_eff": n_eff, "reasons": [], "tier_counts": [4, 4, 4, 4, 4]},
            "completeness": "not_run"}


def add_prompt(w, n: int, status: str = "approved", template: str = "club_pair", clubs: tuple[str, ...] = (),
               mass: float | None = None, competition: dict | None = None, text: str | None = None) -> int:
    """A prompt row; `mass` sets its seed difficulty (a higher log_recall_mass is an easier prompt). The default
    gives every n a different seed, rising with n."""
    mass = -3.0 - n * 0.05 if mass is None else mass
    rarity = Jsonb({"model_version": "test", "log_recall_mass": mass}) if status != "gate_failed" else None
    gates = gate_results(passed=status != "gate_failed")
    return w.cur.execute(
        "insert into prompts (template, params, text, answer_type, answer_query, competition_id, club_ids, status,"
        " gate_results, answer_count, facts_as_of, rarity)"
        " values (%s, %s, %s, 'person', 'select 1', %s, %s, %s, %s, 20, now(), %s) returning id",
        (template, Jsonb({"n": n}), text or f"Placeholder prompt {n}", (competition or w.comp)["id"],
         [w.clubs[c]["id"] for c in clubs], status, Jsonb(gates), rarity)).fetchone()["id"]


def add_answers(w, prompt_id: int, count: int = 8, weak: int = 0) -> list[int]:
    """`count` person answers with decreasing predicted share and rising tier; the last `weak` are flagged."""
    ids = []
    for i in range(count):
        person = w.person(f"Placeholder Person {prompt_id}-{i}")
        tier, points = TIERS[min(4, i * 5 // count)]
        w.cur.execute(
            "insert into prompt_answers (prompt_id, entity_id, evidence_fact_ids, flags, views_de, views_en,"
            " tie_amount, seasons_since, score, predicted_share, model_tier, tier, points)"
            " values (%s, %s, '{}', %s, 10, 10, 1, 0, %s, %s, %s, %s, %s)",
            (prompt_id, person["id"], ["weak_stint"] if i >= count - weak else [], -float(i),
             round(0.5 / (i + 1), 6), tier, tier, points))
        ids.append(person["id"])
    w.cur.execute("update prompts set answer_count = %s where id = %s", (count, prompt_id))
    return ids


def add_user(w) -> int:
    auth_id = w.cur.execute("insert into auth.users (id) values (gen_random_uuid()) returning id").fetchone()["id"]
    return w.cur.execute("insert into users (auth_user_id) values (%s) returning id", (auth_id,)).fetchone()["id"]


def add_dive(w, day, prompt_ids: list[int], opens_at: datetime | None = None, is_test: bool = False,
             competition: dict | None = None) -> int:
    opens_at = opens_at or datetime(2030, 1, 1, tzinfo=UTC)
    dive_id = w.cur.execute(
        "insert into dives (competition_id, sport, dive_date, opens_at, closes_at, is_test)"
        " values (%s, 'football', %s, %s, %s, %s) returning id",
        ((competition or w.comp)["id"], day, opens_at, opens_at + timedelta(days=1), is_test)).fetchone()["id"]
    for position, pid in enumerate(prompt_ids, 1):
        w.cur.execute("insert into dive_prompts (dive_id, position, prompt_id) values (%s, %s, %s)",
                      (dive_id, position, pid))
    return dive_id


def add_attempt(w, user_id: int, dive_id: int, rated: bool = True) -> int:
    return w.cur.execute("insert into attempts (user_id, dive_id, rated) values (%s, %s, %s) returning id",
                         (user_id, dive_id, rated)).fetchone()["id"]


def add_answer(w, attempt_id: int, position: int, prompt_id: int, verdict: str = "wrong", points: int = 0,
               entity: int | None = None, **cols) -> int:
    """An answers row (a play row by default). A matched verdict gets entity, tier 3 and match_kind 'full'."""
    row = {"attempt_id": attempt_id, "position": position, "prompt_id": prompt_id, "verdict": verdict,
           "points": points, "served_at": datetime(2030, 1, 1, tzinfo=UTC),
           "answered_at": datetime(2030, 1, 1, 0, 0, 5, tzinfo=UTC), "raw_text": "placeholder", **cols}
    if verdict == "matched":
        row.update({"matched_entity_id": entity, "tier": 3, "match_kind": "full", **cols})
    if verdict == "timeout":
        row.update({"answered_at": None, "raw_text": None, **cols})
    return w.cur.execute(
        f"insert into answers ({', '.join(row)}) values ({', '.join(['%s'] * len(row))}) returning id",
        tuple(row.values())).fetchone()["id"]

