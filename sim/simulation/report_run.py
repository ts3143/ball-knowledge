"""report.md, metrics.json, CSVs and charts of `simulation run` and `simulation fit-seed`."""

import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import functools
import itertools
import operator

import matplotlib.pyplot as plt
import numpy as np

from rating import ENGINE_VERSION

from .experiment import BAR_NAMES, diagnostics, evaluate_bars
from .metrics import COHORT_NAMES, RD_BUCKETS, REPORT_DATES
from .runner import ROUND, clean, git_commit, run_dir_name
from .seedfit import tunables_block

LABELS = ("current", "proposed")
LABEL_TEXT = {"current": "current tunables (seed map without information)", "proposed": "current tunables + proposed seed map"}


def a(agg: dict, key: str, stat: str = "mean") -> float:
    return agg[key][stat] if key in agg else float("nan")


def f3(x: float) -> str:
    return "n/a" if np.isnan(x) else f"{x:.3f}"


def pc(x: float) -> str:
    return "n/a" if np.isnan(x) else f"{x:.1%}"


def f0(x: float) -> str:
    return "n/a" if np.isnan(x) else f"{x:.0f}"


def rng_text(agg: dict, key: str, fmt=f3) -> str:
    """mean (min to max across seeds)."""
    if key not in agg:
        return "n/a"
    s = agg[key]
    return f"{fmt(s['mean'])} ({fmt(s['min'])} to {fmt(s['max'])})"


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(r) + " |" for r in rows]


def mark(passed) -> str:
    return "n/a" if passed is None else ("PASS" if passed else "FAIL")


def bars_table(results: dict, labels: tuple[str, ...]) -> tuple[list[str], dict]:
    cols, bars = [], {}
    for world in ("model-true", "model-wrong"):
        for label in labels:
            pop = results.get((world, "population"), {}).get(label)
            test = results.get((world, "testers"), {}).get(label)
            if pop is None and test is None:
                continue
            bars[(world, label)] = evaluate_bars(pop["agg"] if pop else None, test["agg"] if test else None)
            cols.append((world, label))
    header = ["bar"] + [f"{w}, {l}" for w, l in cols]
    rows = []
    for i, name in enumerate(BAR_NAMES):
        rows.append([name] + [f"{bars[c][i]['text']} **{mark(bars[c][i]['passed'])}**" for c in cols])
    return table(header, rows), bars


def cohort_table(agg: dict, dates: list[int]) -> list[str]:
    rows = []
    for c in COHORT_NAMES:
        row = [c]
        for d in dates:
            row.append(f"{f3(a(agg, f'est.{c}.{d:03d}'))} / {f3(a(agg, f'e2e.{c}.{d:03d}'))} / {f3(a(agg, f'base.{c}.{d:03d}'))}")
        rows.append(row)
    return table(["cohort"] + [f"date {d}" for d in dates], rows)


def seed_table(pop: dict | None, test: dict | None) -> list[str]:
    rows = []
    for name, agg, prefix in (("seed rating, 1000-player scenario", pop, "seed"), ("after the date-close update, 1000 players", pop, "after"),
                              ("seed rating, 15-player testers", test, "seed"), ("after the date-close update, 15 players", test, "after")):
        if agg is None:
            continue
        rows.append([name, f3(a(agg, f"{prefix}.spearman")), f0(a(agg, f"{prefix}.rmse")), f0(a(agg, f"{prefix}.bias")),
                     f0(a(agg, f"{prefix}.rmse_centered"))])
    return table(["prompt rating against the oracle's true rating", "rank correlation", "RMSE", "bias (engine minus true)", "RMSE after removing bias"], rows)


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([round(x, ROUND) if isinstance(x, float) else x for x in r])


