"""The step-5 schema: constraints, triggers and the bk_app grants, on placeholder rows inside a rolled-back
transaction. Needs the local database."""

from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest
from psycopg import errors

from tests.db_dives import add_answer, add_answers, add_attempt, add_dive, add_prompt, add_user
from tests.db_world import World

pytestmark = pytest.mark.db


@dataclass
class Play:
    w: World
    user: int
    dive: int
    attempt: int
    prompt: int


@pytest.fixture
def play(world):
    """One approved prompt in a dive with a user's attempt."""
    w = world
    prompt = add_prompt(w, 1)
    add_answers(w, prompt)
    user = add_user(w)
    dive = add_dive(w, date(2030, 1, 1), [prompt])
    return Play(w, user, dive, add_attempt(w, user, dive), prompt)


def refuses(conn, error, sql, params=()):
    """Asserts that the statement raises `error`; its savepoint is rolled back so the test can go on."""
    with pytest.raises(error), conn.transaction():
        conn.execute(sql, params)


# ---------------------------------------------------------------------------
# answers: append-only
# ---------------------------------------------------------------------------

def test_answers_are_append_only(conn, play):
    w, attempt, prompt = play.w, play.attempt, play.prompt
    answer = add_answer(w, attempt, 1, prompt)
    refuses(conn, errors.RestrictViolation, "update answers set points = 100 where id = %s", (answer,))
    refuses(conn, errors.RestrictViolation, "update answers set raw_text = 'other' where id = %s", (answer,))
    refuses(conn, errors.RestrictViolation, "delete from answers where id = %s", (answer,))
    refuses(conn, errors.RestrictViolation, "truncate answers, review_queue")
    refuses(conn, errors.RestrictViolation, "truncate answers cascade")
    assert w.cur.execute("select points, raw_text from answers where id = %s", (answer,)).fetchone() == {
        "points": 0, "raw_text": "placeholder"}


def test_a_position_has_one_play_row_but_any_number_of_corrections(conn, play):
    w, attempt, prompt = play.w, play.attempt, play.prompt
    first = add_answer(w, attempt, 1, prompt, "unmatched_entity")
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        add_answer(w, attempt, 1, prompt)  # a double submit conflicts instead of double counting
    entity = w.person("Placeholder Credited")["id"]
    second = add_answer(w, attempt, 1, prompt, "matched", 50, entity, origin="review", corrects_answer_id=first,
                        match_kind="review")
    third = add_answer(w, attempt, 1, prompt, "matched", 75, entity, origin="review", corrects_answer_id=second,
                       match_kind="review", tier=4)
    effective = w.cur.execute("select id, points from effective_answers where attempt_id = %s", (attempt,)).fetchall()
    assert effective == [{"id": third, "points": 75}]  # the latest row by id is the effective one


def test_corrections_must_name_a_row_of_the_same_position(conn, play):
    w, user, attempt, prompt = play.w, play.user, play.attempt, play.prompt
    other_prompt = add_prompt(w, 2)
    dive2 = add_dive(w, date(2030, 1, 2), [other_prompt])
    attempt2 = add_attempt(w, user, dive2)
    first = add_answer(w, attempt, 1, prompt)
    entity = w.person("Placeholder Credited")["id"]
    with pytest.raises(errors.CheckViolation, match="another attempt, position or prompt"), conn.transaction():
        add_answer(w, attempt2, 1, other_prompt, "matched", 50, entity, origin="review", corrects_answer_id=first,
                   match_kind="review")
    with pytest.raises(errors.CheckViolation), conn.transaction():
        add_answer(w, attempt, 1, prompt, origin="review")  # a correction with nothing to correct
    with pytest.raises(errors.CheckViolation, match="answers_correction_iff_review"), conn.transaction():
        add_answer(w, attempt, 2, prompt, corrects_answer_id=first)  # a play row that corrects something


