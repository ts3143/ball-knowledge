"""Read-only calibration of the pick-share model: summary numbers per template, a parameter sweep and the
report against T's gut lists. Nothing here writes to the database.
"""

import itertools
import statistics
import tomllib
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ingest.normalize import normalize
from prompts.templates import TEMPLATES

from .features import Features
from .model import ModelParams, RatedPrompt, rate_prompt

SWEEP_KEYS = ("alpha", "beta", "half_life_seasons", "fame_floor_views", "max_bandwagon_answer_share")
TIER_MISS_BAR = 0  # T-top answers allowed in Deep Cut or You Know Ball
CONCORDANCE_BAR = 0.9


@dataclass
class Rated:
    """A stored prompt with its features (computed once) and what the model says about it."""
    prompt: Any  # preview.StoredPrompt
    features: dict[int, Features]
    result: RatedPrompt

    @property
    def n(self) -> int:
        return len(self.features)


def rate_all(prompts, features: dict[int, dict[int, Features]], mp: ModelParams) -> list[Rated]:
    return [Rated(p, features[p.id], rate_prompt(features[p.id], mp)) for p in prompts]


# ---------------------------------------------------------------------------
# Summary numbers
# ---------------------------------------------------------------------------

def quantiles(values: list[float]) -> tuple[float, float, float, float, float]:
    """min, quartiles, max."""
    if not values:
        return (0.0,) * 5
    q = statistics.quantiles(values, n=4, method="inclusive") if len(values) > 1 else [values[0]] * 3
    return min(values), q[0], q[1], q[2], max(values)


def group_stats(items: list[Rated], min_alternatives: list[int], min_answers: int, max_answers: int,
                n_tiers: int) -> dict:
    """The numbers for one group of prompts. `items` is everything that passes the size gate at the lowest
    alternative; the distributions use those that pass at the real minimum."""
    yields = {m: (sum(r.result.gate["pass"] for r in items if m <= r.n <= max_answers),
                  sum(m <= r.n <= max_answers for r in items)) for m in min_alternatives}
    base = [r for r in items if min_answers <= r.n <= max_answers]
    reasons = Counter(x.split(":")[0] if x.startswith("Bandwagon") else x
                      for r in base for x in r.result.gate["reasons"])
    deep = [(e, r) for r in base for e, t in r.result.tiers.items() if t >= n_tiers - 1]
    flagged_deep = sum(bool(r.prompt.flags[e]) for e, r in deep)  # stored weak_stint / edge_season flags
    cross = Counter((r.prompt.mix_pass, r.result.gate["pass"]) for r in base if r.prompt.mix_pass is not None)

    def med(values):
        return statistics.median(values) if values else 0.0

    return {
        "yields": yields, "base": len(base), "reasons": reasons,
        "tier_counts": [med([r.result.gate["tier_counts"][k] for r in base]) for k in range(n_tiers)],
        "top_share": med([r.result.gate["top_share"] for r in base]),
        "n_eff_ratio": med([r.result.gate["n_eff"] / r.n for r in base]),
        "expected_points": quantiles([r.result.expected_points for r in base]),
        "deep_answers": len(deep), "deep_flagged": flagged_deep, "cross": cross}


def all_group_stats(rated: list[Rated], tunables: dict, mp: ModelParams) -> dict[str, dict]:
    gates = tunables["quality_gates"]
    alts = sorted(set(gates["report_min_answers_alternatives"]) | {gates["min_answers"]})
    groups = {t: [r for r in rated if r.prompt.template == t] for t in TEMPLATES}
    groups = {t: g for t, g in groups.items() if g}
    groups["all"] = list(rated)
    return {t: group_stats(g, alts, gates["min_answers"], gates["max_answers"], len(mp.tier_names))
            for t, g in groups.items()}


def fmt_yield(y: tuple[int, int]) -> str:
    return f"{y[0]}/{y[1]}"


