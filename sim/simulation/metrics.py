"""The metrics of one simulated run (design section 4), as a flat dict of floats: name -> value (nan when undefined).
Names: est./e2e./base./validt.<cohort>.<date> are rank correlations (estimation: BK against true BK; end to end:
BK against theta; base: mean points so far against true BK; validt: true BK against theta), dates are 1-based and
zero padded to three digits.
"""

import itertools

import numpy as np

from rating.core import SCALE, Opponent, rate_period
from rating.params import Params

from .oracle import spearman
from .play import GameArrays, Replayed
from .world import COHORTS, World

COHORT_NAMES = ("all", *COHORTS)
REPORT_DATES = (7, 14, 28, 61)
MIN_COHORT = 5  # fewer players than this: no rank correlation
RD_BUCKETS = (0.0, 50.0, 80.0, 110.0, 150.0, 250.0, 351.0)
PLACE_BAND = 10.0  # percentile points
PLACE_STAY = 5  # dives
PLACE_MIN_DIVES = 15  # players with fewer dives cannot show a placement at 10 and then stay for 5
NOT_PLACED = 999.0
CLUB_STEPS = (5, 10, 15, 20, 30)
# What the sweep reports besides the pass bars (config/world.toml [sweep] overrides these through Task.analysis).
DEFAULT_ANALYSIS = {"uncertain_rd_thresholds": [110], "uncertain_after_dives": [1, 3, 7, 14], "club_min_prompts": [20],
                    "lock_games": []}
MIN_CLUB_PLAYERS = 8
INFLATION_START = 31  # 1-based date; the slope is taken from here to the last date


def percentile_matrix(values: np.ndarray) -> np.ndarray:
    """Percentile (0 to 100) of each entry among the non-nan entries of its column (date)."""
    out = np.full(values.shape, np.nan)
    for t in range(values.shape[1]):
        ok = ~np.isnan(values[:, t])
        n = int(ok.sum())
        if n > 1:
            ranks = np.argsort(np.argsort(values[ok, t], kind="mergesort"), kind="mergesort")
            out[ok, t] = ranks / (n - 1) * 100.0
    return out