def test_verdict_and_points_checks(conn, play):
    w, attempt, prompt = play.w, play.attempt, play.prompt
    entity = w.person("Placeholder Credited")["id"]
    add_answer(w, attempt, 1, prompt, "matched", 50, entity)  # a credit with its entity, tier and kind
    cases = [
        ("a credit needs its entity", {"verdict": "matched", "points": 50, "matched_entity_id": None, "tier": 3,
                                           "match_kind": "full"}, "answers_matched_has_credit"),
        ("a credit needs its tier", {"verdict": "matched", "points": 50, "matched_entity_id": entity, "tier": None,
                                         "match_kind": "full"}, "answers_matched_has_credit"),
        ("a wrong answer scores 0", {"verdict": "wrong", "points": 10}, "answers_matched_has_credit"),
        ("a late answer scores 0", {"verdict": "late", "points": 25}, "answers_matched_has_credit"),
        ("a timeout has no answer time", {"verdict": "timeout", "points": 0}, "answers_timeout_shape"),
        ("a timeout has no text", {"verdict": "timeout", "points": 0, "answered_at": None}, "answers_timeout_shape"),
        ("an answered row has a time", {"verdict": "wrong", "answered_at": None}, "answers_timeout_shape"),
        ("an unknown verdict", {"verdict": "great"}, "answers_verdict_check"),
        ("text is capped", {"raw_text": "x" * 201}, "answers_raw_text_check"),
        ("tiers are 1 to 5", {"verdict": "matched", "points": 50, "matched_entity_id": entity, "tier": 6,
                                  "match_kind": "full"}, "answers_tier_check"),
    ]
    for position, (why, cols, constraint) in enumerate(cases, 2):
        with pytest.raises(errors.CheckViolation, match=constraint), conn.transaction():
            _raw_answer(w, attempt, position, prompt, cols)
    # the legal shapes
    add_answer(w, attempt, 20, prompt, "timeout")
    add_answer(w, attempt, 21, prompt, "blank", raw_text="")
    add_answer(w, attempt, 22, prompt, "late")
    add_answer(w, attempt, 23, prompt, "unmatched_entity")


def _raw_answer(w, attempt, position, prompt, cols):
    """An answers row from exactly these columns over a valid wrong answer (None means null)."""
    row = {"attempt_id": attempt, "position": position, "prompt_id": prompt, "verdict": "wrong", "points": 0,
           "served_at": datetime(2030, 1, 1, tzinfo=UTC), "answered_at": datetime(2030, 1, 1, tzinfo=UTC),
           "raw_text": "placeholder", **cols}
    w.cur.execute(f"insert into answers ({', '.join(row)}) values ({', '.join(['%s'] * len(row))})",
                  tuple(row.values()))


# ---------------------------------------------------------------------------
# attempts and dives
# ---------------------------------------------------------------------------

def test_one_rated_attempt_per_user_per_dive(conn, play):
    w, user, dive = play.w, play.user, play.dive
    with pytest.raises(errors.UniqueViolation, match="attempts_one_rated_per_dive"), conn.transaction():
        add_attempt(w, user, dive)
    add_attempt(w, user, dive, rated=False)  # archive plays are unrated and unlimited
    add_attempt(w, user, dive, rated=False)
    add_attempt(w, add_user(w), dive)  # another user's rated attempt is fine


def test_dive_dates_keep_real_and_test_dives_apart(conn, world):
    w = world
    with pytest.raises(errors.CheckViolation, match="dives_test_date_range"), conn.transaction():
        add_dive(w, date(1900, 1, 1), [], is_test=False)
    with pytest.raises(errors.CheckViolation, match="dives_test_date_range"), conn.transaction():
        add_dive(w, date(2030, 1, 1), [], is_test=True)
    add_dive(w, date(1900, 1, 1), [], is_test=True)
    add_dive(w, date(2030, 1, 1), [], is_test=False)
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        add_dive(w, date(2030, 1, 1), [])  # one dive per competition and date
    with pytest.raises(errors.CheckViolation, match="dives_check"), conn.transaction():
        conn.execute("insert into dives (competition_id, sport, dive_date, opens_at, closes_at)"
                     " values (%s, 'football', '2030-02-01', now(), now())", (w.comp["id"],))


