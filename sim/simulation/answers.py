"""The answer model, in closed form per (player, prompt), and a vectorised sampler of it.

For a player with effective knowledge theta_eff and strategy lambda, on a prompt with answers a (stored scores s_a):
    knows a:   k_a = sigmoid(theta_eff + s_a - c0)
    nothing:   B = prod(1 - k_a)
    wrong:     w (a property of the player), taken first: P(wrong) = w
    blank:     (1 - w) * B
    valid:     (1 - w) * (1 - B), the answer chosen with q_a proportional to k_a * exp((1 - 2 lambda) s_a)
lambda 0 picks as the predicted shares do (softmax of the scores), lambda 1 favours the rarest known answer.
A blank or wrong answer scores 0 points. Valid answers score the stored points of their tier.
"""

from dataclasses import dataclass

import numpy as np

# outcome codes in sampled games
VALID, BLANK, WRONG = 0, 1, 2


def sigmoid(x):
    return 0.5 * (1.0 + np.tanh(0.5 * x))


@dataclass(frozen=True)
class PromptOutcomes:
    """Closed-form outcome probabilities of m players on one prompt with n answers."""
    p_valid: np.ndarray  # (m,)
    p_blank: np.ndarray  # (m,)
    p_wrong: np.ndarray  # (m,)
    q: np.ndarray  # (m, n): which valid answer, rows sum to 1


def outcome_probs(theta_eff: np.ndarray, lam: np.ndarray, wrong: np.ndarray, scores: np.ndarray,
                  c0: float) -> PromptOutcomes:
    z = theta_eff[:, None] + scores[None, :] - c0
    log_blank = -np.logaddexp(0.0, z).sum(axis=1)  # sum of log(1 - k_a)
    blank = np.exp(log_blank)
    log_q = -np.logaddexp(0.0, -z) + (1.0 - 2.0 * lam)[:, None] * scores[None, :]  # log k_a + (1 - 2 lambda) s_a
    log_q -= log_q.max(axis=1, keepdims=True)
    q = np.exp(log_q)
    q /= q.sum(axis=1, keepdims=True)
    return PromptOutcomes(p_valid=(1.0 - wrong) * -np.expm1(log_blank), p_blank=(1.0 - wrong) * blank,
                          p_wrong=np.asarray(wrong, dtype=float).copy(), q=q)


def expected_points(o: PromptOutcomes, points: np.ndarray) -> np.ndarray:
    return o.p_valid * (o.q @ points)


def valid_rate_of_median_player(c0: float, prompt_scores: list[np.ndarray], wrong: float) -> float:
    """Mean over prompts of the valid-answer probability of a player with theta_eff 0."""
    total = 0.0
    for s in prompt_scores:
        total += (1.0 - wrong) * -np.expm1(-np.logaddexp(0.0, s - c0).sum())
    return total / len(prompt_scores)


def solve_c0(prompt_scores: list[np.ndarray], target: float, wrong: float, tol: float = 1e-10) -> float:
    """c0 such that the median player (theta 0, no favourite, wrong-answer rate `wrong`) answers validly on `target`
    of the prompts. The valid rate falls as c0 rises, so bisection."""
    lo, hi = -50.0, 80.0
    if not (valid_rate_of_median_player(hi, prompt_scores, wrong) < target
            < valid_rate_of_median_player(lo, prompt_scores, wrong)):
        raise ValueError(f"no c0 in [{lo}, {hi}] gives a median valid rate of {target} (wrong rate {wrong})")
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if valid_rate_of_median_player(mid, prompt_scores, wrong) > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


@dataclass(frozen=True)
class Sampled:
    outcome: np.ndarray  # (m,) VALID, BLANK or WRONG
    answer: np.ndarray  # (m,) index into the prompt's answers, -1 unless VALID
    points: np.ndarray  # (m,)


def sample_prompt(o: PromptOutcomes, points: np.ndarray, rng: np.random.Generator) -> Sampled:
    """One draw per player: the outcome, then (when valid) the answer. Two uniforms per player, in player order."""
    m = len(o.p_valid)
    u_outcome, u_answer = rng.random(m), rng.random(m)
    outcome = np.where(u_outcome < o.p_wrong, WRONG,
                       np.where(u_outcome < o.p_wrong + o.p_blank, BLANK, VALID))
    cdf = np.cumsum(o.q, axis=1)
    cdf[:, -1] = 1.0
    answer = (cdf < u_answer[:, None]).sum(axis=1)
    answer = np.where(outcome == VALID, answer, -1)
    pts = np.where(outcome == VALID, points[np.maximum(answer, 0)], 0.0)
    return Sampled(outcome, answer, pts)
