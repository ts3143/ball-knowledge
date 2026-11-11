"""Loads tunables.toml (repo root) and the .env secrets."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_ROOT = REPO_ROOT / "pipeline"
DATA_DIR = PIPELINE_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CACHE_DIR = DATA_DIR / "cache"


@dataclass(frozen=True)
class Settings:
    tunables: dict
    database_url: str
    wikimedia_contact: str

    @property
    def ingest(self) -> dict:
        return self.tunables["ingest"]


def load_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env")
    with open(REPO_ROOT / "tunables.toml", "rb") as f:
        tunables = tomllib.load(f)
    database_url = os.environ.get("DATABASE_URL", "").strip()
    contact = os.environ.get("WIKIMEDIA_CONTACT", "").strip()
    missing = [name for name, value in (("DATABASE_URL", database_url), ("WIKIMEDIA_CONTACT", contact)) if not value]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)} in .env (see .env.example).")
    return Settings(tunables=tunables, database_url=database_url, wikimedia_contact=contact)
