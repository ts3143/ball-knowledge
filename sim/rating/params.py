"""Engine parameters and the tunables loader. Every value comes from tunables.toml (or an explicit override)."""

import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

TUNABLES_PATH = Path(__file__).resolve().parents[2] / "tunables.toml"


@dataclass(frozen=True)
class SeedParams:
    intercept: float
    rd: float
    coefficients: dict[str, float]


@dataclass(frozen=True)
class Params:
    initial_rating: float
    initial_rd: float  # also the cap for idle growth and for phi*
    initial_volatility: float
    tau: float
    volatility_epsilon: float
    child_scope_initial_rd: float
    game_weight: float
    uncertain_rd_threshold: float
    club_rating_min_prompts: int
    prompt_lock_answers: int
    points_scale: float  # points that make a result of 1.0 (the top tier's points)
    seed: SeedParams

    @classmethod
    def from_tunables(cls, tunables: dict) -> "Params":
        g = tunables["glicko"]
        s = g["prompt_seed"]
        return cls(
            initial_rating=float(g["initial_rating"]), initial_rd=float(g["initial_rd"]),
            initial_volatility=float(g["initial_volatility"]), tau=float(g["tau"]),
            volatility_epsilon=float(g["volatility_epsilon"]),
            child_scope_initial_rd=float(g["child_scope_initial_rd"]), game_weight=float(g["game_weight"]),
            uncertain_rd_threshold=float(g["uncertain_rd_threshold"]),
            club_rating_min_prompts=int(g["club_rating_min_prompts"]),
            prompt_lock_answers=int(g["prompt_lock_answers"]),
            points_scale=float(max(tunables["tiers"]["points"])),
            seed=SeedParams(float(s["intercept"]), float(s["rd"]),
                            {k: float(v) for k, v in s["coefficients"].items()}))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        return cls(**{**d, "seed": SeedParams(**d["seed"])})


def parse_override(text: str) -> tuple[list[str], object]:
    """'glicko.tau=0.8' -> (['glicko', 'tau'], 0.8). The value is parsed as TOML, or kept as a string."""
    key, sep, raw = text.partition("=")
    if not sep or not key.strip():
        raise ValueError(f"override must look like section.key=value, got {text!r}")
    try:
        value = tomllib.loads(f"v = {raw.strip()}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw.strip()
    return [part.strip() for part in key.split(".")], value


def apply_overrides(tree: dict, overrides: list[str]) -> dict:
    """`--set key=value` overrides applied in place. An override must name an existing key, so a typo fails
    instead of silently changing nothing."""
    for text in overrides:
        parts, value = parse_override(text)
        node = tree
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"unknown key {'.'.join(parts)}")
            node = node[part]
        if not isinstance(node, dict) or parts[-1] not in node:
            raise KeyError(f"unknown key {'.'.join(parts)}")
        node[parts[-1]] = value
    return tree


def load_tunables(overrides: list[str] | None = None, path: Path = TUNABLES_PATH) -> dict:
    """tunables.toml as a dict, with `--set key=value` overrides applied."""
    with open(path, "rb") as f:
        tunables = tomllib.load(f)
    return apply_overrides(tunables, overrides or [])


def load_params(overrides: list[str] | None = None) -> Params:
    return Params.from_tunables(load_tunables(overrides))