def report_text(meta: dict, results: dict, fit: dict | None) -> tuple[str, dict]:
    labels = tuple(l for l in LABELS if any(l in v for v in results.values()))
    bar_lines, bars = bars_table(results, labels)
    L = [f"# Rating simulation report: {meta['title']}", "",
         "This replays simulated players through the real rating engine and compares the ratings it produces with the oracle's "
         "true BK. 'Current' is tunables.toml as it stands (tau 0.5, child RD 250, game weight 1, and a prompt seed map that "
         "carries no information: every prompt starts at 1500 with RD 350). "
         + ("'Proposed' adds the fitted prompt seed map below. " if fit else "") +
         f"Snapshot sha256 `{meta['snapshot_sha256']}`, engine {ENGINE_VERSION}, commit {meta['git_commit']}.", "",
         "## Pass bars", "",
         ("Each cell is the value and whether T's bar is met. Population bars use the mean over the population seeds; the tester bar "
         "uses the median over the tester seeds. 'Spearman with true skill' for the tester bar is the rank correlation of BK with "
         "the player's true knowledge (theta); the correlation with the oracle's true BK is shown next to it."), ""] + bar_lines + [""]
    found = {}
    for (world, label) in bars:
        for code, text, kind in diagnostics(results.get((world, "population"), {}).get(label, {}).get("agg"), f"{world} / {label}"):
            found.setdefault(kind, []).append((world, label, code, text))
    fixed = {(w, c) for (w, l, c, _) in functools.reduce(operator.iadd, found.values(), []) if l == "proposed"}
    L += ["## Things that look wrong rather than untuned", ""]
    engine = [t for (_, _, _, t) in found.get("engine", [])]
    L += [f"- {t}" for t in engine] if engine else [("- Nothing in the automatic checks points at the engine itself: estimation improves with dives, RD "
                                                    "flags the players who are actually misplaced, and RD is not too small.")]
    L += ["", "## Things that look untuned", ""]
    untuned = [f"{t}{' (gone with the proposed seed)' if l == 'current' and (w, c) not in fixed else ''}" for (w, l, c, t) in found.get("untuned", [])]
    L += [f"- {t}" for t in untuned] if untuned else ["- None."]
    L += [""]
    if fit:
        L += ["## The prompt seed map", "",
              ("A prompt's starting rating is intercept + coefficients x features, fitted by least squares to the oracle's true prompt "
              "ratings in the model-true world (averaged over the population seeds). 'CV' is leave-one-template-out: each template "
              "is predicted by a fit that never saw it. 'Wrong world' scores those same predictions against the model-wrong world's "
              "true ratings."), ""]
        L += table(["model", "in-sample RMSE (true)", "CV RMSE (true)", "CV RMSE (wrong world)"],
                   [[r["model"], f0(r["in_sample_rmse_true"]), f0(r["cv_rmse_true"]), f0(r["cv_rmse_wrong"])] for r in fit["table"]])
        L += ["", (f"Rule: ship log_recall_mass alone unless another feature cuts its CV RMSE ({f0(fit['base_cv_rmse_true'])}) by 15%. The best "
              f"addition is {fit['best_extra']} at {f0(fit['best_extra_cv_rmse_true'])}, so the chosen model is **{fit['chosen']}**."),
              "Mean residual of the chosen fit by template, with its number of prompts (an offset is added only above 50, and for templates with at least " + str(fit["min_template_prompts"]) + " prompts): "
              + ", ".join(f"{t} {r:+.0f} (n={fit['template_prompts'][t]})" for t, r in fit["template_mean_residuals"].items()) + ".",
              f"seed_rd = the CV RMSE in the wrong world ({f0(fit['cv_rmse_wrong_world'])}), floor 60: **{f0(fit['rd'])}**.", "",
              "Proposed (not written to tunables.toml):", "", "```toml", tunables_block(fit), "```", ""]
    L += ["## What the prompt ratings look like", "",
          ("The engine's prompt rating before and after the date-close update, against the oracle's true prompt rating. Bias is a "
          "level offset; the rank correlation and the centred RMSE say how well prompts are ordered."), ""]
    for world in ("model-true", "model-wrong"):
        for label in labels:
            pop = results.get((world, "population"), {}).get(label)
            test = results.get((world, "testers"), {}).get(label)
            if pop or test:
                L += [f"**{world}, {label}**", ""] + seed_table(pop["agg"] if pop else None, test["agg"] if test else None) + [""]
    for world in ("model-true", "model-wrong"):
        for label in labels:
            r = results.get((world, "population"), {}).get(label)
            if not r:
                continue
            agg = r["agg"]
            dates = [d for d in REPORT_DATES if f"est.all.{d:03d}" in agg]
            L += [f"## Population: {world}, {label}", "",
                  (f"Rank recovery by cohort at the report dates, as estimation / end to end / mean-points baseline (each a rank "
                  f"correlation, mean over {agg['validity.oracle']['n']} seeds). Estimation: BK against the oracle's true BK. End to end: "
                  "BK against true knowledge. Baseline: mean points so far against true BK."), ""] + cohort_table(agg, dates) + [""]
            L += [f"- Validity (oracle BK against knowledge): {rng_text(agg, 'validity.oracle')}; oracle fit RMSE {rng_text(agg, 'oracle.rmse', lambda x: f'{x:.4f}')}.",
                  (f"- Level and scale: the engine's median BK sits {f0(a(agg, 'scale.level_offset.last'))} points from the true one at the last date, "
                  f"and its spread is {f3(a(agg, 'scale.slope.last'))}x the true spread."),
                  (f"- Placement: median {f0(a(agg, 'placement.median'))} dives; {a(agg, 'placement.share_placed'):.0%} of players with 15+ dives placed "
                  "(within 10 percentile points of the truth for 5 dives in a row)."),
                  f"- Inflation (non-drifting players, last 30 dates): {a(agg, 'inflation.per30'):+.1f} points.",
                  (f"- What players see at the last date: BK p5 {f0(a(agg, 'see.bk_p5'))}, median {f0(a(agg, 'see.bk_p50'))}, p95 {f0(a(agg, 'see.bk_p95'))}; "
                  f"median dive score {f0(a(agg, 'see.dive_median'))} (p10 {f0(a(agg, 'see.dive_p10'))}, p90 {f0(a(agg, 'see.dive_p90'))}) of 700."),
                  (f"- Clubs: a daily fan's favourite-club rating first becomes visible (20 prompts) on date {f0(a(agg, 'clubs.first_visible_median'))} "
                  f"(median; {a(agg, 'clubs.first_visible_share'):.0%} of daily fans get there within the season). Within-club rank correlation "
                  f"and mean RD when a player reaches 10 prompts: {f3(a(agg, 'clubs.n10.spearman'))}, RD {f0(a(agg, 'clubs.n10.rd'))} "
                  f"({f0(a(agg, 'clubs.n10.clubs'))} clubs have enough players); 20 prompts: {f3(a(agg, 'clubs.n20.spearman'))}, RD {f0(a(agg, 'clubs.n20.rd'))} "
                  f"({f0(a(agg, 'clubs.n20.clubs'))} clubs); 30 prompts: {f3(a(agg, 'clubs.n30.spearman'))} ({f0(a(agg, 'clubs.n30.clubs'))} clubs)."),
                  (f"- Calibration of the engine's predicted result per game: largest decile gap {a(agg, 'calib.max_gap'):.3f}, mean gap "
                  f"(predicted minus actual) {a(agg, 'calib.mean_gap'):+.3f}."), "",
                  ("RD calibration: how often the rating error (after removing the level offset of the date) is within 1 and 2 RD, by RD bucket, "
                  "over every player and date from date 7. A well-calibrated RD gives about 68% and 95%."), ""]
            rows = []
            for b, (lo, hi) in enumerate(itertools.pairwise(RD_BUCKETS)):
                rows.append([f"{lo:.0f} to {hi:.0f}", f0(a(agg, f"rd.bucket{b}.n")), pc(a(agg, f"rd.bucket{b}.cov1")), pc(a(agg, f"rd.bucket{b}.cov2"))])
            rows.append(["all", "", f"{a(agg, 'rd.cov1.all'):.1%}", f"{a(agg, 'rd.cov2.all'):.1%}"])
            L += table(["RD bucket", "observations (mean per seed)", "within 1 RD", "within 2 RD"], rows)
            L += ["", (f"Misplaced by more than 10 percentile points: uncertain players {a(agg, 'rd.misplaced_uncertain'):.1%}, settled players "
                  f"{a(agg, 'rd.misplaced_settled'):.1%} ({a(agg, 'rd.uncertain_share'):.1%} of observations are uncertain)."), ""]
    for world in ("model-true", "model-wrong"):
        for label in labels:
            r = results.get((world, "testers"), {}).get(label)
            if not r:
                continue
            agg = r["agg"]
            L += [f"## Testers: {world}, {label}", "",
                  f"15 players, 28 dates, {agg['validity.oracle']['n']} seeds. Rank correlation by date: median and p10 to p90 across seeds.", ""]
            rows = []
            for key, name in (("e2e", "BK vs true knowledge"), ("est", "BK vs true BK"), ("base", "mean points so far vs true BK")):
                row = [name]
                for d in (7, 14, 28):
                    k = f"{key}.all.{d:03d}"
                    row.append(f"{f3(a(agg, k, 'median'))} ({f3(a(agg, k, 'p10'))} to {f3(a(agg, k, 'p90'))})")
                rows.append(row)
            L += table(["", "date 7", "date 14", "date 28"], rows) + [""]
    L += ["![Rank recovery curve](rank_curve.png)", "", "![RD calibration](rd_calibration.png)", "", "![Tester distribution](tester_distribution.png)", ""]
    if fit:
        L += ["![Seed fit](seed_fit.png)", ""]
    L += ["Files next to this report: `metrics.json`, `config.resolved.json`, `pass_bars.csv`, `rank_curve.csv`, `rd_calibration.csv`, "
          "`tester_distribution.csv`" + (", `seed_fit.csv`" if fit else "") + ", and the PNGs.", ""]
    return "\n".join(L), bars