def test_dive_prompts_accept_approved_prompts_of_the_dives_competition_only(conn, world):
    w = world
    other = w.entity("competition", "Placeholder Other League")
    dive = add_dive(w, date(2030, 1, 1), [])
    for status in ("candidate", "gate_failed", "gate_passed", "rejected"):
        with pytest.raises(errors.CheckViolation, match="only approved prompts"), conn.transaction():
            conn.execute("insert into dive_prompts (dive_id, position, prompt_id) values (%s, 1, %s)",
                         (dive, add_prompt(w, 10, status=status)))
    foreign = add_prompt(w, 11, competition=other)
    with pytest.raises(errors.CheckViolation, match="another competition or sport"), conn.transaction():
        conn.execute("insert into dive_prompts (dive_id, position, prompt_id) values (%s, 1, %s)", (dive, foreign))
    good = add_prompt(w, 12)
    conn.execute("insert into dive_prompts (dive_id, position, prompt_id) values (%s, 1, %s)", (dive, good))
    # each prompt is rated on exactly one date
    dive2 = add_dive(w, date(2030, 1, 2), [])
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        conn.execute("insert into dive_prompts (dive_id, position, prompt_id) values (%s, 1, %s)", (dive2, good))
    with pytest.raises(errors.UniqueViolation), conn.transaction():  # and each position once
        conn.execute("insert into dive_prompts (dive_id, position, prompt_id) values (%s, 1, %s)",
                     (dive, add_prompt(w, 13)))


def test_a_dive_with_attempts_keeps_its_prompts(conn, play):
    w, dive = play.w, play.dive
    refuses(conn, errors.CheckViolation, "delete from dive_prompts where dive_id = %s", (dive,))
    refuses(conn, errors.CheckViolation, "insert into dive_prompts (dive_id, position, prompt_id) values (%s, 2, %s)",
            (dive, add_prompt(w, 5)))
    refuses(conn, (errors.CheckViolation, errors.ForeignKeyViolation), "delete from dives where id = %s", (dive,))


# ---------------------------------------------------------------------------
# the freeze
# ---------------------------------------------------------------------------

def test_a_prompt_in_a_dive_is_frozen_and_unscheduling_thaws_it(conn, world):
    w = world
    prompt = add_prompt(w, 1, clubs=("A", "B"))
    entities = add_answers(w, prompt)
    free = add_prompt(w, 2, status="gate_passed")
    dive = add_dive(w, date(2030, 1, 1), [prompt])
    for column, value in (("text", "'Another wording'"), ("answer_query", "'select 2'"), ("club_ids", "'{}'"),
                          ("sport", "'basketball'"), ("status", "'rejected'"), ("rarity", "'{}'::jsonb"),
                          ("gate_results", "'{}'::jsonb"), ("competition_id", str(w.clubs["A"]["id"]))):
        refuses(conn, errors.RestrictViolation, f"update prompts set {column} = {value} where id = %s", (prompt,))
    refuses(conn, errors.RestrictViolation, "update prompt_answers set tier = 1, model_tier = 1 where prompt_id = %s",
            (prompt,))
    refuses(conn, errors.RestrictViolation, "delete from prompt_answers where prompt_id = %s", (prompt,))
    refuses(conn, errors.RestrictViolation, "truncate prompt_answers")
    refuses(conn, (errors.RestrictViolation, errors.ForeignKeyViolation), "delete from prompts where id = %s",
            (prompt,))
    # moving a row of another prompt into a frozen one is an update too
    other = add_prompt(w, 3, status="gate_passed")
    add_answers(w, other, count=1)
    refuses(conn, errors.RestrictViolation, "update prompt_answers set prompt_id = %s where prompt_id = %s",
            (prompt, other))
    # what stays possible: the review note, and inserting a missing answer (a data-gap fix)
    conn.execute("update prompts set review_note = 'checked' where id = %s", (prompt,))
    person = w.person("Placeholder Gap")
    conn.execute("insert into prompt_answers (prompt_id, entity_id, evidence_fact_ids) values (%s, %s, '{}')",
                 (prompt, person["id"]))
    # a prompt no dive uses is not frozen
    conn.execute("update prompts set text = 'Free wording' where id = %s", (free,))
    conn.execute("update prompt_answers set flags = '{x}' where prompt_id = %s", (free,))
    # unscheduling lifts the freeze
    conn.execute("delete from dives where id = %s", (dive,))
    conn.execute("update prompts set text = 'Now editable' where id = %s", (prompt,))
    conn.execute("update prompt_answers set flags = '{x}' where prompt_id = %s and entity_id = %s",
                 (prompt, entities[0]))


