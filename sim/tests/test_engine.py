import math
import random
from datetime import date, timedelta

import pytest

from rating.core import Opponent, idle_rd, rate_period
from rating.engine import replay
from rating.golden import golden_params
from rating.seed import prompt_features, seed_rating
from rating.types import CLUB, COMPETITION, SPORT, Game, Prompt

D0 = date(2030, 1, 1)
P = golden_params()
COMP, SPORT_SCOPE = (COMPETITION, 1), (SPORT, "football")


def day(n):
    return D0 + timedelta(days=n)


def prompts(ids, competition=1, clubs=None):
    return [Prompt(i, competition, (clubs or {}).get(i, ()), {"log_recall_mass": -1.0 - (i % 5) * 0.5}) for i in ids]


def dive(d, user, attempt, ids, points, **flags):
    return [Game(user, day(d), attempt, pid, pts, **flags) for pid, pts in zip(ids, points, strict=True)]


def seed_of(p, params=P):
    return seed_rating(p.features, params.seed)


def row(result, user, d, scope, attempt=1):
    return next(h for h in result.history if (h.user_id, h.date, h.scope_type, h.scope_id, h.attempt)
                == (user, day(d), scope[0], scope[1], attempt))


def test_child_scope_starts_from_the_parents_state_at_the_start_of_the_date():
    ps = prompts(range(1, 15), clubs={i: (50,) for i in range(8, 15)})
    games = dive(0, 1, 1, range(1, 8), [10, 25, 0, 50, 10, 75, 25]) + dive(1, 1, 1, range(8, 15), [25] * 7)
    r = replay(ps, games, P)
    comp, club = row(r, 1, 1, COMP), row(r, 1, 1, (CLUB, 50))
    assert club.rating_before == comp.rating_before  # not the competition's updated rating
    assert comp.rating_after != comp.rating_before
    assert club.rd_before == max(P.child_scope_initial_rd, comp.rd_before)
    assert club.volatility_before == P.initial_volatility
    opps = [Opponent(seed_of(p), P.seed.rd, 0.25) for p in ps[7:]]
    expected = rate_period(club.rating_before, club.rd_before, club.volatility_before, 0, opps, P)
    assert club.rating_after == pytest.approx(expected.rating, rel=1e-12)
    assert r.scopes[(1, (CLUB, 50))].games == 7


def test_child_rd_is_the_larger_of_child_initial_and_parent_rd():
    ps = prompts(range(1, 15), clubs={i: (50,) for i in range(8, 15)})
    # the parent is still at RD 350 + idle on the club's first date: a new player whose first prompts are club prompts
    r = replay(ps, dive(0, 1, 1, range(8, 15), [25] * 7), golden_params(child_scope_initial_rd=100.0))
    club = row(r, 1, 0, (CLUB, 50))
    assert club.rd_before == 350.0  # parent RD 350 beats the child's 100


def test_a_clubs_parent_is_the_competition_of_its_first_prompt_lowest_id_on_ties():
    ps = prompts(range(1, 8), competition=2, clubs={1: (50,)}) + prompts(range(8, 15), competition=1, clubs={8: (50,)})
    games = dive(0, 1, 1, range(1, 8), [25] * 7) + dive(0, 1, 2, range(8, 15), [25] * 7)
    r = replay(ps, games, P)
    club = row(r, 1, 0, (CLUB, 50), attempt=1)
    comp1_start = row(r, 1, 0, (COMPETITION, 1), attempt=2).rating_before
    assert club.rating_before == pytest.approx(comp1_start)
    assert club.rd_before == max(P.child_scope_initial_rd, row(r, 1, 0, (COMPETITION, 1), attempt=2).rd_before)


