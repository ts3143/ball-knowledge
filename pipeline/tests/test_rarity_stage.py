"""The rarity stage of prompt generation and `rarity check`, on placeholder rows inside a rolled-back transaction.
Needs the local database."""

from datetime import date

import pytest
from psycopg.types.json import Jsonb

from rarity.check import check
from rarity.stage import RARITY_COLUMNS
from tests.db_world import TUNABLES, World, rarity_tunables

pytestmark = pytest.mark.db

N = 15


def pageviews(w, person, views):
    w.cur.execute("insert into entity_pageviews (entity_id, wiki, month, views) values (%s, 'de', %s, %s)",
                  (person["id"], date(2020, 1, 1), views))


def club_a_citizens(w, views, n=N, weak_last=False):
    """n placeholder players with a spell at club A and the test country's citizenship; views[i] pageviews each.
    The least viewed is weak (no appearance count) when weak_last."""
    people = []
    for i in range(n):
        p = w.person(f"Player {i:02d}")
        w.stint(p, "A", apps=None if weak_last and i == n - 1 else 20)
        w.fact(p["id"], "nationality", w.country["id"])
        pageviews(w, p, views(i))
        people.append(p)
    return people


def latest_pageview_month(w):
    """pageviews_through is the latest month loaded anywhere, and this database holds real pageviews too."""
    return w.cur.execute("select max(month) as m from entity_pageviews").fetchone()["m"].isoformat()


def graded(i):
    return int(3_000_000 * 0.45 ** i) + 1


def prompt_and_answers(w, template="club_nationality"):
    [prompt] = w.prompts(template)
    return prompt, w.answers(prompt["id"])


def snapshot(w):
    """Everything the model stored, by prompt params and entity."""
    rows = w.cur.execute(
        f"select p.template, p.params, p.status, p.rarity, p.gate_results, a.entity_id, {', '.join('a.' + c for c in RARITY_COLUMNS)}"
        " from prompts p left join prompt_answers a on a.prompt_id = p.id where p.competition_id = %s"
        " order by p.id, a.entity_id", (w.comp["id"],)).fetchall()
    return [{k: v for k, v in r.items()} for r in rows]


def test_rarity_is_written_for_candidates(conn):
    w = World(conn)
    people = club_a_citizens(w, graded)
    stats = w.run("club_nationality")["club_nationality"]
    prompt, answers = prompt_and_answers(w)
    assert len(answers) == N and prompt["status"] == "gate_passed" and stats.gate_passed == 1
    r = prompt["rarity"]
    assert r["model_version"] == "rarity-2" and r["reference_season"] == 2013 and r["pageviews_through"] == latest_pageview_month(w)
    assert r["params"]["features"]["open_ended_current_seasons"] == 2
    assert r["params"]["alpha"] == 0.6 and r["params"]["fame_weights"] == {"de": 1.0, "en": 0.4}
    assert r["params"]["weak_answer_max_tier"] == 3 and r["params"]["cumulative_share"][0] == 0.5
    assert prompt["gate_results"]["tiers"]["pass"] and prompt["gate_results"]["tiers"]["n"] == N
    assert sum(a["predicted_share"] for a in answers.values()) == pytest.approx(1.0)
    top, bottom = answers[people[0]["id"]], answers[people[-1]["id"]]
    assert (top["views_de"], top["views_en"], top["tie_amount"]) == (graded(0), 0, 20.0)
    assert top["tier"] == 1 and top["points"] == 10 and top["score"] > bottom["score"]
    assert bottom["tier"] == 5 and bottom["points"] == 100 and not bottom["hipster_pick"]
    assert {a["tier"] for a in answers.values()} == {1, 2, 3, 4, 5}
    assert all(a["tier"] == a["model_tier"] for a in answers.values())


def test_prompts_that_fail_the_size_gate_are_not_rated(conn):
    w = World(conn)
    club_a_citizens(w, graded)
    gates = {**TUNABLES["quality_gates"], "min_answers": 100}
    w.run("club_nationality", quality_gates=gates)
    prompt, answers = prompt_and_answers(w)
    assert prompt["status"] == "gate_failed" and prompt["rarity"] is None
    assert prompt["gate_results"]["tiers"] == "not_run"
    assert all(a["score"] is None and a["tier"] is None and not a["hipster_pick"] for a in answers.values())


def test_the_tier_gate_blocks_a_prompt_that_cannot_spread_over_the_tiers(conn):
    w = World(conn)
    club_a_citizens(w, lambda i: 1000)  # identical players: one block, so one tier
    stats = w.run("club_nationality")["club_nationality"]
    prompt, answers = prompt_and_answers(w)
    gate = prompt["gate_results"]["tiers"]
    assert prompt["gate_results"]["size"]["pass"] and not gate["pass"]
    assert "empty tier: Bandwagon" in gate["reasons"] and prompt["status"] == "gate_failed"
    assert (stats.size_passed, stats.tier_failed, stats.gate_passed) == (1, 1, 0)
    assert len(answers) == N and all(a["score"] is not None for a in answers.values())  # still rated, to see why


