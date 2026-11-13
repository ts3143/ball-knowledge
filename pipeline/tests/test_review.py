"""T's approval of prompts: the decisions, the rules about who can be approved, and the interactive loop driven
by injected input and output. Placeholder prompts inside a rolled-back transaction; needs the local database."""

import io
import tomllib
from datetime import date

import pytest

from ingest.config import REPO_ROOT
from prompts.difficulty import tercile_bands
from prompts.review import (
    Decision,
    ReviewError,
    decide,
    decide_many,
    load_prompt,
    print_counts,
    queue,
    render,
    review_loop,
    seed_pool,
)
from tests.db_dives import add_answers, add_dive, add_prompt

pytestmark = pytest.mark.db

TUNABLES = tomllib.loads((REPO_ROOT / "tunables.toml").read_text())
TIER_NAMES = TUNABLES["tiers"]["names"]


def status_of(w, prompt_id):
    return w.cur.execute("select status, reviewed_at, review_note, text from prompts where id = %s",
                         (prompt_id,)).fetchone()


def script(*lines):
    """An input function that returns the lines in turn and raises EOFError when they run out."""
    it = iter(lines)

    def read(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None
    return read


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------

def test_approve_marks_the_prompt_with_a_time_and_an_optional_note(conn, world):
    w = world
    a, b = add_prompt(w, 1, status="gate_passed"), add_prompt(w, 2, status="gate_passed")
    decide_many(conn, [a, b, a], "approved", "reads well")  # a repeated id is applied once
    for pid in (a, b):
        row = status_of(w, pid)
        assert row["status"] == "approved" and row["reviewed_at"] is not None and row["review_note"] == "reads well"


def test_reject_needs_a_note(conn, world):
    w = world
    a = add_prompt(w, 1, status="gate_passed")
    with pytest.raises(ReviewError, match="needs a --note"):
        decide_many(conn, [a], "rejected", None)
    with pytest.raises(ReviewError, match="needs a --note"):
        decide_many(conn, [a], "rejected", "   ")
    assert status_of(w, a)["status"] == "gate_passed"
    decide_many(conn, [a], "rejected", "two answers are the same person")
    row = status_of(w, a)
    assert (row["status"], row["review_note"]) == ("rejected", "two answers are the same person")
    assert row["reviewed_at"] is not None


@pytest.mark.parametrize("status", ["candidate", "gate_failed", "rejected", "approved"])
def test_only_gate_passed_prompts_can_be_approved(conn, world, status):
    w = world
    pid = add_prompt(w, 1, status=status)
    with pytest.raises(ReviewError, match=f"prompt {pid} is {status}: only gate_passed prompts can be approved"):
        decide_many(conn, [pid], "approved", None)
    assert status_of(w, pid)["status"] == status


def test_one_bad_id_changes_nothing(conn, world):
    w = world
    good, bad = add_prompt(w, 1, status="gate_passed"), add_prompt(w, 2, status="gate_failed")
    with pytest.raises(ReviewError) as e:
        decide_many(conn, [good, bad, 987654321], "approved", None)
    assert f"prompt {bad} is gate_failed" in str(e.value) and "prompt 987654321 does not exist" in str(e.value)
    assert status_of(w, good)["status"] == "gate_passed" and status_of(w, good)["reviewed_at"] is None


def test_an_approved_prompt_can_be_rejected_until_a_dive_uses_it(conn, world):
    w = world
    free, used = add_prompt(w, 1, status="gate_passed"), add_prompt(w, 2, status="approved")
    decide_many(conn, [free], "approved", None)
    decide_many(conn, [free], "rejected", "approved by mistake")
    assert status_of(w, free)["status"] == "rejected"
    add_dive(w, date(2030, 1, 1), [used])
    with pytest.raises(ReviewError, match=f"prompt {used} is in a dive"):
        decide_many(conn, [used], "rejected", "too late")
    assert status_of(w, used)["status"] == "approved"


def test_editing_the_wording_keeps_the_old_text_in_the_note(conn, world):
    w = world
    pid = add_prompt(w, 1, status="gate_passed", text="Name a player of Placeholder FC")
    decide(conn, [Decision(pid, "approved", text="Name any player of Placeholder FC")])
    row = status_of(w, pid)
    assert row["status"] == "approved" and row["text"] == "Name any player of Placeholder FC"
    assert row["review_note"] == "wording edited; was: Name a player of Placeholder FC"


# ---------------------------------------------------------------------------
# what T sees
# ---------------------------------------------------------------------------

def test_the_queue_lets_templates_take_turns_and_only_lists_gate_passed(conn, world):
    w = world
    a = [add_prompt(w, i, status="gate_passed", template="t_a") for i in (1, 2, 3)]
    b = [add_prompt(w, i, status="gate_passed", template="t_b") for i in (4, 5)]
    add_prompt(w, 6, status="approved", template="t_a")
    add_prompt(w, 7, status="gate_failed", template="t_b")
    other = w.entity("competition", "Placeholder Other League")
    add_prompt(w, 8, status="gate_passed", template="t_a", competition=other)
    assert queue(w.cur, w.comp["id"]) == [a[0], b[0], a[1], b[1], a[2]]
    assert queue(w.cur, w.comp["id"], limit=3) == [a[0], b[0], a[1]]
    assert queue(w.cur, w.comp["id"], ["t_b"]) == b


def test_the_screen_shows_what_t_needs_and_fits_a_terminal(conn, world):
    w = world
    pid = add_prompt(w, 1, status="gate_passed", clubs=("A", "B"), text="Name a player of Placeholder FC")
    add_answers(w, pid, count=12, weak=3)
    for i in range(2, 8):
        add_prompt(w, i, status="gate_passed")
    seeds = seed_pool(w.cur, TUNABLES, w.comp["id"])
    prompt = load_prompt(w.cur, pid, seeds, tercile_bands(seeds))
    screen = render(prompt, 1, 7, TIER_NAMES)

    assert "Name a player of Placeholder FC" in screen and "clubs: Test Club A, Test Club B" in screen
    assert "answers: 12" in screen and "weak answers: 3 of 12" in screen
    assert "tiers:   Bandwagon 4  Casual 4" in screen  # the counts stored in gate_results
    assert f"seed difficulty: {prompt.seed:.0f} (higher = harder; easy third of 7 prompts)" in screen
    assert "most likely answers (top 5" in screen and "least likely answers (bottom 5)" in screen
    assert "Placeholder Person" in screen and " 10 pts" in screen and "100 pts" in screen
    assert screen.count("weak") == 3 + 1  # three weak answers marked, plus the summary line
    assert max(len(line) for line in screen.splitlines()) <= 78
    top = [ln for ln in screen.splitlines() if ln.startswith("    1.")]
    assert len(top) == 1 and "50.0%" in top[0]


def test_a_small_prompt_shows_no_overlapping_bottom_list(conn, world):
    w = world
    pid = add_prompt(w, 1, status="gate_passed")
    add_answers(w, pid, count=7)
    seeds = seed_pool(w.cur, TUNABLES, w.comp["id"])
    screen = render(load_prompt(w.cur, pid, seeds, tercile_bands(seeds)), 1, 1, TIER_NAMES)
    assert "least likely answers (bottom 2)" in screen
    assert [ln.split(".")[0].strip() for ln in screen.splitlines() if ln.startswith("  ") and ". " in ln] == [
        "1", "2", "3", "4", "5", "6", "7"]


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

def run_loop(conn, w, *lines, **kwargs):
    out = io.StringIO()
    counts = review_loop(conn, TUNABLES, w.comp["id"], input_fn=script(*lines), out=out, **kwargs)
    return counts, out.getvalue()


def test_the_loop_applies_each_choice(conn, world):
    w = world
    ids = [add_prompt(w, i, status="gate_passed", text=f"Placeholder prompt {i}") for i in range(1, 6)]
    for pid in ids:
        add_answers(w, pid, count=6)
    counts, out = run_loop(conn, w,
                           "a",                      # 1 approve
                           "r", "too vague",         # 2 reject with a note
                           "e", "Better wording",    # 3 edit and approve
                           "s",                      # 4 skip
                           "q")                      # quit at 5
    assert counts == {"approved": 2, "rejected": 1, "edited": 1, "skipped": 1, "unseen": 1}
    states = [status_of(w, pid) for pid in ids]
    assert [s["status"] for s in states] == ["approved", "rejected", "approved", "gate_passed", "gate_passed"]
    assert states[1]["review_note"] == "too vague"
    assert states[2]["text"] == "Better wording" and "was: Placeholder prompt 3" in states[2]["review_note"]
    assert states[3]["reviewed_at"] is None
    assert "5 prompts to review" in out and "[3 of 5]" in out and "current: Placeholder prompt 3" in out
    buffer = io.StringIO()
    print_counts(counts, buffer)
    assert buffer.getvalue() == "approved 2 (of which edited 1), rejected 1, skipped 1, not reached 1\n"


def test_empty_answers_to_a_follow_up_go_back_to_the_menu_and_bad_input_asks_again(conn, world):
    w = world
    pid = add_prompt(w, 1, status="gate_passed")
    add_answers(w, pid)
    counts, out = run_loop(conn, w, "x", "r", "", "e", "", "R", "needs a note", "")
    assert status_of(w, pid)["status"] == "rejected"  # "R" (any case) after two cancelled follow-ups
    assert counts["rejected"] == 1 and "Please answer a, r, e, s or q." in out


def test_the_end_of_input_quits_cleanly_and_each_decision_was_already_saved(conn, world):
    w = world
    ids = [add_prompt(w, i, status="gate_passed") for i in (1, 2)]
    for pid in ids:
        add_answers(w, pid)
    counts, _ = run_loop(conn, w, "a")  # then EOF, as with Ctrl-D
    assert (counts["approved"], counts["unseen"]) == (1, 1)
    assert [status_of(w, pid)["status"] for pid in ids] == ["approved", "gate_passed"]


def test_nothing_to_review(conn, world):
    counts, out = run_loop(conn, world)
    assert sum(counts.values()) == 0 and "No gate_passed prompts waiting for review." in out


def test_limit_and_template_narrow_the_loop(conn, world):
    w = world
    for i in range(1, 5):
        add_prompt(w, i, status="gate_passed", template="t_a" if i % 2 else "t_b")
    counts, out = run_loop(conn, w, "s", "s", "s", limit=2)
    assert counts["skipped"] == 2 and "2 prompts to review" in out
    counts, out = run_loop(conn, w, "s", "s", "s", template_ids=["t_b"])
    assert counts["skipped"] == 2 and "2 prompts to review" in out
