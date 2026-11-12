"""One Glicko-2 rating period for one player (Glickman, "Example of the Glicko-2 system"), pure functions.
Ratings are on the Glicko-1 scale in and out. Two additions to the paper:
  - game_weight w: v = 1 / sum(w g^2 E (1-E)), delta = v * sum(w g (s - E)). w = 1 is standard Glicko-2.
  - phi* (the pre-period deviation) and idle growth are capped at initial_rd.
Sums run left to right in a plain loop (never sum() or fsum), in the caller's game order, so the TypeScript
port can match.
"""

import math
from dataclasses import dataclass

from .params import Params

SCALE = 173.7178  # Glicko-2 scale factor (400 / ln 10, as rounded in the paper)
CENTER = 1500.0
MAX_ITERATIONS = 100
_PI2 = math.pi * math.pi


class VolatilityError(RuntimeError):
    """The volatility step did not converge within MAX_ITERATIONS."""


@dataclass(frozen=True)
class Opponent:
    rating: float
    rd: float
    score: float  # this player's result against the opponent, 0..1


@dataclass(frozen=True)
class Rated:
    rating: float
    rd: float
    volatility: float
    iterations: int  # volatility-step iterations (0 when there were no games)
    bracket: str  # "log" or "scan": which way the volatility search bracket was found ("" without games)


def idle_rd(rd: float, volatility: float, periods: int, initial_rd: float) -> float:
    """RD after `periods` rating periods without games: phi^2 grows by volatility^2 each, capped at initial_rd."""
    if periods <= 0:
        return rd
    phi = rd / SCALE
    grown = math.sqrt(phi * phi + periods * volatility * volatility)
    return min(grown, initial_rd / SCALE) * SCALE


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / _PI2)


def _expected(mu: float, mu_j: float, g_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-g_j * (mu - mu_j)))


def new_volatility(phi: float, volatility: float, delta: float, v: float, tau: float,
                   epsilon: float) -> tuple[float, int, str]:
    """Step 5: the Illinois algorithm. Returns (sigma', iterations, bracket)."""
    a = math.log(volatility * volatility)
    phi2, delta2, tau2 = phi * phi, delta * delta, tau * tau

    def f(x: float) -> float:
        ex = math.exp(x)
        d = phi2 + v + ex
        return ex * (delta2 - phi2 - v - ex) / (2.0 * d * d) - (x - a) / tau2

    iterations = 0
    big_a = a
    if delta2 > phi2 + v:
        big_b = math.log(delta2 - phi2 - v)
        bracket = "log"
    else:
        bracket = "scan"
        k = 1
        while f(a - k * tau) < 0.0:
            k += 1
            iterations += 1
            if iterations > MAX_ITERATIONS:
                raise VolatilityError(f"bracketing took more than {MAX_ITERATIONS} steps")
        big_b = a - k * tau
    f_a, f_b = f(big_a), f(big_b)
    while abs(big_b - big_a) > epsilon:
        iterations += 1
        if iterations > MAX_ITERATIONS:
            raise VolatilityError(f"volatility step took more than {MAX_ITERATIONS} iterations")
        big_c = big_a + (big_a - big_b) * f_a / (f_b - f_a)
        f_c = f(big_c)
        if f_c * f_b <= 0.0:  # <= so an exact root (f_c == 0) ends the search
            big_a, f_a = big_b, f_b
        else:
            f_a = f_a / 2.0
        big_b, f_b = big_c, f_c
    return math.exp(big_a / 2.0), iterations, bracket


def rate_period(rating: float, rd: float, volatility: float, idle_periods: int,
                opponents: list[Opponent], params: Params) -> Rated:
    """Idle growth for `idle_periods` empty periods, then one period against `opponents` (in the caller's order).
    Without opponents this is only the idle growth for idle_periods."""
    rd = idle_rd(rd, volatility, idle_periods, params.initial_rd)
    if not opponents:
        return Rated(rating, rd, volatility, 0, "")
    w = params.game_weight
    mu, phi = (rating - CENTER) / SCALE, rd / SCALE
    inv_v = 0.0  # sum of w g^2 E (1-E)
    gain = 0.0  # sum of w g (s - E)
    for o in opponents:
        g = _g(o.rd / SCALE)
        e = _expected(mu, (o.rating - CENTER) / SCALE, g)
        inv_v += w * g * g * e * (1.0 - e)
        gain += w * g * (o.score - e)
    v = 1.0 / inv_v
    delta = v * gain
    sigma, iterations, bracket = new_volatility(phi, volatility, delta, v, params.tau,
                                                params.volatility_epsilon)
    phi_star = min(math.sqrt(phi * phi + sigma * sigma), params.initial_rd / SCALE)
    phi_new = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    mu_new = mu + phi_new * phi_new * gain
    return Rated(SCALE * mu_new + CENTER, SCALE * phi_new, sigma, iterations, bracket)
