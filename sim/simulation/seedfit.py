"""The prompt seed map (design section 3): rating = intercept + sum(coefficient * feature) (+ template offset), fitted by
OLS to the oracle's true prompt ratings, with leave-one-template-out cross-validation."""

import copy
from dataclasses import dataclass

import numpy as np

FEATURES = ("log_recall_mass", "ln_answer_count", "top_share", "ln_n_eff")
BASE_FEATURE = "log_recall_mass"
MIN_GAIN = 0.15  # another feature must cut the CV RMSE by this much to ship
OFFSET_THRESHOLD = 50.0  # a template's mean residual above this gets an offset
RD_FLOOR = 60.0


@dataclass(frozen=True)
class SeedData:
    """One row per prompt: its features and its true rating in each world (averaged over the seeds it was played in)."""
    prompt_ids: np.ndarray
    templates: list[str]
    features: np.ndarray  # (n, 4) in FEATURES order
    y_true: np.ndarray  # model-true world
    y_wrong: np.ndarray  # model-wrong world


def design(features: np.ndarray, names: tuple[str, ...]) -> np.ndarray:
    cols = [features[:, FEATURES.index(n)] for n in names]
    return np.column_stack([np.ones(len(features)), *cols])


def ols(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return np.linalg.lstsq(x, y, rcond=None)[0]


def template_folds(templates: list[str]) -> list[np.ndarray]:
    """Leave-one-template-out: one boolean test mask per template, in sorted template order; the masks partition the rows."""
    arr = np.array(templates)
    return [arr == t for t in sorted(set(templates))]


def loto_predictions(x: np.ndarray, y_train: np.ndarray, templates: list[str]) -> np.ndarray:
    """Each prompt's prediction from a fit that never saw its template (trained on y_train)."""
    pred = np.zeros(len(y_train))
    for test in template_folds(templates):
        pred[test] = x[test] @ ols(x[~test], y_train[~test])
    return pred


def rmse(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sqrt(np.mean((a - b) ** 2)))


def propose(data: SeedData, min_template_prompts: int = 10) -> dict:
    """The proposed seed map and the numbers behind the choice."""
    models = {"intercept only": ()}
    models[BASE_FEATURE] = (BASE_FEATURE,)
    for f in FEATURES:
        if f != BASE_FEATURE:
            models[f"{BASE_FEATURE} + {f}"] = (BASE_FEATURE, f)
    models["all four"] = FEATURES
    table = []
    for name, feats in models.items():
        x = design(data.features, feats)
        pred_true = loto_predictions(x, data.y_true, data.templates)
        pred_wrong = loto_predictions(x, data.y_true, data.templates)
        full = x @ ols(x, data.y_true)
        table.append({"model": name, "features": list(feats), "in_sample_rmse_true": rmse(full, data.y_true),
                      "cv_rmse_true": rmse(pred_true, data.y_true), "cv_rmse_wrong": rmse(pred_wrong, data.y_wrong)})
    base_cv = next(r["cv_rmse_true"] for r in table if r["model"] == BASE_FEATURE)
    extras = [r for r in table if r["model"] not in ("intercept only", BASE_FEATURE)]
    best = min(extras, key=lambda r: r["cv_rmse_true"])
    chosen = best if best["cv_rmse_true"] <= (1 - MIN_GAIN) * base_cv else next(r for r in table if r["model"] == BASE_FEATURE)
    feats = tuple(chosen["features"])
    x = design(data.features, feats)
    beta = ols(x, data.y_true)
    resid = data.y_true - x @ beta
    offsets = {}
    for t in sorted(set(data.templates)):
        sel = np.array(data.templates) == t
        mean = float(resid[sel].mean())
        if abs(mean) > OFFSET_THRESHOLD and sel.sum() >= min_template_prompts:
            offsets[t] = mean
    cv_wrong = chosen["cv_rmse_wrong"]
    return {"table": table, "base_cv_rmse_true": base_cv, "best_extra": best["model"], "best_extra_cv_rmse_true": best["cv_rmse_true"],
            "chosen": chosen["model"], "intercept": float(beta[0]), "coefficients": dict(zip(feats, map(float, beta[1:]), strict=True)),
            "template_offsets": offsets, "min_template_prompts": min_template_prompts,
            "template_prompts": {t: int(sum(x == t for x in data.templates)) for t in sorted(set(data.templates))},
            "template_mean_residuals": {t: float(resid[np.array(data.templates) == t].mean())
                                                                      for t in sorted(set(data.templates))},
            "cv_rmse_wrong_world": cv_wrong, "rd": max(RD_FLOOR, cv_wrong), "rd_floor_applied": cv_wrong < RD_FLOOR,
            "cv_predictions_true": loto_predictions(x, data.y_true, data.templates),
            "cv_predictions_wrong": loto_predictions(x, data.y_true, data.templates)}


def with_seed(tunables: dict, p: dict) -> dict:
    """A copy of the tunables with the proposed seed map in [glicko.prompt_seed]."""
    out = copy.deepcopy(tunables)
    seed = out["glicko"]["prompt_seed"]
    seed["intercept"], seed["rd"], seed["coefficients"] = p["intercept"], p["rd"], dict(p["coefficients"])
    if p["template_offsets"]:
        seed["template_offsets"] = dict(p["template_offsets"])
    return out


def tunables_block(p: dict) -> str:
    coeffs = ", ".join(f"{k} = {v:.6g}" for k, v in p["coefficients"].items())
    lines = ["[glicko.prompt_seed]", f"intercept = {p['intercept']:.6g}", f"rd = {p['rd']:.6g}", f"coefficients = {{ {coeffs} }}"]
    if p["template_offsets"]:
        lines.append("template_offsets = { " + ", ".join(f"{k} = {v:.6g}" for k, v in p["template_offsets"].items()) + " }")
    return "\n".join(lines)
