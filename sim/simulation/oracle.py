"""The oracle ("true BK"): the Glicko-style ratings that best explain the closed-form expected results.

Fit E_ij = sigmoid(u_i - v_j) to the full matrix of expected results by cross-entropy, alternating Newton steps on
the players' u and the prompts' v. Ratings are r = 1500 + 173.7178 u, with the median player pinned at 1500. A
player's true BK at a time solves mean_j sigmoid(u - v_j) = mean_j E_true(i, t, j); a club's does the same over
that club's prompts.
"""

from dataclasses import dataclass

import numpy as np

from rating.core import CENTER, SCALE

from .answers import sigmoid


@dataclass(frozen=True)
class Oracle:
    u: np.ndarray  # (m,) player ratings, logit scale, median 0
    v: np.ndarray  # (U,) prompt ratings, logit scale, same origin
    rmse: float  # sqrt(mean (E - fitted)^2), in result units
    iterations: int

    @property
    def player_rating(self) -> np.ndarray:
        return CENTER + SCALE * self.u

    @property
    def prompt_rating(self) -> np.ndarray:
        return CENTER + SCALE * self.v


def fit_oracle(e: np.ndarray, max_iterations: int = 500, tol: float = 1e-9, max_step: float = 2.0) -> Oracle:
    """Cross-entropy fit by alternating damped Newton steps. e: (m, U), values in [0, 1]."""
    m, n = e.shape
    u, v = np.zeros(m), np.zeros(n)
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        p = sigmoid(u[:, None] - v[None, :])
        step_u = np.clip((e - p).sum(axis=1) / (p * (1 - p)).sum(axis=1), -max_step, max_step)
        u = u + step_u
        p = sigmoid(u[:, None] - v[None, :])
        step_v = np.clip(-(e - p).sum(axis=0) / (p * (1 - p)).sum(axis=0), -max_step, max_step)
        v = v + step_v
        if max(np.abs(step_u).max(), np.abs(step_v).max()) < tol:
            break
    shift = np.median(u)
    u, v = u - shift, v - shift
    rmse = float(np.sqrt(np.mean((e - sigmoid(u[:, None] - v[None, :])) ** 2)))
    return Oracle(u, v, rmse, iterations)


def solve_true_bk(v: np.ndarray, target: np.ndarray, iterations: int = 60) -> np.ndarray:
    """u per player with mean_j sigmoid(u - v_j) = target (bisection; the mean rises with u). target: (m,)."""
    lo, hi = np.full(len(target), -40.0), np.full(len(target), 40.0)
    for _ in range(iterations):
        mid = 0.5 * (lo + hi)
        mean = sigmoid(mid[:, None] - v[None, :]).mean(axis=1)
        above = mean > target
        hi = np.where(above, mid, hi)
        lo = np.where(above, lo, mid)
    return 0.5 * (lo + hi)


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rank correlation, ties get their average rank."""
    def ranks(x):
        order = np.argsort(x, kind="mergesort")
        r = np.empty(len(x))
        r[order] = np.arange(len(x), dtype=float)
        for value in np.unique(x[np.diff(np.sort(x), prepend=np.nan) == 0]):
            tied = x == value
            r[tied] = r[tied].mean()
        return r

    return float(np.corrcoef(ranks(np.asarray(a)), ranks(np.asarray(b)))[0, 1])
