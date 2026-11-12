"""The fake world: players, their schedules and the answer model's parameters for one snapshot."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .answers import PromptOutcomes, expected_points, outcome_probs, sigmoid, solve_c0
from .snapshot import Snapshot

WORLD_PATH = Path(__file__).resolve().parents[1] / "config" / "world.toml"
COHORTS = ("daily", "irregular", "newcomer", "lapse")
STREAMS = ("population", "schedule", "order", "answers", "noise", "examples")  # append only: keeps the others
WORLDS = ("model-true", "model-wrong")


def load_world_config(overrides: list[str] | None = None, path: Path = WORLD_PATH) -> dict:
    from rating.params import apply_overrides

    with open(path, "rb") as f:
        cfg = tomllib.load(f)["world"]
    return apply_overrides({"world": cfg}, overrides or [])["world"]


def streams(seed: int) -> dict[str, np.random.Generator]:
    """Independent random streams, so changing e.g. the answers' draws never changes the population."""
    return {name: np.random.default_rng(s) for name, s in zip(STREAMS, np.random.SeedSequence(seed).spawn(len(STREAMS)))}


@dataclass(frozen=True)
class World:
    cfg: dict
    n_dates: int
    theta: np.ndarray  # (m,) knowledge at the first date
    drift: np.ndarray  # (m,) change in theta over the season (0 for most)
    lam: np.ndarray  # (m,) strategy
    wrong: np.ndarray  # (m,) wrong-answer rate
    affinity: np.ndarray  # (m, P) bonus from favourite clubs on each prompt
    favourites: list[tuple[int, ...]]  # (m,) club ids
    cohort: np.ndarray  # (m,) index into COHORTS
    plays: np.ndarray  # (m, T) bool
    used: np.ndarray  # (U,) prompt indices that are played, in play order
    prompt_date: np.ndarray  # (P,) date index of each prompt, -1 if never played
    prompt_scores: list[np.ndarray]  # (P,) scores the players' knowledge is based on (perturbed in model-wrong)
    c0: float

    @property
    def n_players(self) -> int:
        return len(self.theta)

    def theta_at(self, date_index: int) -> np.ndarray:
        frac = date_index / max(self.n_dates - 1, 1)
        return self.theta + self.drift * frac

    def theta_eff(self, j: int, date_index: int) -> np.ndarray:
        return self.theta_at(date_index) + self.affinity[:, j]


def build_world(snapshot: Snapshot, cfg: dict, prompts_per_dive: int, seed: int, world_name: str) -> World:
    if world_name not in WORLDS:
        raise ValueError(f"world must be one of {WORLDS}")
    rng = streams(seed)
    m, T, P = int(cfg["players"]), int(cfg["dates"]), snapshot.n_prompts
    if T * prompts_per_dive > P:
        raise ValueError(f"{T} dates x {prompts_per_dive} prompts need more than the snapshot's {P} prompts")
    pop = rng["population"]
    theta = pop.normal(0.0, cfg["theta_sd"], m)
    z = theta / cfg["theta_sd"]
    lam = sigmoid(cfg["lambda_expert_slope"] * z + pop.normal(0.0, cfg["lambda_noise_sd"], m))
    wrong = cfg["wrong_base"] + cfg["wrong_scale"] * lam * (1.0 - sigmoid(z))
    drifting = pop.random(m) < cfg["drift_share"]
    drift = np.where(drifting, pop.normal(0.0, cfg["drift_sd"], m), 0.0)

    clubs = sorted({c for cs in snapshot.club_ids for c in cs})
    weight = np.array([sum(c in cs for cs in snapshot.club_ids) for c in clubs], dtype=float)
    n_fav = pop.choice(len(cfg["favourites_share"]), size=m, p=np.asarray(cfg["favourites_share"]) / sum(cfg["favourites_share"]))
    favourites, bonus = [], []
    for i in range(m):
        n = min(int(n_fav[i]), len(clubs))
        picked = pop.choice(len(clubs), size=n, replace=False, p=weight / weight.sum()) if n else np.array([], int)
        favourites.append(tuple(sorted(clubs[c] for c in picked)))
        bonus.append(pop.uniform(cfg["affinity_min"], cfg["affinity_max"], n))
    affinity = np.zeros((m, P))
    prompts_of_club = {c: [j for j, cs in enumerate(snapshot.club_ids) if c in cs] for c in clubs}
    for i in range(m):
        for c, b in zip(favourites[i], bonus[i]):
            idx = prompts_of_club[c]
            affinity[i, idx] = np.maximum(affinity[i, idx], b)

    cohort, plays = build_schedules(cfg, m, T, rng["schedule"])
    order = rng["order"].permutation(P)[: T * prompts_per_dive]
    prompt_date = np.full(P, -1, dtype=np.int64)
    prompt_date[order] = np.arange(len(order)) // prompts_per_dive

    scores = [snapshot.score[snapshot.answers(j)].copy() for j in range(P)]
    if world_name == "model-wrong":
        noise = rng["noise"]
        templates = sorted(set(snapshot.template))
        template_noise = dict(zip(templates, noise.normal(0.0, cfg["model_wrong"]["template_noise_sd"], len(templates))))
        scores = [s + noise.normal(0.0, cfg["model_wrong"]["answer_noise_sd"], len(s)) + template_noise[snapshot.template[j]]
                  for j, s in enumerate(scores)]
    median_wrong = cfg["wrong_base"] + cfg["wrong_scale"] * 0.5 * 0.5  # lambda 1/2, theta 0
    c0 = solve_c0(scores, cfg["median_valid_rate"], median_wrong)
    return World(cfg, T, theta, drift, lam, wrong, affinity, favourites, cohort, plays, order, prompt_date, scores, c0)