def corr(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float:
    ok = mask & ~np.isnan(a) & ~np.isnan(b)
    return spearman(a[ok], b[ok]) if ok.sum() >= MIN_COHORT else float("nan")


def baseline_matrix(played: np.ndarray, games: GameArrays, n_dates: int) -> np.ndarray:
    """Mean points per prompt over the player's dives so far, nan before the first dive."""
    m = played.shape[0]
    points, count = np.zeros((m, n_dates)), np.zeros((m, n_dates))
    np.add.at(points, (games.player, games.date), games.points)
    np.add.at(count, (games.player, games.date), 1.0)
    cum_p, cum_n = np.cumsum(points, axis=1), np.cumsum(count, axis=1)
    return np.where(cum_n > 0, cum_p / np.maximum(cum_n, 1.0), np.nan)


def seed_stats(prefix: str, rating: np.ndarray, truth: np.ndarray, templates: list[str], out: dict) -> None:
    err = rating - truth
    out[f"{prefix}.spearman"] = spearman(rating, truth)
    out[f"{prefix}.rmse"] = float(np.sqrt(np.mean(err**2)))
    out[f"{prefix}.bias"] = float(err.mean())
    out[f"{prefix}.rmse_centered"] = float(err.std())
    for t in sorted(set(templates)):
        sel = np.array([x == t for x in templates])
        out[f"{prefix}.template.{t}.n"] = float(sel.sum())
        out[f"{prefix}.template.{t}.bias"] = float(err[sel].mean())
        out[f"{prefix}.template.{t}.rmse"] = float(np.sqrt(np.mean(err[sel] ** 2)))


def e_pred_deciles(world: World, snapshot, games: GameArrays, rep: Replayed, used_pos: dict[int, int], out: dict) -> None:
    """The engine's predicted result for every game (player's rating at the start of the dive against the prompt's
    seed rating, with the prompt's g(RD)) against the result that happened, by decile of the prediction."""
    r_player = rep.rating_before[games.player, games.date]
    pos = np.array([used_pos[int(j)] for j in games.prompt])
    mu_j, phi_j = (rep.prompt_seed[pos] - 1500.0) / SCALE, rep.prompt_rd_seed[pos] / SCALE
    g = 1.0 / np.sqrt(1.0 + 3.0 * phi_j**2 / np.pi**2)
    e = 1.0 / (1.0 + np.exp(-g * ((r_player - 1500.0) / SCALE - mu_j)))
    actual = games.points / 100.0
    order = np.argsort(e, kind="mergesort")
    worst = 0.0
    for k, chunk in enumerate(np.array_split(order, 10), 1):
        out[f"calib.d{k:02d}.pred"] = float(e[chunk].mean())
        out[f"calib.d{k:02d}.actual"] = float(actual[chunk].mean())
        worst = max(worst, abs(e[chunk].mean() - actual[chunk].mean()))
    out["calib.max_gap"] = float(worst)
    out["calib.mean_gap"] = float(e.mean() - actual.mean())


def run_metrics(world: World, snapshot, true_prompt: np.ndarray, true_bk: np.ndarray, bk_dates: list[int], validity: float,
                fit_rmse: float, club_true: np.ndarray, clubs: list[int], games: GameArrays, rep: Replayed,
                tunables: dict, full: bool, analysis: dict | None = None) -> dict:
    """true_bk: (m, len(bk_dates)) true BK on the 0-based dates `bk_dates`. full=False skips the metrics only the
    population scenario needs."""
    m, T = world.n_players, world.n_dates
    analysis = {**DEFAULT_ANALYSIS, **(analysis or {})}
    out: dict[str, float] = {"validity.oracle": validity, "oracle.rmse": fit_rmse}
    theta_t = np.stack([world.theta_at(t) for t in range(T)], axis=1)
    true_full = np.full((m, T), np.nan)
    true_full[:, bk_dates] = true_bk
    present = ~np.isnan(rep.rating)
    base = baseline_matrix(rep.played, games, T)
    members = {"all": np.ones(m, dtype=bool), **{c: world.cohort == k for k, c in enumerate(COHORTS)}}
    report = [d for d in REPORT_DATES if d <= T]
    curve_dates = [t for t in range(T) if t in bk_dates]
    for name, mask in members.items():
        for t in curve_dates:
            out[f"est.{name}.{t + 1:03d}"] = corr(rep.rating[:, t], true_full[:, t], mask & present[:, t])
        for d in report:
            t = d - 1
            sel = mask & present[:, t]
            out[f"e2e.{name}.{d:03d}"] = corr(rep.rating[:, t], theta_t[:, t], sel)
            out[f"base.{name}.{d:03d}"] = corr(base[:, t], true_full[:, t], sel)
            out[f"validt.{name}.{d:03d}"] = corr(true_full[:, t], theta_t[:, t], sel)
    out["players_rated.last"] = float(present[:, -1].sum())

    used_pos = {int(j): u for u, j in enumerate(world.used)}
    templates = [snapshot.template[int(j)] for j in world.used]
    seed_stats("seed", rep.prompt_seed, true_prompt, templates, out)
    seed_stats("after", rep.prompt_rating, true_prompt, templates, out)
    out["prompt.rd_seed"] = float(rep.prompt_rd_seed.mean())
    out["prompt.games_mean"] = float(rep.prompt_games.mean())
    e_pred_deciles(world, snapshot, games, rep, used_pos, out)

    last = rep.rating[:, -1]
    ok = ~np.isnan(last)
    out["see.bk_p5"], out["see.bk_p50"], out["see.bk_p95"] = (float(np.percentile(last[ok], q)) for q in (5, 50, 95))
    dives = np.zeros((m, T))
    np.add.at(dives, (games.player, games.date), games.points)
    out["see.dive_median"] = float(np.median(dives[rep.played]))
    out["see.dive_p10"], out["see.dive_p90"] = (float(np.percentile(dives[rep.played], q)) for q in (10, 90))
    if not full or T <= REPORT_DATES[0]:
        return out

    # level and spread of the engine's scale against the oracle's, at the last date
    err_all = rep.rating - true_full
    out["scale.level_offset.last"] = float(np.nanmedian(err_all[:, -1]))
    sel = ok & ~np.isnan(true_full[:, -1])
    out["scale.slope.last"] = float(np.polyfit(true_full[sel, -1], rep.rating[sel, -1], 1)[0])

    # RD calibration: error after removing the cross-section's level offset on each date, from date 7 on
    start = REPORT_DATES[0] - 1
    errs, rds, mis, unc = [], [], [], []
    bk_pct, true_pct = percentile_matrix(rep.rating), percentile_matrix(true_full)
    for t in range(start, T):
        sel = present[:, t] & ~np.isnan(true_full[:, t])
        if sel.sum() < MIN_COHORT:
            continue
        e = err_all[sel, t]
        errs.append(np.abs(e - np.median(e)))
        rds.append(rep.rd[sel, t])
        mis.append(np.abs(bk_pct[sel, t] - true_pct[sel, t]) > PLACE_BAND)
        unc.append(rep.rd[sel, t] > tunables["glicko"]["uncertain_rd_threshold"])
    errs, rds, mis, unc = map(np.concatenate, (errs, rds, mis, unc))
    out["rd.cov1.all"], out["rd.cov2.all"] = float((errs <= rds).mean()), float((errs <= 2 * rds).mean())
    for b, (lo, hi) in enumerate(itertools.pairwise(RD_BUCKETS)):
        sel = (rds >= lo) & (rds < hi)
        out[f"rd.bucket{b}.n"] = float(sel.sum())
        out[f"rd.bucket{b}.cov1"] = float((errs[sel] <= rds[sel]).mean()) if sel.any() else float("nan")
        out[f"rd.bucket{b}.cov2"] = float((errs[sel] <= 2 * rds[sel]).mean()) if sel.any() else float("nan")
    out["rd.uncertain_share"] = float(unc.mean())
    out["rd.misplaced_uncertain"] = float(mis[unc].mean()) if unc.any() else float("nan")
    out["rd.misplaced_settled"] = float(mis[~unc].mean()) if (~unc).any() else float("nan")
    out["rd.misplaced_ratio"] = out["rd.misplaced_uncertain"] / out["rd.misplaced_settled"] \
        if out["rd.misplaced_settled"] > 0 else float("nan")

    # placement: dives until within the band of the true percentile, and staying there for PLACE_STAY dives
    inside = np.abs(bk_pct - true_pct) <= PLACE_BAND
    placed = []
    for i in range(m):
        seq = inside[i, rep.played[i]].astype(int)
        if len(seq) < PLACE_MIN_DIVES:
            continue
        stay = np.convolve(seq, np.ones(PLACE_STAY, dtype=int), "valid") == PLACE_STAY
        placed.append(float(np.argmax(stay)) + 1.0 if stay.any() else NOT_PLACED)
    placed = np.array(placed)
    out["placement.players"] = float(len(placed))
    out["placement.median"] = float(np.median(placed)) if len(placed) else float("nan")
    out["placement.share_placed"] = float((placed < NOT_PLACED).mean()) if len(placed) else float("nan")
    out["placement.p75"] = float(np.percentile(placed, 75)) if len(placed) else float("nan")

    # inflation: slope of the mean BK of the players without drift, per 30 dates, from INFLATION_START on
    stable = (world.drift == 0.0) & present[:, min(INFLATION_START, T) - 1]
    if T > INFLATION_START + 5 and stable.sum() >= MIN_COHORT:
        ts = np.arange(INFLATION_START - 1, T)
        means = np.nanmean(rep.rating[stable][:, ts], axis=0)
        out["inflation.per30"] = float(np.polyfit(ts, means, 1)[0] * 30.0)
    else:
        out["inflation.per30"] = float("nan")

    for thr in analysis["uncertain_rd_thresholds"]:
        flag = rds > thr
        out[f"unc.t{thr:03d}.share"] = float(flag.mean())
        out[f"unc.t{thr:03d}.mis_uncertain"] = float(mis[flag].mean()) if flag.any() else float("nan")
        out[f"unc.t{thr:03d}.mis_settled"] = float(mis[~flag].mean()) if (~flag).any() else float("nan")
        for k in analysis["uncertain_after_dives"]:
            rd_k = np.array([rep.rd[i, np.flatnonzero(rep.played[i])[k - 1]] for i in range(m) if rep.played[i].sum() >= k])
            out[f"unc.k{k:02d}.t{thr:03d}.share"] = float((rd_k > thr).mean()) if len(rd_k) else float("nan")
    club_metrics(world, club_true, clubs, rep, tunables, analysis["club_min_prompts"], true_full[:, -1], out)
    if analysis["lock_games"]:
        lock_metrics(world, snapshot, games, rep, true_prompt, tunables, analysis["lock_games"], out)
    return out


def lock_metrics(world: World, snapshot, games: GameArrays, rep: Replayed, true_prompt: np.ndarray, tunables: dict,
                 ns: list[int], out: dict) -> None:
    """A prompt's rating after its first n answers (players in id order), against the true rating, over the prompts
    that have at least n answers."""
    params = Params.from_tunables(tunables)
    used_pos = {int(j): u for u, j in enumerate(world.used)}
    errs: dict[int, list[float]] = {n: [] for n in ns}
    order = np.argsort(games.prompt, kind="stable")  # stable: players stay in id order within a prompt
    prompts = games.prompt[order]
    bounds = np.flatnonzero(np.diff(prompts, prepend=-1, append=-1))
    for lo, hi in itertools.pairwise(bounds):
        idx = order[lo:hi]
        u = used_pos[int(prompts[lo])]
        opps = [Opponent(rep.rating_before[p, d], rep.rd_before[p, d], 1.0 - pts / params.points_scale)
                for p, d, pts in zip(games.player[idx], games.date[idx], games.points[idx], strict=True)]
        for n in ns:
            if len(opps) >= n:
                r = rate_period(rep.prompt_seed[u], rep.prompt_rd_seed[u], params.initial_volatility, 0, opps[:n], params)
                errs[n].append(r.rating - true_prompt[u])
    for n in ns:
        e = np.array(errs[n])
        out[f"lock.n{n:03d}.prompts"] = float(len(e))
        out[f"lock.n{n:03d}.rmse"] = float(np.sqrt(np.mean(e**2))) if len(e) else float("nan")
        out[f"lock.n{n:03d}.bias"] = float(e.mean()) if len(e) else float("nan")


def club_metrics(world: World, club_true: np.ndarray, clubs: list[int], rep: Replayed, tunables: dict,
                 visible_steps: list[int], true_overall: np.ndarray, out: dict) -> None:
    cum: dict[tuple[int, int], list[tuple[int, int, float, float]]] = {}
    for key, rows in rep.club_rows.items():
        total, acc = 0, []
        for t, g, r, rd in sorted(rows):
            total += g
            acc.append((t, total, r, rd))
        cum[key] = acc
    # first visible club rating of a daily fan (any favourite club) for each candidate minimum, 1-based date
    daily = np.flatnonzero(world.cohort == COHORTS.index("daily"))
    fans = [i for i in daily if world.favourites[i]]
    out["clubs.fans_daily"] = float(len(fans))
    for visible in sorted({*visible_steps, tunables["glicko"]["club_rating_min_prompts"]}):
        first = []
        for i in fans:
            dates = [next((t + 1 for t, total, _, _ in cum.get((int(i), c), []) if total >= visible), None) for c in world.favourites[i]]
            dates = [d for d in dates if d is not None]
            if dates:
                first.append(min(dates))
        out[f"clubs.v{visible:02d}.share"] = len(first) / len(fans) if fans else float("nan")
        out[f"clubs.v{visible:02d}.median"] = float(np.median(first)) if first else float("nan")
    out["clubs.first_visible_share"] = out[f"clubs.v{tunables['glicko']['club_rating_min_prompts']:02d}.share"]
    out["clubs.first_visible_median"] = out[f"clubs.v{tunables['glicko']['club_rating_min_prompts']:02d}.median"]
    col = {c: k for k, c in enumerate(clubs)}
    for n in CLUB_STEPS:
        per_club: dict[int, list[tuple[float, float, float, float, float]]] = {}
        for (i, c), rows in cum.items():
            hit = next(((t, r, rd) for t, total, r, rd in rows if total >= n), None)
            if hit is not None:
                per_club.setdefault(c, []).append((hit[1], hit[2], club_true[i, col[c]], rep.rating[i, hit[0]], true_overall[i]))
        rhos, rds, errs, err_rds, deltas = [], [], [], [], []
        for c, rows in per_club.items():
            if len(rows) >= MIN_CLUB_PLAYERS:
                a = np.array(rows)
                rhos.append(spearman(a[:, 0], a[:, 2]))
                # what the club rating adds to the player's overall rating: club minus overall, against the true difference
                deltas.append(spearman(a[:, 0] - a[:, 3], a[:, 2] - a[:, 4]))
                rds.append(a[:, 1].mean())
                e = a[:, 0] - a[:, 2]  # level-aligned within the club, as for the sport scope
                errs.append(np.abs(e - np.median(e)))
                err_rds.append(a[:, 1])
        if errs:
            e, r = np.concatenate(errs), np.concatenate(err_rds)
            out[f"clubs.n{n}.cov1"], out[f"clubs.n{n}.cov2"] = float((e <= r).mean()), float((e <= 2 * r).mean())
        else:
            out[f"clubs.n{n}.cov1"] = out[f"clubs.n{n}.cov2"] = float("nan")
        out[f"clubs.n{n}.spearman"] = float(np.nanmean(rhos)) if rhos else float("nan")
        out[f"clubs.n{n}.delta_spearman"] = float(np.nanmean(deltas)) if deltas else float("nan")
        out[f"clubs.n{n}.rd"] = float(np.mean(rds)) if rds else float("nan")
        out[f"clubs.n{n}.clubs"] = float(len(rhos))