def test_a_weak_answer_is_capped_at_real_one_in_tier_and_points_but_not_in_model_tier(conn):
    w = World(conn)
    people = club_a_citizens(w, graded, weak_last=True)
    stats = w.run("club_nationality")["club_nationality"]
    prompt, answers = prompt_and_answers(w)
    weak = answers[people[-1]["id"]]
    assert weak["flags"] == ["weak_stint"] and weak["model_tier"] == 5
    assert (weak["tier"], weak["points"]) == (3, 50)
    assert all(a["tier"] == a["model_tier"] for e, a in answers.items() if e != people[-1]["id"])
    counts = prompt["gate_results"]["tiers"]["tier_counts"]
    assert sum(counts) == N and counts[2] >= 1 and stats.weak_cap_cost == 0  # the gate still passes without it


def test_an_approved_prompts_rarity_does_not_change_when_a_tunable_does(conn):
    w = World(conn)
    club_a_citizens(w, graded)
    w.run("club_nationality")
    prompt, _ = prompt_and_answers(w)
    w.cur.execute("update prompts set status = 'approved' where id = %s", (prompt["id"],))
    before = snapshot(w)
    stats = w.run("club_nationality", rarity=rarity_tunables(w.comp["wikidata_id"], alpha=0.9))["club_nationality"]
    assert stats.approved == 1 and stats.approved_params_differ == 1 and stats.updated == 0
    assert snapshot(w) == before
    # a candidate does follow the tunable
    w.cur.execute("update prompts set status = 'gate_passed' where id = %s", (prompt["id"],))
    stats = w.run("club_nationality", rarity=rarity_tunables(w.comp["wikidata_id"], alpha=0.9))["club_nationality"]
    assert stats.updated == 1 and w.prompts("club_nationality")[0]["rarity"]["params"]["alpha"] == 0.9


def test_a_second_run_changes_nothing(conn):
    w = World(conn)
    club_a_citizens(w, graded)
    first = w.run()
    assert sum(s.inserted for s in first.values()) > 0
    before = snapshot(w)
    second = w.run()
    assert [(s.inserted, s.updated, s.deleted) for s in second.values()] == [(0, 0, 0)] * len(second)
    assert snapshot(w) == before


def test_template_only_runs_do_not_change_values(conn):
    w = World(conn)
    club_a_citizens(w, graded)
    w.run("club_nationality")
    subset = [r for r in snapshot(w) if r["template"] == "club_nationality"]
    w.run()
    assert [r for r in snapshot(w) if r["template"] == "club_nationality"] == subset
    w.run("relegated_clubs")
    assert [r for r in snapshot(w) if r["template"] == "club_nationality"] == subset


def test_check_passes_and_fails_on_a_tampered_row(conn, capsys):
    w = World(conn)
    club_a_citizens(w, graded, weak_last=True)
    w.run()
    assert check(conn, w.comp["id"]) == 0
    assert "all reproduce exactly" in capsys.readouterr().out
    prompt, answers = prompt_and_answers(w)
    victim = next(iter(answers))
    w.cur.execute("update prompt_answers set score = score + 1e-9 where prompt_id = %s and entity_id = %s",
                  (prompt["id"], victim))
    assert check(conn, w.comp["id"]) >= 1
    out = capsys.readouterr().out
    assert f"entity {victim}" in out and "score" in out
    w.cur.execute("update prompt_answers set score = score - 1e-9 where prompt_id = %s and entity_id = %s",
                  (prompt["id"], victim))
    w.cur.execute("update prompt_answers set points = 10 where prompt_id = %s and tier > 1", (prompt["id"],))
    assert check(conn, w.comp["id"]) >= 1


def test_check_lists_other_versions_without_recomputing_and_flags_missing_rarity(conn, capsys):
    w = World(conn)
    club_a_citizens(w, graded)
    w.run("club_nationality")
    prompt, _ = prompt_and_answers(w)
    w.cur.execute("update prompts set rarity = jsonb_set(rarity, '{model_version}', '\"rarity-0\"') where id = %s",
                  (prompt["id"],))
    w.cur.execute("update prompt_answers set score = 0 where prompt_id = %s", (prompt["id"],))
    assert check(conn, w.comp["id"]) == 0
    assert "1 prompts on rarity-0, not recomputed" in capsys.readouterr().out
    w.cur.execute("update prompts set rarity = null where id = %s", (prompt["id"],))
    w.cur.execute("update prompt_answers set score = null, predicted_share = null, model_tier = null, tier = null,"
                  " points = null, views_de = null, views_en = null, tie_amount = null, seasons_since = null"
                  " where prompt_id = %s", (prompt["id"],))
    assert check(conn, w.comp["id"]) == 1
    assert "passes the size gate but has no rarity" in capsys.readouterr().out