def charts(out: Path, results: dict, fit: dict | None, seed_data) -> None:
    labels = [l for l in LABELS if any(l in v for v in results.values())]
    fig, axes = plt.subplots(1, len(labels), figsize=(6 * len(labels), 4), squeeze=False, sharey=True)
    for ax, label in zip(axes[0], labels):
        r = results.get(("model-true", "population"), {}).get(label)
        if not r:
            continue
        agg = r["agg"]
        for c in COHORT_NAMES:
            ds = sorted(int(k.rsplit(".", 1)[1]) for k in agg if k.startswith(f"est.{c}."))
            ax.plot(ds, [agg[f"est.{c}.{d:03d}"]["mean"] for d in ds], label=c)
        ax.axhline(0.9, color="grey", lw=0.6, ls="--")
        ax.set_title(f"model-true, {label}")
        ax.set_xlabel("dive date")
        ax.set_ylabel("rank correlation: BK vs true BK")
    if axes[0][0].get_legend_handles_labels()[0]:
        axes[0][0].legend()
    fig.tight_layout()
    fig.savefig(out / "rank_curve.png", dpi=120)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4), sharey=True)
    for ax, world in zip(axes, ("model-true", "model-wrong")):
        for label in labels:
            r = results.get((world, "population"), {}).get(label)
            if not r:
                continue
            agg = r["agg"]
            xs = np.arange(len(RD_BUCKETS) - 1)
            ax.plot(xs, [a(agg, f"rd.bucket{b}.cov1") for b in xs], "o-", label=f"{label}: within 1 RD")
            ax.plot(xs, [a(agg, f"rd.bucket{b}.cov2") for b in xs], "s--", label=f"{label}: within 2 RD")
        ax.axhline(0.68, color="grey", lw=0.6)
        ax.axhline(0.95, color="grey", lw=0.6)
        ax.set_xticks(range(len(RD_BUCKETS) - 1), [f"{lo:.0f}-{hi:.0f}" for lo, hi in itertools.pairwise(RD_BUCKETS)])
        ax.set_title(world)
        ax.set_xlabel("RD bucket")
    axes[0].set_ylabel("share of errors inside")
    if axes[0].get_legend_handles_labels()[0]:
        axes[0].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(out / "rd_calibration.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 4))
    for label in labels:
        r = results.get(("model-true", "testers"), {}).get(label)
        if r:
            ax.hist(r["per_seed"].get("e2e", []), bins=np.linspace(0, 1, 26), alpha=0.5, label=label)
    ax.axvline(0.8, color="k", lw=0.8)
    ax.set_xlabel("rank correlation of BK with true knowledge at date 28 (15 testers)")
    ax.set_ylabel("seeds")
    if ax.get_legend_handles_labels()[0]:
        ax.legend()
    fig.tight_layout()
    fig.savefig(out / "tester_distribution.png", dpi=120)
    plt.close(fig)

    if fit:
        fig, axes = plt.subplots(1, 2, figsize=(10, 4.5))
        for ax, key, title in zip(axes, ("y_true", "y_wrong"), ("model-true world", "model-wrong world")):
            y = getattr(seed_data, key)
            pred = fit["cv_predictions_true" if key == "y_true" else "cv_predictions_wrong"]
            for t in sorted(set(seed_data.templates)):
                sel = np.array(seed_data.templates) == t
                ax.plot(pred[sel], y[sel], ".", label=t)
            lim = [min(y.min(), pred.min()), max(y.max(), pred.max())]
            ax.plot(lim, lim, "k--", lw=0.8)
            ax.set_title(title)
            ax.set_xlabel("seed rating (leave-one-template-out)")
            ax.set_ylabel("true prompt rating")
        axes[0].legend(fontsize=6)
        fig.tight_layout()
        fig.savefig(out / "seed_fit.png", dpi=120)
        plt.close(fig)


def write_outputs(root: Path, scenario: str, config: dict, results: dict, fit: dict | None, seed_data, meta: dict,
                  timestamp: str) -> Path:
    config = clean(config)
    out = root / run_dir_name(timestamp, scenario, config)
    out.mkdir(parents=True, exist_ok=True)
    text, bars = report_text(meta, results, fit)
    (out / "report.md").write_text(text)
    (out / "config.resolved.json").write_text(json.dumps({**config, "git_commit": git_commit()}, indent=2, sort_keys=True) + "\n")
    metrics = {f"{w}/{s}/{l}": {"aggregate": r["agg"], "seeds": r["seeds"]} for (w, s), v in results.items() for l, r in v.items()}
    doc = {"pass_bars": {f"{w}/{l}": b for (w, l), b in bars.items()}, "runs": metrics}
    if fit:
        doc["seed_fit"] = {k: v for k, v in fit.items() if not k.startswith("cv_predictions")}
    (out / "metrics.json").write_text(json.dumps(clean(doc), indent=2, sort_keys=True) + "\n")
    rows = [[w, l, b["bar"], b["text"], mark(b["passed"])] for (w, l), bl in bars.items() for b in bl]
    write_csv(out / "pass_bars.csv", ["world", "config", "bar", "value", "result"], rows)
    curve = [[w, l, k.split(".")[1], int(k.rsplit(".", 1)[1]), s["mean"], s["min"], s["max"]]
             for (w, sc), v in results.items() if sc == "population" for l, r in v.items() for k, s in r["agg"].items() if k.startswith("est.")]
    write_csv(out / "rank_curve.csv", ["world", "config", "cohort", "date", "mean", "min", "max"], curve)
    rd = [[w, l, b, a(r["agg"], f"rd.bucket{b}.n"), a(r["agg"], f"rd.bucket{b}.cov1"), a(r["agg"], f"rd.bucket{b}.cov2")]
          for (w, sc), v in results.items() if sc == "population" for l, r in v.items() for b in range(len(RD_BUCKETS) - 1)]
    write_csv(out / "rd_calibration.csv", ["world", "config", "rd_bucket", "observations", "within_1_rd", "within_2_rd"], rd)
    dist = [[w, l, seed, v1, v2] for (w, sc), v in results.items() if sc == "testers" for l, r in v.items()
            for seed, v1, v2 in zip(r["seeds"], r["per_seed"].get("e2e", []), r["per_seed"].get("est", []), strict=False)]
    write_csv(out / "tester_distribution.csv", ["world", "config", "seed", "e2e_spearman_date28", "est_spearman_date28"], dist)
    if fit:
        write_csv(out / "seed_fit.csv", ["model", "in_sample_rmse_true", "cv_rmse_true", "cv_rmse_wrong"],
                  [[r["model"], r["in_sample_rmse_true"], r["cv_rmse_true"], r["cv_rmse_wrong"]] for r in fit["table"]])
    charts(out, results, fit, seed_data)
    return out