# ---------------------------------------------------------------------------
# other tables
# ---------------------------------------------------------------------------

def test_review_queue_keeps_one_row_per_prompt_and_input(conn, play):
    prompt = play.prompt
    conn.execute("insert into review_queue (prompt_id, input_norm) values (%s, 'placeholder')", (prompt,))
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        conn.execute("insert into review_queue (prompt_id, input_norm) values (%s, 'placeholder')", (prompt,))
    with pytest.raises(errors.CheckViolation, match="review_queue_resolved_has_resolution"), conn.transaction():
        conn.execute("update review_queue set status = 'resolved' where prompt_id = %s", (prompt,))


def test_rating_tables_follow_the_step_4_contract(conn, play):
    w, user, prompt = play.w, play.user, play.prompt
    sport = ("insert into ratings (user_id, scope_type, sport, entity_id, rating, rd, volatility, games,"
             " engine_version, params_hash) values (%s, 'sport', 'football', %s, 1500, 350, 0.06, 0, 'e', 'h')")
    conn.execute(sport, (user, None))
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        conn.execute(sport, (user, None))  # nulls not distinct: one sport row per user
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(sport, (user, w.comp["id"]))  # a sport scope has no entity
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute("insert into ratings (user_id, scope_type, sport, rating, rd, volatility, games,"
                     " engine_version, params_hash) values (%s, 'club', 'football', 1500, 350, 0.06, 0, 'e', 'h')",
                     (user,))  # a club scope needs one
    conn.execute("insert into prompt_ratings (prompt_id, seed_rating, seed_rd, rating, rd, volatility, games,"
                 " engine_version, params_hash) values (%s, 1700, 60, 1700, 60, 0.06, 0, 'e', 'h')", (prompt,))
    refuses(conn, (errors.RestrictViolation, errors.ForeignKeyViolation), "delete from prompts where id = %s",
            (prompt,))


# ---------------------------------------------------------------------------
# grants
# ---------------------------------------------------------------------------

NEW_TABLES = ["users", "dives", "dive_prompts", "attempts", "answers", "effective_answers", "review_queue",
              "ratings", "rating_history", "prompt_ratings"]


def test_anon_and_authenticated_have_no_access_to_the_new_tables(conn):
    for role in ("anon", "authenticated"):
        for table in NEW_TABLES:
            for privilege in ("select", "insert", "update", "delete", "truncate", "references", "trigger"):
                assert not conn.execute("select has_table_privilege(%s, %s, %s)",
                                        (role, f"public.{table}", privilege)).fetchone()[0], (role, table, privilege)
    rls = dict(conn.execute("select relname, relrowsecurity from pg_class where relnamespace = 'public'::regnamespace"
                            " and relname = any(%s) and relkind = 'r'", (NEW_TABLES,)).fetchall())
    assert rls and all(rls.values()), rls


def as_app(conn, sql, params=()):
    """Runs a select as bk_app, then goes back to the connection's own role. When the select fails, the savepoint
    rollback undoes the role change."""
    with conn.transaction():
        conn.execute("set local role bk_app")
        rows = conn.execute(sql, params).fetchall()
        conn.execute("reset role")
    return rows