def build_schedules(cfg: dict, m: int, T: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    shares = np.array([cfg["cohort_shares"][c] for c in COHORTS], dtype=float)
    counts = np.floor(shares / shares.sum() * m).astype(int)
    counts[0] += m - counts.sum()  # rounding remainder to daily
    cohort = rng.permutation(np.repeat(np.arange(len(COHORTS)), counts))
    plays = np.zeros((m, T), dtype=bool)
    for i in range(m):
        c = COHORTS[cohort[i]]
        if c == "daily":
            plays[i] = True
        elif c == "irregular":
            plays[i] = rng.random(T) < rng.uniform(cfg["irregular_p_min"], cfg["irregular_p_max"])
        elif c == "newcomer":
            join = int(rng.integers(1, max(2, T - int(cfg["newcomer_min_dates_left"]) + 1)))
            plays[i, join:] = rng.random(T - join) < cfg["newcomer_play_probability"]
        else:
            gap = int(rng.integers(cfg["lapse_gap_min"], cfg["lapse_gap_max"] + 1))
            start = int(rng.integers(5, max(6, T - gap - 4)))
            plays[i] = True
            plays[i, start:start + gap] = False
    return cohort, plays


def prompt_outcomes(world: World, snapshot: Snapshot, j: int, players: np.ndarray, date_index: int) -> PromptOutcomes:
    """Outcome probabilities of `players` (indices) on prompt j as they are on `date_index`."""
    theta_eff = world.theta_at(date_index)[players] + world.affinity[players, j]
    return outcome_probs(theta_eff, world.lam[players], world.wrong[players], world.prompt_scores[j], world.c0)


@dataclass(frozen=True)
class ExpectedMatrices:
    """Closed-form results of every player on every used prompt, each at the date its prompt is played."""
    result: np.ndarray  # (m, U) expected points / points_scale
    valid: np.ndarray  # (m, U)
    blank: np.ndarray  # (m, U)
    wrong: np.ndarray  # (m, U)
    points: np.ndarray  # (m, U) expected points
    tier_mass: np.ndarray  # (m, U, 5) probability of a valid answer of each tier


def expected_matrices(world: World, snapshot: Snapshot, points_scale: float) -> ExpectedMatrices:
    m, U = world.n_players, len(world.used)
    everyone = np.arange(m)
    result, valid, blank, wrong, pts = (np.zeros((m, U)) for _ in range(5))
    tier_mass = np.zeros((m, U, 5))
    for u, j in enumerate(world.used):
        sl = snapshot.answers(j)
        o = prompt_outcomes(world, snapshot, int(j), everyone, int(world.prompt_date[j]))
        pts[:, u] = expected_points(o, snapshot.points[sl])
        result[:, u] = pts[:, u] / points_scale
        valid[:, u], blank[:, u], wrong[:, u] = o.p_valid, o.p_blank, o.p_wrong
        onehot = np.eye(5)[snapshot.tier[sl] - 1]
        tier_mass[:, u, :] = o.p_valid[:, None] * (o.q @ onehot)
    return ExpectedMatrices(result, valid, blank, wrong, pts, tier_mass)