def test_idle_count_is_dates_skipped_not_dates_since():
    ps = prompts(range(1, 22))
    games = dive(0, 1, 1, range(1, 8), [25] * 7) + dive(1, 1, 1, range(8, 15), [25] * 7) \
        + dive(4, 1, 1, range(15, 22), [25] * 7)
    r = replay(ps, games, P)
    a0, b1, c4 = (row(r, 1, d, COMP) for d in (0, 1, 4))
    assert b1.rd_before == a0.rd_after  # the next date: no idle growth
    assert c4.rd_before == pytest.approx(idle_rd(b1.rd_after, b1.volatility_after, 2, P.initial_rd), rel=1e-12)


def test_prompts_are_frozen_within_a_date_and_settled_after_all_users():
    ps = prompts(range(1, 8))
    games = dive(0, 1, 1, range(1, 8), [100] * 7) + dive(0, 2, 1, range(1, 8), [0] * 7)
    r = replay(ps, games, P)
    for u, result in ((1, 1.0), (2, 0.0)):
        expected = rate_period(1500.0, 350.0, 0.06, 0, [Opponent(seed_of(p), P.seed.rd, result) for p in ps], P)
        assert row(r, u, 0, COMP).rating_after == pytest.approx(expected.rating, rel=1e-12)
    assert all(r.prompts[p.id].rating != seed_of(p) for p in ps)  # moved only at date close
    assert all(r.prompts[p.id].seed_rating == seed_of(p) for p in ps)


def test_prompt_date_close_uses_the_competition_state_and_one_minus_the_result():
    ps = prompts([1])
    # user 1 is established on date 0 (other prompts), then both users play prompt 1 on date 1
    ps += prompts(range(2, 9))
    games = dive(0, 1, 1, range(2, 9), [50] * 7) + dive(1, 1, 1, [1], [75]) + dive(1, 2, 1, [1], [10])
    r = replay(ps, games, P)
    u1_start = row(r, 1, 1, COMP)
    opps = [Opponent(u1_start.rating_before, u1_start.rd_before, 1 - 0.75),
            Opponent(1500.0, 350.0, 1 - 0.10)]  # user 2 is new: initial competition state
    seed = seed_of(ps[0])
    expected = rate_period(seed, P.seed.rd, P.initial_volatility, 0, opps, P)
    assert r.prompts[1].rating == pytest.approx(expected.rating, rel=1e-12)
    assert r.prompts[1].rd == pytest.approx(expected.rd, rel=1e-12)
    assert r.prompts[1].games == 2


def test_the_prompt_lock_happens_at_the_threshold_and_a_locked_prompt_never_updates():
    params = golden_params(prompt_lock_answers=3)
    ps = prompts([1, 2])
    two = replay(ps, dive(0, 1, 1, [1], [50]) + dive(0, 2, 1, [1], [50]), params)
    assert not two.prompts[1].locked and two.prompts[1].games == 2
    games = [g for u in (1, 2, 3) for g in dive(0, u, 1, [1], [50])] + [g for u in (1, 4) for g in dive(1, u, 1, [1], [100])]
    r = replay(ps, games, params)
    p = r.prompts[1]
    assert p.locked and p.games == 3 and p.last_period == day(0)  # the second date did not touch it
    once = replay(ps, [g for g in games if g.date == day(0)], params)
    assert (p.rating, p.rd, p.volatility) == (once.prompts[1].rating, once.prompts[1].rd, once.prompts[1].volatility)


def test_two_attempts_on_one_date_equal_one_batch():
    ps = prompts(range(1, 15), clubs={3: (50,), 10: (50,)})
    games = dive(0, 1, 1, range(1, 8), [25, 50, 0, 75, 10, 15, 100])
    games2 = dive(0, 1, 2, range(8, 15), [10, 0, 25, 50, 100, 25, 10])
    split = replay(ps, games + games2, P)
    batch = replay(ps, [Game(g.user_id, g.date, 1, g.prompt_id, g.points) for g in games + games2], P)
    assert split.scopes == batch.scopes
    last = row(split, 1, 0, SPORT_SCOPE, attempt=2)
    assert last.games == 14
    assert row(split, 1, 0, SPORT_SCOPE, attempt=2).rating_before == row(split, 1, 0, SPORT_SCOPE).rating_after