def test_bk_app_reads_what_the_app_needs_and_nothing_of_the_facts(conn, play):
    prompt = play.prompt
    # RLS applies to bk_app, so the policy must let it see rows, not only the grant
    assert as_app(conn, "select count(*) from prompts where id = %s", (prompt,)) == [(1,)]
    assert as_app(conn, "select count(*) from prompt_answers where prompt_id = %s", (prompt,))[0][0] == 8
    for table in ("entities", "entity_aliases", "dives", "dive_prompts", "users", "attempts", "answers",
                  "effective_answers", "review_queue", "ratings", "rating_history", "prompt_ratings"):
        as_app(conn, f"select count(*) from {table}")
    for table in ("facts", "entity_pageviews", "club_seasons"):
        with pytest.raises(errors.InsufficientPrivilege):
            as_app(conn, f"select count(*) from {table}")
    with pytest.raises(errors.InsufficientPrivilege):
        as_app(conn, "select * from stints(2000)")


def test_bk_app_can_write_only_what_it_should(conn, play):
    w, user, dive, attempt, prompt = play.w, play.user, play.dive, play.attempt, play.prompt
    entity = w.person("Placeholder Credited")["id"]
    with conn.transaction():
        conn.execute("set local role bk_app")
        answer = conn.execute(
            "insert into answers (attempt_id, position, prompt_id, verdict, points, served_at, answered_at)"
            " values (%s, 1, %s, 'wrong', 0, now(), now()) returning id", (attempt, prompt)).fetchone()[0]
        conn.execute("insert into review_queue (prompt_id, input_norm, first_answer_id) values (%s, 'x', %s)",
                     (prompt, answer))
        conn.execute("update review_queue set occurrences = occurrences + 1, last_seen_at = now() where prompt_id = %s",
                     (prompt,))
        conn.execute("update attempts set current_position = 1 where id = %s", (attempt,))
        conn.execute("insert into prompt_ratings (prompt_id, seed_rating, seed_rd, rating, rd, volatility, games,"
                     " engine_version, params_hash) values (%s, 1700, 60, 1700, 60, 0.06, 0, 'e', 'h')", (prompt,))
        conn.execute("update dives set settled_at = now() where id = %s", (dive,))
        conn.execute("delete from prompt_ratings where prompt_id = %s", (prompt,))
        conn.execute("reset role")
    denied = [
        ("update answers set points = 100 where attempt_id = %s", attempt),
        ("delete from answers where attempt_id = %s", attempt),
        ("truncate answers", None),
        ("update review_queue set status = 'resolved', resolution = 'not_valid' where prompt_id = %s", prompt),
        ("update review_queue set sample_raw = 'x' where prompt_id = %s", prompt),
        ("delete from review_queue where prompt_id = %s", prompt),
        ("update prompts set text = 'x' where id = %s", prompt),
        ("update prompt_answers set tier = 1 where prompt_id = %s", prompt),
        ("delete from prompt_answers where prompt_id = %s", prompt),
        ("insert into prompt_answers (prompt_id, entity_id, evidence_fact_ids) values (%s, %s, '{}')", prompt),
        ("update entities set canonical_name = 'x' where id = %s", entity),
        ("update dives set dive_date = '2031-01-01' where id = %s", dive),
        ("insert into dive_prompts (dive_id, position, prompt_id) values (%s, 2, %s)", dive),
        ("delete from dives where id = %s", dive),
        ("delete from attempts where id = %s", attempt),
        ("delete from users where id = %s", user),
        ("insert into facts (subject_id, predicate, source_ref) values (%s, 'played_for', 'x')", entity),
    ]
    for sql, arg in denied:
        params = {0: (), 1: (arg,), 2: (arg, entity)}[sql.count("%s")]
        with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
            conn.execute("set local role bk_app")
            conn.execute(sql, params)


def test_bk_app_is_a_plain_role(conn):
    # LOGIN is granted outside the migration (`npm run db:app-role` sets the password), so it is not checked here.
    row = conn.execute("select rolsuper, rolbypassrls, rolcreatedb, rolcreaterole"
                       " from pg_roles where rolname = 'bk_app'").fetchone()
    assert row == (False, False, False, False)
