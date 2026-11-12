"""report.md, metrics.json, CSVs and charts of `simulation sweep` and `simulation tiers`."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from rating import ENGINE_VERSION

from .experiment import BAR_NAMES, evaluate_bars
from .report_run import f0, f3, mark, pc, table, write_csv
from .runner import clean, git_commit, run_dir_name
from .seedfit import tunables_block
from .sweep import get, label

W = ("model-true", "model-wrong")


def rat(x: float) -> str:
    return "n/a" if np.isnan(x) else f"{x:.1f}"


def combo_text(c) -> str:
    return f"tau {c[0]:g}, child RD {c[1]:g}, game weight {c[2]:g}"


def tiers_table(rows: list[dict]) -> list[str]:
    return table(["variant", "points", "cutoffs", "validity", "end-to-end, date 28, 1000 players", "testers date 28: median (p10 to p90)",
                  "dive score median (p10 to p90) of 700", "prompts failing the tier gate", "points from You Know Ball"],
                 [[r["variant"], "/".join(str(p) for p in r["points"]), "/".join(f"{c:g}" for c in r["cumulative_share"][:4]),
                   f3(r["validity"]), f3(r["e2e_28_population"]), f"{f3(r['e2e_28_testers_median'])} ({f3(r['e2e_28_testers_p10'])} to {f3(r['e2e_28_testers_p90'])})",
                   f"{f0(r['dive_median'])} ({f0(r['dive_p10'])} to {f0(r['dive_p90'])})", f"{r['gate_failing']} of {r['prompts']}", pc(r["ykb_points_share"])]
                  for r in rows])


def tier_text(rows: list[dict], d: dict) -> list[str]:
    cur = next(r for r in rows if r["variant"] == "current")
    if d["change"]:
        return [f"Variant {d['best_variant']} raises validity by {d['best_gain']:+.3f} (bar: {d['min_gain']:.2f}), so it is recommended."]
    return [(f"The best alternative ({d['best_variant']}) changes validity by {d['best_gain']:+.3f} against the current {cur['validity']:.3f}; the "
            f"design's bar is +{d['min_gain']:.2f}. **Recommendation: keep the current tiers.**")]


def tier_chart(out: Path, rows: list[dict]) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    names = [r["variant"] for r in rows]
    axes[0].bar(names, [r["validity"] for r in rows])
    v = [r["validity"] for r in rows if np.isfinite(r["validity"])]
    if v:
        axes[0].set_ylim(min(v) - 0.02, max(v) + 0.02)
    axes[0].set_title("validity (oracle BK vs true skill)")
    axes[1].bar(names, [r["e2e_28_testers_median"] for r in rows])
    e = [r["e2e_28_testers_median"] for r in rows if np.isfinite(r["e2e_28_testers_median"])]
    if e:
        axes[1].set_ylim(min(e) - 0.02, max(e) + 0.02)
    axes[1].set_title("testers, date 28: median end-to-end Spearman")
    for ax in axes:
        ax.tick_params(axis="x", rotation=40, labelsize=7)
    fig.tight_layout()
    fig.savefig(out / "tier_sensitivity.png", dpi=120)
    plt.close(fig)


def write_tiers_outputs(root: Path, rows: list[dict], decision: dict, snapshot, tunables: dict, timestamp: str) -> Path:
    config = clean({"scenario": "tiers", "variants": rows and [r["variant"] for r in rows], "snapshot": snapshot.header["prompts_sha256"],
                    "tunables": {k: tunables[k] for k in ("dive", "tiers", "glicko")}, "engine_version": ENGINE_VERSION})
    out = root / run_dir_name(timestamp, "tiers", config)
    out.mkdir(parents=True, exist_ok=True)
    L = ["# Tier sensitivity", "",
         ("Each variant re-tiers every prompt with the pipeline's own model code (answer scores and shares are unchanged, so the "
         "stored tiers are reproduced exactly for 'current'), refits the prompt seed map for the variant, and replays the model-true "
         "world: 1000 players for 3 seeds, and the 15-player tester scenario for 200 seeds. Players answer the same way whatever the "
         "points are; only the points and the tier labels change."), ""] + tiers_table(rows) + [""] + tier_text(rows, decision) \
        + ["", "![Tier sensitivity](tier_sensitivity.png)", ""]
    (out / "report.md").write_text("\n".join(L) + "\n")
    (out / "config.resolved.json").write_text(json.dumps({**config, "git_commit": git_commit()}, indent=2, sort_keys=True) + "\n")
    (out / "metrics.json").write_text(json.dumps(clean({"variants": [{k: v for k, v in r.items() if k != "seconds"} for r in rows], "decision": decision}),
                                                  indent=2, sort_keys=True) + "\n")
    write_csv(out / "tiers.csv", ["variant", "validity", "e2e_28_population", "e2e_28_testers_median", "dive_median", "dive_p10", "dive_p90",
                                  "gate_failing", "ykb_points_share"],
              [[r["variant"], r["validity"], r["e2e_28_population"], r["e2e_28_testers_median"], r["dive_median"], r["dive_p10"], r["dive_p90"],
                r["gate_failing"], r["ykb_points_share"]] for r in rows])
    tier_chart(out, rows)
    return out


def feasibility_chart(out: Path, r: dict) -> None:
    sweep = r["sweep"]
    taus, crds, ws = sweep["tau"], sweep["child_scope_initial_rd"], sweep["game_weight"]
    by = {e.combo: e for e in r["ranked"]}
    fig, axes = plt.subplots(2, len(ws), figsize=(3.4 * len(ws), 6), squeeze=False)
    scores = [e.rows[w]["objective"] for e in r["ranked"] for w in W if np.isfinite(e.rows[w]["objective"])]
    lo, hi = min(scores), max(scores)
    for i, w in enumerate(W):
        for j, wt in enumerate(ws):
            ax = axes[i][j]
            grid = np.array([[by[(t, c, wt)].rows[w]["objective"] for c in crds] for t in taus], dtype=float)
            ax.imshow(grid, vmin=lo, vmax=hi, cmap="viridis", aspect="auto")
            for a, t in enumerate(taus):
                for b, c in enumerate(crds):
                    row = by[(t, c, wt)].rows[w]
                    if by[(t, c, wt)].diverged:
                        ax.text(b, a, "div", ha="center", va="center", fontsize=6, color="red")
                        continue
                    ok = sweep["coverage_min"] <= row["cov2"] <= sweep["coverage_max"]
                    ax.text(b, a, f"{row['cov2']:.0%}", ha="center", va="center", fontsize=6, color="white" if ok else "red",
                            fontweight="bold" if ok else "normal")
            ax.set_xticks(range(len(crds)), [f"{c:g}" for c in crds])
            ax.set_yticks(range(len(taus)), [f"{t:g}" for t in taus])
            ax.set_title(f"{w}, w={wt:g}", fontsize=8)
            if j == 0:
                ax.set_ylabel("tau")
            if i == 1:
                ax.set_xlabel("child RD")
    fig.suptitle("Colour: mean estimation Spearman, dates 7-61. Number: 2 RD coverage (white = inside 90-98%, red = outside)", fontsize=9)
    fig.tight_layout()
    fig.savefig(out / "grid_feasibility.png", dpi=120)
    plt.close(fig)


def marker_chart(out: Path, r: dict) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    ts = [u["threshold"] for u in r["uncertain"]]
    for w in W:
        axes[0].plot(ts, [u[w]["mis_uncertain"] for u in r["uncertain"]], "o-", label=f"{w}: uncertain")
        axes[0].plot(ts, [u[w]["mis_settled"] for u in r["uncertain"]], "s--", label=f"{w}: settled")
    axes[0].set_xlabel("uncertain RD threshold")
    axes[0].set_ylabel("share misplaced by > 10 percentile points")
    axes[0].legend(fontsize=6)
    ks = r["sweep"]["uncertain_after_dives"]
    for u in r["uncertain"]:
        axes[1].plot(ks, [u["model-true"][f"after_{k}"] for k in ks], "o-", label=f"threshold {u['threshold']}")
    axes[1].set_xlabel("dives played")
    axes[1].set_ylabel("share marked uncertain (model-true)")
    axes[1].set_xscale("log")
    axes[1].legend(fontsize=6)
    ns = [c["min_prompts"] for c in r["clubs"]]
    for w in W:
        axes[2].plot(ns, [c[w]["median_date"] for c in r["clubs"]], "o-", label=f"{w}: median date")
    axes[2].set_xlabel("club_rating_min_prompts")
    axes[2].set_ylabel("date a daily fan's club rating appears")
    axes[2].legend(fontsize=6)
    fig.tight_layout()
    fig.savefig(out / "marker_and_clubs.png", dpi=120)
    plt.close(fig)


def tester_chart(out: Path, r: dict) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), sharey=True)
    for ax, w in zip(axes, W):
        for tag, entry in (("recommended", r["winner"]), ("runner-up", r["runner_up"])):
            ps = r["testers"][w][label(entry.combo)]["per_seed"]
            if ps:
                ax.hist(ps["e2e"], bins=np.linspace(0, 1, 26), alpha=0.5, label=tag)
        ax.axvline(0.8, color="k", lw=0.8)
        ax.set_title(w)
        ax.set_xlabel("BK vs true skill at date 28 (15 testers)")
    axes[0].set_ylabel("seeds")
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(out / "tester_distribution.png", dpi=120)
    plt.close(fig)


def write_sweep_outputs(root: Path, r: dict, timestamp: str) -> Path:
    from .tuning import tunables_diff

    sweep, win, run_up = r["sweep"], r["winner"], r["runner_up"]
    n_feasible = sum(e.feasible for e in r["ranked"])
    diff = tunables_diff(r["current"], r["recommended"])
    unc_rec, unc_why = r["uncertain_rec"]
    club_rec, club_why = r["club_rec"]
    fit, dec = r["fit"], r["tier_decision"]
    start = next(e for e in r["ranked"] if e.combo == (0.5, 250, 1.0)) if any(e.combo == (0.5, 250, 1.0) for e in r["ranked"]) else None
    bars_rec = {w: r["bars"][(w, "recommended")] for w in W}
    passed = {w: [b["passed"] for b in bars_rec[w]] for w in W}
    all_pass = all(p is True for w in W for p in passed[w])
    failed = [(w, b["bar"], b["text"]) for w in W for b in bars_rec[w] if b["passed"] is False]
    config = clean({"scenario": "sweep", "seed": r["seed"], "world": r["cfg"], "scenarios": r["scenarios"], "sweep": sweep,
                    "recommended": {k: r["recommended"][k] for k in ("dive", "tiers", "glicko")},
                    "snapshot": r["snapshot"].header["prompts_sha256"], "engine_version": ENGINE_VERSION})
    out = root / run_dir_name(timestamp, "sweep", config)
    out.mkdir(parents=True, exist_ok=True)

    L = ["# Step 4 tuning: recommended tunables", "", "## Read this first", ""]
    L += [f"- **Glicko parameters.** Recommended: **{combo_text(win.combo)}**. It is the best combination whose 2 RD coverage is inside "
          f"{sweep['coverage_min']:.0%} to {sweep['coverage_max']:.0%} in both worlds ({n_feasible} of {len(r['ranked'])} combinations are). "
          f"Mean estimation Spearman over dates {sweep['objective_first_date']} to the end: "
          + ", ".join(f"{w} {win.rows[w]['objective']:.4f}" for w in W) + ". Runner-up: " + combo_text(run_up.combo) + " ("
          + ", ".join(f"{w} {run_up.rows[w]['objective']:.4f}" for w in W) + ")."
          if win.feasible else f"- **No combination keeps 2 RD coverage inside the band in both worlds.** The closest is {combo_text(win.combo)}."]
    diverged = [e for e in r["ranked"] if e.diverged]
    if diverged:
        L += [f"- **The engine diverged** (volatility and ratings blew up, a failure of the Glicko update itself) for {len(diverged)} of {len(r['ranked'])} combinations: "
              + "; ".join(sorted({f"tau {e.combo[0]:g} with game weight {e.combo[2]:g}" for e in diverged})) + ". They are treated as infeasible. Standard Glicko-2 has no "
              "cap on volatility; a cap would be a change to the engine (bump ENGINE_VERSION)."]
    edges = [name for name, key, v in (("tau", "tau", win.combo[0]), ("child RD", "child_scope_initial_rd", win.combo[1]), ("game weight", "game_weight", win.combo[2]))
             if v in (min(sweep[key]), max(sweep[key])) and len(sweep[key]) > 1]
    if edges:
        L += [f"- Edge of the grid: the winner sits at the {' and '.join(edges)} limit of the values searched; a better value may lie beyond it."]
    if max(sweep["game_weight"]) > 3:
        L += [("- Game weights above 3 (4, 5, 6) are outside the grid first specified. They were added because nothing from 1 to 3 kept 2 RD coverage under 98% "
              "(98.1% at 3, still falling).")]
    L += [("- The sport-scope metrics (every pass bar) do not depend on child RD, which only sets where a new club scope starts. Child RD is therefore chosen "
          "among otherwise tied combinations by closeness to the starting 250. Lower values rank clubs slightly better against the true club BK, but that is mostly overall skill "
          "showing through (see the club-specific column), so it is not taken as evidence.")]
    if start is not None:
        L += ["- The starting values (tau 0.5, child RD 250, game weight 1) with the fitted seed had 2 RD coverage "
              + " and ".join(f"{start.rows[w]['cov2']:.1%} ({w})" for w in W) + "; the winner brings it to "
              + " and ".join(f"{win.rows[w]['cov2']:.1%}" for w in W) + "."]
    L += [f"- **Prompt seed.** log_recall_mass alone is not enough; the fit chose **{fit['chosen']}** (cross-validated error {f0(fit['base_cv_rmse_true'])} to "
          f"{f0(next(t for t in fit['table'] if t['model'] == fit['chosen'])['cv_rmse_true'])} rating points). Template offsets: "
          + (", ".join(f"{k} {v:+.0f}" for k, v in fit["template_offsets"].items()) if fit["template_offsets"] else "none (no template with at least "
             f"{fit['min_template_prompts']} prompts is off by more than 50)") + ".",
          f"- **Uncertain marker.** Recommended threshold **{unc_rec}** ({unc_why}); current is {int(r['current']['glicko']['uncertain_rd_threshold'])}.",
          f"- **Club rating visibility.** Recommended **{club_rec}** prompts ({club_why}); current is {int(r['current']['glicko']['club_rating_min_prompts'])}.",
          "- **Tiers.** " + tier_text(r["tier_rows"], dec)[0],
          "- **Pass bars at the recommended values:** " + ("all pass in both worlds." if all_pass else
              "; ".join(f"{w}: {bar} ({text})" for w, bar, text in failed) + " fail(s). See the table below."), ""]

    L += ["## Recommended tunables (a diff against tunables.toml, not applied)", "", "```diff"] + diff + ["```", "",
          "How each value was chosen, and the runner-up:", ""]
    notes = [["glicko.tau, child_scope_initial_rd, game_weight", combo_text(win.combo),
              f"best mean estimation Spearman ({win.score:.4f}) among combinations feasible in both worlds; ties go to game weight nearest 1 and tau in 0.3 to 1.2",
              combo_text(run_up.combo) + f" ({run_up.score:.4f})"],
             ["glicko.prompt_seed", f"intercept {fit['intercept']:.1f}, rd {fit['rd']:.0f}, " + ", ".join(f"{k} {v:.2f}" for k, v in fit["coefficients"].items()),
              "OLS to the oracle's true prompt ratings; the extra feature must cut leave-one-template-out RMSE by 15%; rd is the wrong-world CV RMSE, floor 60",
              f"log_recall_mass alone (CV {f0(fit['base_cv_rmse_true'])})"],
             ["glicko.uncertain_rd_threshold", str(unc_rec), unc_why,
              "next best: " + str(sorted(r["uncertain"], key=lambda u: -u["f1"])[1]["threshold"]) if len(r["uncertain"]) > 1 else ""],
             ["glicko.club_rating_min_prompts", str(club_rec), club_why, "see the club table"],
             ["tiers", "unchanged" if not dec["change"] else dec["best_variant"], f"validity gain {dec['best_gain']:+.3f} against the bar of {dec['min_gain']:.2f}",
              f"best alternative: {dec['best_variant']}"]]
    L += table(["key", "recommended", "chosen by", "runner-up"], notes) + ["", "Prompt seed block as it would appear:", "", "```toml", tunables_block(
        {**fit, "rd": fit["rd"]}), "```", ""]

    L += ["## Pass bars at the recommended values", "",
          ("Population bars are the mean over 3 seeds of 1000 players; the tester bar is the median over 200 seeds. 'Starting point' is "
          "tau 0.5, child RD 250, game weight 1 with the same seed map (population bars only)."), ""]
    cols = [(w, "recommended") for w in W] + [(w, "runner-up") for w in W]
    start_bars = {w: evaluate_bars(r["per_world"][w][label((0.5, 250, 1.0))], None) for w in W} if start is not None else {}
    header = ["bar"] + [f"{w}, {t}" for w, t in cols] + ([f"{w}, starting point" for w in W] if start_bars else [])
    rows = []
    for i, name in enumerate(BAR_NAMES):
        row = [name] + [f"{r['bars'][c][i]['text']} **{mark(r['bars'][c][i]['passed'])}**" for c in cols]
        if start_bars:
            row += [f"{start_bars[w][i]['text']} **{mark(start_bars[w][i]['passed'])}**" for w in W]
        rows.append(row)
    L += table(header, rows) + [""]

    L += ["## Testers (15 players, 28 dates, 200 seeds)", "", "Rank correlation of BK with true skill, median and p10 to p90 across seeds.", ""]
    rows = []
    for w in W:
        for tag, entry in (("recommended", win), ("runner-up", run_up)):
            agg = r["testers"][w][label(entry.combo)]["agg"]
            rows.append([w, tag] + [f"{f3(get(agg, f'e2e.all.{d:03d}', 'median'))} ({f3(get(agg, f'e2e.all.{d:03d}', 'p10'))} to {f3(get(agg, f'e2e.all.{d:03d}', 'p90'))})" for d in (7, 14, 28)])
    L += table(["world", "config", "date 7", "date 14", "date 28"], rows) + ["", "![Tester distribution](tester_distribution.png)", ""]

    L += ["## The grid: top 10", "",
          (f"{len(r['ranked'])} combinations, both worlds, 3 seeds each. Feasible means 2 RD coverage inside {sweep['coverage_min']:.0%} to "
          f"{sweep['coverage_max']:.0%} in both worlds. Objective is the mean estimation Spearman over dates {sweep['objective_first_date']} to the last, averaged "
          "over the two worlds. Coverage errors have the level offset of each date removed; a calibrated RD gives about 68% within 1 RD and 95% within 2."), ""]
    feasible = [e for e in r["ranked"] if e.feasible] or r["ranked"]
    top = [win, run_up] + [e for e in feasible if e is not win and e is not run_up][:8] if win is not run_up else [win] + [e for e in feasible if e is not win][:9]
    rows = []
    for k, e in enumerate(top, 1):
        t, f = e.rows["model-true"], e.rows["model-wrong"]
        tag = " (recommended)" if e is win else " (runner-up)" if e is run_up and e is not win else ""
        rows.append([str(k) + tag, f"{e.combo[0]:g}", f"{e.combo[1]:g}", f"{e.combo[2]:g}", f"{t['objective']:.4f} / {f['objective']:.4f}",
                     f"{t['cov2']:.1%} / {f['cov2']:.1%}", f"{t['cov1']:.1%} / {f['cov1']:.1%}", f"{rat(t['misplaced_ratio'])} / {rat(f['misplaced_ratio'])}",
                     f"{f0(t['placement'])} / {f0(f['placement'])}", f"{t['inflation']:+.1f} / {f['inflation']:+.1f}",
                     f"{t['irregular_margin']:+.4f} / {f['irregular_margin']:+.4f}",
                     f"{f3(t['club_spearman_10'])} / {f3(f['club_spearman_10'])}; club-specific {f3(t['club_delta_10'])} / {f3(f['club_delta_10'])}; {pc(t['club_cov2_10'])} / {pc(f['club_cov2_10'])}"])
    L += [("The recommended and runner-up combinations come first (many combinations are tied within the tie tolerance, and the rule picks among them); "
          "the rest follow by score."), ""]
    L += table(["#", "tau", "child RD", "w", "objective (true / wrong)", "within 2 RD", "within 1 RD", "uncertain misplaced ratio", "placement (dives)",
                "inflation per 30 dates", "irregular margin over mean points", "club rating at 10 prompts: Spearman; club-specific Spearman; 2 RD coverage (true / wrong)"], rows)
    L += ["", f"Objective range over all feasible combinations: {min(e.score for e in r['ranked'] if e.feasible):.4f} to {max(e.score for e in r['ranked'] if e.feasible):.4f}"
          if n_feasible else "No combination was feasible.", "", "![Grid feasibility](grid_feasibility.png)", ""]

    L += ["## The uncertain marker", "",
          ("At the winning parameters. 'Misplaced' means more than 10 percentile points from the truth. The marker is a detector of misplaced players; F1 balances how "
          "often a marked player is really misplaced against how many of the misplaced players get marked. 'After k dives' is the share of players with at least k dives "
          "who are marked right after their k-th dive (model-true)."), ""]
    rows = []
    for u in r["uncertain"]:
        t = u["model-true"]
        rows.append([str(u["threshold"]), pc(t["share"]), pc(t["mis_uncertain"]), pc(t["mis_settled"]), (rat(t["ratio"]) + "x" if not np.isnan(t["ratio"]) else "n/a"), pc(t["recall"]), f"{u['f1']:.3f}"]
                    + [pc(t[f"after_{k}"]) for k in sweep["uncertain_after_dives"]])
    L += table(["threshold", "observations marked", "misplaced if marked", "misplaced if not", "ratio", "recall", "F1 (both worlds)"]
               + [f"marked after {k} dives" for k in sweep["uncertain_after_dives"]], rows)
    L += ["", f"Recommendation: **{unc_rec}** ({unc_why}).", "", "## Club rating visibility", "",
          ("For daily players with a favourite club: the date their favourite club's rating reaches the minimum number of prompts, the share who get there within "
          f"{r['scenarios']['population']['dates']} dates, and the within-club rank correlation and mean RD at the moment players reach that many prompts "
          "(over clubs with at least 8 such players; model-true / model-wrong)."), ""]
    rows = [[str(c["min_prompts"]), f"{f0(c['model-true']['median_date'])} / {f0(c['model-wrong']['median_date'])}",
             f"{pc(c['model-true']['share'])} / {pc(c['model-wrong']['share'])}", f"{f3(c['model-true']['spearman'])} / {f3(c['model-wrong']['spearman'])}",
             f"{f3(c['model-true']['delta'])} / {f3(c['model-wrong']['delta'])}", f"{f0(c['model-true']['rd'])} / {f0(c['model-wrong']['rd'])}", f"{f0(c['model-true']['clubs'])}"] for c in r["clubs"]]
    L += table(["minimum prompts", "median date visible", "share of fans within the season", "within-club Spearman", "club-specific Spearman", "mean RD", "clubs measured"], rows)
    L += ["", f"Recommendation: **{club_rec}** ({club_why}).", "", "![Marker and clubs](marker_and_clubs.png)", "",
          "## Prompt lock (moot in v1, since each prompt is rated on one date)", "",
          "Prompt-rating error after its first n answers, over prompts with at least n answers (model-true / model-wrong).", ""]
    rows = [[str(k["games"]), f"{f0(k['model-true']['prompts'])}", f"{f0(k['model-true']['rmse'])} / {f0(k['model-wrong']['rmse'])}",
             f"{k['model-true']['bias']:+.0f} / {k['model-wrong']['bias']:+.0f}"] for k in r["locks"]]
    L += table(["answers", "prompts", "RMSE vs true rating", "bias"], rows)
    L += ["", "## Tier sensitivity", "", "Model-true world; each variant re-tiered with the pipeline's model code, the seed map refitted for it.", ""] + tiers_table(r["tier_rows"]) \
        + [""] + tier_text(r["tier_rows"], dec) + ["", "![Tier sensitivity](tier_sensitivity.png)", ""]
    L += ["## Prompt seed fit", ""] + table(["model", "in-sample RMSE (true)", "CV RMSE (true)", "CV RMSE (wrong world)"],
                                          [[t["model"], f0(t["in_sample_rmse_true"]), f0(t["cv_rmse_true"]), f0(t["cv_rmse_wrong"])] for t in fit["table"]])
    L += ["", f"Offsets need at least {fit['min_template_prompts']} prompts in the template. Mean residual by template: "
          + ", ".join(f"{t} {x:+.0f} (n={fit['template_prompts'][t]})" for t, x in fit["template_mean_residuals"].items()) + ".", ""]
    L += [("Files: `metrics.json`, `config.resolved.json`, `grid.csv`, `pass_bars.csv`, `uncertain.csv`, `club_visibility.csv`, `lock.csv`, `tiers.csv`, "
          "`tester_distribution.csv`, `seed_fit.csv` and the PNGs."), ""]
    (out / "report.md").write_text("\n".join(L) + "\n")
    (out / "config.resolved.json").write_text(json.dumps({**config, "git_commit": git_commit()}, indent=2, sort_keys=True) + "\n")
    metrics = {"winner": list(win.combo), "runner_up": list(run_up.combo), "feasible": n_feasible, "diff": diff,
               "uncertain": r["uncertain"], "uncertain_recommendation": unc_rec, "clubs": r["clubs"], "club_recommendation": club_rec,
               "locks": r["locks"], "tiers": [{k: v for k, v in t.items() if k != "seconds"} for t in r["tier_rows"]], "tier_decision": dec,
               "bars": {f"{w}/{t}": b for (w, t), b in r["bars"].items()},
               "grid": [{"combo": list(e.combo), "feasible": e.feasible, "score": e.score, "rows": e.rows} for e in r["ranked"]],
               "seed_fit": {k: v for k, v in fit.items() if not k.startswith("cv_predictions")}}
    (out / "metrics.json").write_text(json.dumps(clean(metrics), indent=2, sort_keys=True) + "\n")
    write_csv(out / "grid.csv", ["tau", "child_rd", "w", "feasible", "score"] + [f"{w}.{k}" for w in W for k in ("objective", "cov2", "cov1", "misplaced_ratio", "placement", "inflation", "irregular_margin")],
              [[*e.combo, str(e.feasible), e.score] + [e.rows[w][k] for w in W for k in ("objective", "cov2", "cov1", "misplaced_ratio", "placement", "inflation", "irregular_margin")] for e in r["ranked"]])
    write_csv(out / "pass_bars.csv", ["world", "config", "bar", "value", "result"], [[w, t, b["bar"], b["text"], mark(b["passed"])] for (w, t), bl in r["bars"].items() for b in bl])
    write_csv(out / "uncertain.csv", ["threshold", "world", "share", "mis_uncertain", "mis_settled", "ratio", "recall"] + [f"after_{k}" for k in sweep["uncertain_after_dives"]],
              [[u["threshold"], w, *[u[w][k] for k in ("share", "mis_uncertain", "mis_settled", "ratio", "recall")], *[u[w][f"after_{k}"] for k in sweep["uncertain_after_dives"]]] for u in r["uncertain"] for w in W])
    write_csv(out / "club_visibility.csv", ["min_prompts", "world", "median_date", "share", "spearman", "rd", "clubs"],
              [[c["min_prompts"], w, *[c[w][k] for k in ("median_date", "share", "spearman", "rd", "clubs")]] for c in r["clubs"] for w in W])
    write_csv(out / "lock.csv", ["answers", "world", "prompts", "rmse", "bias"], [[k["games"], w, k[w]["prompts"], k[w]["rmse"], k[w]["bias"]] for k in r["locks"] for w in W])
    write_csv(out / "tiers.csv", ["variant", "validity", "e2e_28_population", "e2e_28_testers_median", "dive_median", "gate_failing", "ykb_points_share"],
              [[t["variant"], t["validity"], t["e2e_28_population"], t["e2e_28_testers_median"], t["dive_median"], t["gate_failing"], t["ykb_points_share"]] for t in r["tier_rows"]])
    write_csv(out / "tester_distribution.csv", ["world", "config", "seed", "e2e_28", "est_28"],
              [[w, tag, seed, a, b] for w in W for tag, entry in (("recommended", win), ("runner-up", run_up))
               for seed, a, b in zip(r["testers"][w][label(entry.combo)]["seeds"], r["testers"][w][label(entry.combo)]["per_seed"].get("e2e", []),
                                     r["testers"][w][label(entry.combo)]["per_seed"].get("est", []), strict=False)])
    write_csv(out / "seed_fit.csv", ["model", "in_sample_rmse_true", "cv_rmse_true", "cv_rmse_wrong"], [[t["model"], t["in_sample_rmse_true"], t["cv_rmse_true"], t["cv_rmse_wrong"]] for t in fit["table"]])
    feasibility_chart(out, r)
    marker_chart(out, r)
    tester_chart(out, r)
    tier_chart(out, r["tier_rows"])
    return out