def test_shuffling_users_and_games_within_a_date_changes_nothing():
    rng = random.Random(3)
    ids = list(range(1, 29))
    ps = prompts(ids, clubs={i: (50 + i % 3,) for i in ids if i % 2})
    games = []
    for d in range(4):
        for user in range(1, 7):
            if rng.random() < 0.8:
                games += dive(d, user, 1, ids[d * 7:(d + 1) * 7], [rng.choice([0, 10, 15, 25, 50, 100]) for _ in range(7)])
    base = replay(ps, games, P)
    for _ in range(10):
        shuffled = games[:]
        rng.shuffle(shuffled)
        assert replay(ps, shuffled, P) == base


def test_archive_plays_are_absent():
    ps = prompts(range(1, 15))
    rated = dive(0, 1, 1, range(1, 8), [25] * 7)
    archive = dive(1, 1, 1, range(8, 15), [100] * 7, rated=False)
    r = replay(ps, rated + archive, P)
    assert r == replay(ps, rated, P)
    assert all(h.date == day(0) for h in r.history) and 8 not in r.prompts


def test_an_abandoned_attempt_counts_seven_games_with_unanswered_prompts_scoring_zero():
    ps = prompts(range(1, 8))
    answered = [50, 50, 50]
    games = dive(0, 1, 1, range(1, 4), answered) + [Game(1, day(0), 1, pid, 100, unanswered=True) for pid in range(4, 8)]
    r = replay(ps, games, P)
    assert r.scopes[(1, COMP)].games == 7 and r.scopes[(1, SPORT_SCOPE)].games == 7
    zeros = replay(ps, dive(0, 1, 1, range(1, 8), [50, 50, 50, 0, 0, 0, 0]), P)
    assert r.scopes == zeros.scopes  # points on an unanswered row are ignored


def test_unknown_prompts_and_duplicate_prompt_ids_raise():
    with pytest.raises(ValueError):
        replay(prompts([1]), dive(0, 1, 1, [2], [10]), P)
    with pytest.raises(ValueError):
        replay(prompts([1]) + prompts([1]), [], P)


def test_replay_is_deterministic_and_covers_every_touched_scope():
    ps = prompts(range(1, 8), clubs={1: (50,), 2: (51,)})
    games = dive(0, 1, 1, range(1, 8), [25] * 7)
    a, b = replay(ps, games, P), replay(ps, games, P)
    assert a == b
    assert set(a.scopes) == {(1, SPORT_SCOPE), (1, COMP), (1, (CLUB, 50)), (1, (CLUB, 51))}
    assert a.scopes[(1, (CLUB, 50))].games == 1


def test_a_clubs_parent_is_per_user_so_other_users_do_not_change_it():
    ps = (prompts(range(1, 8), 2, {1: (31,)}) + prompts(range(8, 15), 1, {8: (31,)})
          + prompts(range(15, 22), 1) + prompts(range(22, 29), 2))
    user1 = dive(0, 1, 1, range(15, 22), [100] * 7) + dive(0, 1, 2, range(22, 29), [0] * 7) \
        + dive(1, 1, 1, range(1, 8), [50] * 7)
    user2 = dive(1, 2, 1, range(8, 15), [25] * 7)
    alone, both = replay(ps, user1, P), replay(ps, user1 + user2, P)
    assert alone.scopes[(1, (CLUB, 31))] == both.scopes[(1, (CLUB, 31))]
    assert [h for h in alone.history if h.user_id == 1] == [h for h in both.history if h.user_id == 1]
    # user 1's club 31 hangs under competition 2 (its own first prompt), user 2's under competition 1
    comp2 = row(both, 1, 1, (COMPETITION, 2))
    assert row(both, 1, 1, (CLUB, 31)).rating_before == comp2.rating_before
    comp1 = row(both, 2, 1, COMP)
    assert row(both, 2, 1, (CLUB, 31)).rating_before == comp1.rating_before


