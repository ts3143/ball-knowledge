import json

import pytest

from rating import ENGINE_VERSION
from rating.core import Opponent, rate_period
from rating.engine import replay
from rating.golden import GOLDEN_PATH, replay_scenarios, stale_reasons
from rating.params import Params


def test_the_golden_file_is_current():
    assert stale_reasons() == [], "run: uv run python -m rating golden"


def test_the_golden_file_records_the_engine_version_and_every_case_kind():
    doc = json.loads(GOLDEN_PATH.read_text())
    assert doc["engine_version"] == ENGINE_VERSION
    assert len(doc["core"]) >= 50
    assert {c["name"] for c in doc["replay"]} == {
        "multi_scope", "child_scope_start", "idle_gap", "two_competitions_one_date", "low_threshold_lock",
        "abandoned_attempt"}


def test_core_cases_reproduce_from_their_recorded_inputs():
    for c in json.loads(GOLDEN_PATH.read_text())["core"]:
        params = Params.from_dict({**json.loads(json.dumps(replay_scenarios()[0][1].to_dict())), **c["params"]})
        out = rate_period(c["input"]["rating"], c["input"]["rd"], c["input"]["volatility"], c["input"]["idle_periods"],
                          [Opponent(**o) for o in c["input"]["opponents"]], params)
        for key in ("rating", "rd", "volatility"):
            assert getattr(out, key) == pytest.approx(c["expected"][key], rel=1e-6), c["name"]


def test_replay_cases_have_the_features_they_claim():
    scen = {name: (p, pr, g) for name, p, pr, g in replay_scenarios()}
    # low lock: a prompt is locked; abandoned: unanswered rows and an unrated archive play; clubs: a child scope
    p, pr, g = scen["low_threshold_lock"]
    assert replay(pr, g, p).prompts[1].locked
    _, pr, g = scen["abandoned_attempt"]
    assert any(x.unanswered for x in g) and any(not x.rated for x in g)
    p, pr, g = scen["idle_gap"]
    assert (max(x.date for x in g) - min(x.date for x in g)).days == 20