def print_summary(stats: dict[str, dict], tunables: dict, mp: ModelParams) -> None:
    gates = tunables["quality_gates"]
    alts = sorted(set(gates["report_min_answers_alternatives"]) | {gates["min_answers"]})
    print("\n" + "=" * 78 + f"\nTIER GATE AND MODEL SUMMARY (alpha {mp.alpha}, beta {mp.beta}, half-life "
          f"{mp.half_life_seasons}, floor {mp.fame_floor_views}, Bandwagon max {mp.max_bandwagon_answer_share:.0%} "
          f"of answers)\n" + "=" * 78)
    print(f"\nTier gate passes among size-passing prompts (pass/size-passing) at min answers {alts}:")
    for t, s in stats.items():
        print(f"  {t:<24}" + "".join(f"{'min ' + str(m):>9} {fmt_yield(s['yields'][m]):>8}" for m in alts))
    print(f"\nFailure reasons at min {gates['min_answers']} (a prompt can have several):")
    for t, s in stats.items():
        reasons = ", ".join(f"{k} x{v}" for k, v in sorted(s["reasons"].items())) or "none"
        print(f"  {t:<24} of {s['base']:>4}: {reasons}")
    print(f"\nMedians over the {gates['min_answers']}+ prompts: tier counts ({'/'.join(mp.tier_names)}), "
          "top share, n_eff/N, then expected points (min, q1, median, q3, max):")
    for t, s in stats.items():
        counts = "/".join(f"{c:g}" for c in s["tier_counts"])
        pts = " ".join(f"{x:5.1f}" for x in s["expected_points"])
        print(f"  {t:<24} tiers {counts:<16} top {100 * s['top_share']:4.1f}%  n_eff/N {s['n_eff_ratio']:.2f}  pts {pts}")
    print("\nWeak or edge-flagged answers in the two deepest tiers (flagged / all answers there):")
    for t, s in stats.items():
        print(f"  {t:<24} {s['deep_flagged']:>4} / {s['deep_answers']}")
    print("\nAdvisory step-2 mix verdict (rows) against the tier gate (columns), prompt counts:")
    print(f"  {'':<24}{'mix pass':>20}{'mix fail':>20}")
    print(f"  {'':<24}{'tier pass':>10}{'tier fail':>10}{'tier pass':>10}{'tier fail':>10}")
    for t, s in stats.items():
        c = s["cross"]
        print(f"  {t:<24}{c[(True, True)]:>10}{c[(True, False)]:>10}{c[(False, True)]:>10}{c[(False, False)]:>10}")


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def parse_sweep(specs: list[str]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for spec in specs:
        key, _, values = spec.partition("=")
        if key not in SWEEP_KEYS or not values:
            raise SystemExit(f"--sweep takes key=v1,v2,... with key one of {', '.join(SWEEP_KEYS)}; got {spec!r}")
        try:
            out[key] = [float(v) for v in values.split(",")]
        except ValueError:
            raise SystemExit(f"--sweep {key}: not numbers: {values!r}") from None
    return out


def sweep(prompts, features, tunables: dict, mp: ModelParams, grid: dict[str, list[float]]) -> None:
    keys = list(grid)
    gates = tunables["quality_gates"]
    m = gates["min_answers"]
    print(f"\nSWEEP ({', '.join(keys)}); tier gate passes / size-passing prompts at min {m} per template, "
          "then all prompts at min 10 / 12 / 15, median top share, median n_eff/N, median expected points")
    for combo in itertools.product(*grid.values()):
        p = replace(mp, **dict(zip(keys, combo)))
        stats = all_group_stats(rate_all(prompts, features, p), tunables, p)
        label = " ".join(f"{k}={v:g}" for k, v in zip(keys, combo))
        per_template = " ".join(f"{t[:9]}:{fmt_yield(s['yields'][m])}" for t, s in stats.items() if t != "all")
        a = stats["all"]
        alts = [fmt_yield(a["yields"][x]) for x in (10, 12, 15) if x in a["yields"]]
        print(f"{label}\n    {per_template}\n    all {' '.join(alts)}  top {100 * a['top_share']:.1f}%  "
              f"n_eff/N {a['n_eff_ratio']:.2f}  pts {a['expected_points'][2]:.1f}  "
              f"fails: {', '.join(f'{k} x{v}' for k, v in sorted(a['reasons'].items())) or 'none'}")


# ---------------------------------------------------------------------------
# Gut report
# ---------------------------------------------------------------------------

def resolve_name(name: str, canonical: dict[int, str], aliases: dict[int, list[str]]) -> tuple[int | None, str]:
    """(entity id, "") or (None, error). Matches the normalized name against the canonical names first, then
    the aliases, within one answer set. More than one candidate is an error that lists them."""
    key = normalize(name)
    for source in (lambda e: [canonical[e]], lambda e: aliases.get(e, [])):
        hits = sorted(e for e in canonical if key in {normalize(x) for x in source(e)})
        if len(hits) == 1:
            return hits[0], ""
        if hits:
            return None, f"{name!r} is ambiguous: " + ", ".join(f"{canonical[e]} (id {e})" for e in hits)
    return None, f"{name!r} is not in the answer set"


def concordance(scores: dict[int, float], top: list[int], bottom: list[int]) -> tuple[float, int]:
    """(points, pairs): each top x bottom pair scores 1 when the top answer has the higher score, half on a tie."""
    points = sum(1.0 if scores[t] > scores[b] else 0.5 if scores[t] == scores[b] else 0.0
                 for t in top for b in bottom if t != b)
    return points, sum(t != b for t in top for b in bottom)


def model_order(scores: dict[int, float]) -> list[int]:
    return sorted(scores, key=lambda e: (-scores[e], e))


@dataclass
class GutResult:
    points: float = 0.0
    pairs: int = 0
    top_overlap: int = 0
    top_size: int = 0
    bottom_overlap: int = 0
    bottom_size: int = 0
    top_deep: int = 0  # T-top answer in Deep Cut or You Know Ball
    top_real_one_or_deeper: int = 0
    bottom_casual_or_shallower: int = 0
    top_count: int = 0
    bottom_count: int = 0

    def add(self, other: "GutResult") -> None:
        for f in self.__dataclass_fields__:
            setattr(self, f, getattr(self, f) + getattr(other, f))

    @property
    def concordance(self) -> float | None:
        return self.points / self.pairs if self.pairs else None


def gut_result(r: RatedPrompt, top: list[int], bottom: list[int], n_tiers: int) -> GutResult:
    order = model_order(r.scores)
    pts, pairs = concordance(r.scores, top, bottom)
    return GutResult(
        points=pts, pairs=pairs, top_overlap=len(set(top) & set(order[:5])) if top else 0, top_size=len(top),
        bottom_overlap=len(set(bottom) & set(order[-5:])) if bottom else 0, bottom_size=len(bottom),
        top_deep=sum(r.tiers[e] >= n_tiers - 1 for e in top),
        top_real_one_or_deeper=sum(r.tiers[e] >= 3 for e in top),
        bottom_casual_or_shallower=sum(r.tiers[e] <= 2 for e in bottom),
        top_count=len(top), bottom_count=len(bottom))


def fmt_gut(g: GutResult) -> str:
    c = g.concordance
    return (f"concordance {'-' if c is None else f'{c:.2f}'} ({g.points:g}/{g.pairs})  "
            f"top-5 overlap {g.top_overlap}/{g.top_size}  bottom-5 overlap {g.bottom_overlap}/{g.bottom_size}  "
            f"misses: top in Real One+ {g.top_real_one_or_deeper}, top in Deep Cut+ {g.top_deep}, "
            f"bottom in Casual- {g.bottom_casual_or_shallower}")


def load_aliases(cur, entity_ids: list[int]) -> dict[int, list[str]]:
    """entity id -> aliases. `cur` must be a dict_row cursor."""
    cur.execute("select entity_id, alias from entity_aliases where entity_id = any(%s)", (entity_ids,))
    out: dict[int, list[str]] = {}
    for r in cur.fetchall():
        out.setdefault(r["entity_id"], []).append(r["alias"])
    return out


def read_gut(path: Path) -> list[dict]:
    """The sheet's prompts that have a top or bottom list filled in; [] if the file is missing."""
    if not path.exists():
        return []
    return [e for e in tomllib.loads(path.read_text()).get("prompt", []) if e.get("top") or e.get("bottom")]


def gut_report(entries: list[dict], rated: dict[int, Rated], aliases: dict[int, list[str]], mp: ModelParams,
               path: Path) -> str:
    """The text report. `rated` maps prompt id to its Rated; entries are the filled-in sheet prompts."""
    if not entries:
        return (f"gut.toml ({path}) has no top or bottom list filled in yet (or does not exist), so there is "
                "nothing to compare. T fills it in before seeing any tiers.")
    out, overall, n_tiers = [], GutResult(), len(mp.tier_names)
    for entry in entries:
        pid, r = entry.get("id"), rated.get(entry.get("id"))
        if r is None:
            out.append(f"[{pid}] not found in the database; skipped")
            continue
        names = {e: r.prompt.names[e] for e in r.features}
        lists, errors = {}, []
        for which in ("top", "bottom"):
            ids = []
            for name in entry.get(which, []):
                e, err = resolve_name(name, names, {e: aliases.get(e, []) for e in names})
                if e is None:
                    errors.append(f"{which}: {err}")
                elif e in ids:
                    errors.append(f"{which}: {name!r} is listed twice")
                else:
                    ids.append(e)
            lists[which] = ids
        out.append(f"\n[{pid}] {r.prompt.text}")
        out += [f"  ERROR {x}" for x in errors]
        g = gut_result(r.result, lists["top"], lists["bottom"], n_tiers)
        overall.add(g)
        out.append("  " + fmt_gut(g))
        order = model_order(r.result.scores)
        out.append("  model top 5:    " + "; ".join(names[e] for e in order[:5]))
        out.append("  T top:          " + "; ".join(names[e] for e in lists["top"]))
        out.append("  model bottom 5: " + "; ".join(names[e] for e in order[-5:]))
        out.append("  T bottom:       " + "; ".join(names[e] for e in lists["bottom"]))
        for which, ids in lists.items():
            out += [f"    {which} {names[e]}: {mp.tier_names[r.result.tiers[e] - 1]}" for e in ids
                    if (which == "top" and r.result.tiers[e] >= 3) or (which == "bottom" and r.result.tiers[e] <= 2)]
    out.append("\nOVERALL " + fmt_gut(overall))
    c = overall.concordance
    verdict = ("bar met" if c is not None and c >= CONCORDANCE_BAR and overall.top_deep <= TIER_MISS_BAR
               else "bar NOT met")
    out.append(f"bar: concordance >= {CONCORDANCE_BAR} and {TIER_MISS_BAR} T-top answers in Deep Cut or You Know Ball: "
               + verdict)
    return "\n".join(out)