def test_a_club_parent_tie_on_a_date_goes_to_the_lowest_competition_id():
    ps = (prompts(range(1, 8), 2, {1: (31,)}) + prompts(range(8, 15), 1, {8: (31,)}) + prompts(range(15, 22), 1)
          + prompts(range(22, 29), 2))
    games = (dive(0, 1, 1, range(15, 22), [100] * 7) + dive(0, 1, 2, range(22, 29), [0] * 7)  # distinct states
             + dive(1, 1, 1, range(1, 8), [50] * 7) + dive(1, 1, 2, range(8, 15), [50] * 7))
    r = replay(ps, games, P)
    club = row(r, 1, 1, (CLUB, 31), attempt=1)
    comp1, comp2 = row(r, 1, 1, COMP, attempt=2), row(r, 1, 1, (COMPETITION, 2), attempt=1)
    assert comp1.rating_before != comp2.rating_before
    assert club.rating_before == comp1.rating_before


@pytest.mark.parametrize("games, message", [
    (dive(0, 1, 1, [1, 2], [10, 20]) + dive(0, 1, 2, [1], [50]), "duplicate rated game"),
    (dive(0, 1, 1, [1], [101]), "outside 0..100"),
    (dive(0, 1, 1, [1], [-5]), "outside 0..100"),
    (dive(0, 1, 1, [1], [float("nan")]), "finite"),
    ([Game(1, D0, 1, 1, 10.0), Game(1, "2030-01-01", 1, 2, 10)], "datetime.date"),
    ([Game(1, __import__("datetime").datetime(2030, 1, 1), 1, 1, 10)], "datetime.date"),
])
def test_bad_game_rows_are_rejected(games, message):
    with pytest.raises(ValueError, match=message):
        replay(prompts([1, 2]), games, P)


def test_archive_duplicates_and_unanswered_points_are_not_checked():
    games = dive(0, 1, 1, [1], [10]) + dive(0, 1, 1, [1], [10], rated=False) + [Game(1, day(0), 1, 2, 999, unanswered=True)]
    assert replay(prompts([1, 2]), games, P).scopes[(1, COMP)].games == 2


def test_bad_prompts_and_params_are_rejected():
    with pytest.raises(ValueError, match="club twice"):
        replay(prompts([1], clubs={1: (5, 5)}), dive(0, 1, 1, [1], [10]), P)
    with pytest.raises(ValueError, match="finite"):
        replay([Prompt(1, 1, (), {"log_recall_mass": float("inf")})], dive(0, 1, 1, [1], [10]), P)
    with pytest.raises(ValueError, match="params.tau"):
        replay(prompts([1]), dive(0, 1, 1, [1], [10]), golden_params(tau=float("nan")))


def test_prompt_features_take_log_n_eff_and_reject_bad_rows():
    row_ = {"rarity": {"log_recall_mass": 11.25}, "gate_results": {"tiers": {"n_eff": 7.5, "top_share": 0.3}}}
    assert prompt_features(row_["rarity"], row_["gate_results"]) == {"log_recall_mass": 11.25, "ln_n_eff": math.log(7.5)}
    for rarity, gate in [({"log_recall_mass": 1.0}, {"tiers": {"n_eff": 0}}), ({"log_recall_mass": 1.0}, {"tiers": {}}),
                         ({}, {"tiers": {"n_eff": 3}}), ({"log_recall_mass": None}, {"tiers": {"n_eff": 3}}),
                         ({"log_recall_mass": 1.0}, {"tiers": {"n_eff": "7"}}), ({"log_recall_mass": 1.0}, {})]:
        with pytest.raises(ValueError):
            prompt_features(rarity, gate)
    with pytest.raises(ValueError, match="finite"):
        seed_rating({"log_recall_mass": float("nan")}, P.seed)