def test_a_prompt_that_is_not_rated_has_sql_null_rarity_and_cannot_be_approved(conn):
    w = World(conn)
    club_a_citizens(w, graded)
    gates = {**TUNABLES["quality_gates"], "min_answers": 100}
    w.run("club_nationality", quality_gates=gates)
    prompt, _ = prompt_and_answers(w)
    assert prompt["rarity"] is None  # SQL NULL, not a JSON null that would pass `rarity is not null`
    assert w.cur.execute("select jsonb_typeof(rarity) as t from prompts where id = %s",
                         (prompt["id"],)).fetchone()["t"] is None
    w.run("club_nationality", quality_gates=gates)  # a second run keeps it that way
    assert w.cur.execute("select rarity is null as n from prompts where id = %s", (prompt["id"],)).fetchone()["n"]
    with pytest.raises(Exception, match="prompts_approved_has_rarity"), conn.transaction():
        w.cur.execute("update prompts set status = 'approved' where id = %s", (prompt["id"],))
    with pytest.raises(Exception, match="prompts_rarity_is_object"), conn.transaction():
        w.cur.execute("update prompts set rarity = 'null'::jsonb where id = %s", (prompt["id"],))


def test_check_reports_contradictions_between_the_stored_fields(conn, capsys):
    w = World(conn)
    club_a_citizens(w, graded)
    w.run("club_nationality")
    prompt, _ = prompt_and_answers(w)
    assert check(conn, w.comp["id"]) == 0
    capsys.readouterr()
    w.cur.execute("update prompts set status = 'gate_failed' where id = %s", (prompt["id"],))
    w.cur.execute("update prompts set answer_count = answer_count + 1 where id = %s", (prompt["id"],))
    w.cur.execute("update prompts set rarity = rarity - 'reference_season' where id = %s", (prompt["id"],))
    assert check(conn, w.comp["id"]) == 3
    out = capsys.readouterr().out
    assert "status 'gate_failed' contradicts" in out and "answer_count 16 but 15 answer rows" in out
    assert "has no reference_season" in out
    # a rating on a prompt that failed the size gate is a contradiction too
    w.cur.execute("update prompts set gate_results = jsonb_set(gate_results, '{size,pass}', 'false') where id = %s",
                  (prompt["id"],))
    assert check(conn, w.comp["id"]) >= 1
    assert "fails the size gate but has rarity of type object" in capsys.readouterr().out


def test_the_database_refuses_half_rated_answers_and_uncapped_tiers_and_unrated_approvals(conn):
    w = World(conn)
    club_a_citizens(w, graded)
    w.run("club_nationality")
    prompt, answers = prompt_and_answers(w)
    entity = next(iter(answers))
    for sql in ("update prompt_answers set score = null where prompt_id = %s and entity_id = %s",
                "update prompt_answers set tier = 5, model_tier = 4 where prompt_id = %s and entity_id = %s"):
        with pytest.raises(Exception, match="violates check constraint"), conn.transaction():
            w.cur.execute(sql, (prompt["id"], entity))
    with pytest.raises(Exception, match="prompts_approved_has_rarity"), conn.transaction():
        w.cur.execute("update prompts set status = 'approved', rarity = null where id = %s", (prompt["id"],))


def test_check_accepts_approved_and_rejected_prompts_and_still_recomputes_them(conn, capsys):
    w = World(conn)
    club_a_citizens(w, graded)
    w.run("club_nationality")
    prompt, _ = prompt_and_answers(w)
    for status in ("approved", "rejected", "gate_passed"):
        w.cur.execute("update prompts set status = %s, reviewed_at = now() where id = %s", (status, prompt["id"]))
        assert check(conn, w.comp["id"]) == 0, status
        assert "all reproduce exactly" in capsys.readouterr().out
    # a decision does not excuse a failed gate: this prompt's gate_results say it failed
    w.cur.execute("update prompts set status = 'approved', gate_results = jsonb_set(gate_results, '{tiers,pass}',"
                  " 'false') where id = %s", (prompt["id"],))
    assert check(conn, w.comp["id"]) >= 1
    assert "contradicts its gate_results" in capsys.readouterr().out
    # and tampering with an approved prompt's answers is still caught
    w.cur.execute("update prompts set status = 'approved', gate_results = %s where id = %s",
                  (Jsonb(prompt["gate_results"]), prompt["id"]))
    w.cur.execute("update prompt_answers set points = 10 where prompt_id = %s and tier > 1", (prompt["id"],))
    assert check(conn, w.comp["id"]) >= 1
