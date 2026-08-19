"""Settings: .env secrets + config.yaml waterfall configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG: dict[str, Any] = {
    "budget": {"per_run_usd": 10.0},
    "thresholds": {
        "identity_confidence": 0.60,
        "pattern_proven": 0.60,
        "max_emails_per_company": 8,
        "max_guesses_per_person": 3,
        "people_per_role": 2,
    },
    "roles": ["legal", "ceo", "coo", "cfo", "privacy"],
    "generic_fallbacks": {"legal": ["legal", "privacy"], "privacy": ["privacy"]},
    "stages": {
        "identify": ["sheet", "sitescrape", "apollo", "serp", "newsrss", "anymail", "exa", "edgar", "llm_researcher"],
        "pattern": ["sheet", "hunter"],
        "email": ["sheet", "hunter", "anymail", "apollo", "permute"],
        "verify": ["millionverifier", "reoon", "hunter"],
    },
    "llm_researcher": {"model": "claude-opus-5", "max_searches": 6, "enabled_by_default": False},
    "cache_ttl_days": 30,
}


def find_root(start: Path | None = None) -> Path:
    """Project root = nearest ancestor containing config.yaml or pyproject.toml."""
    p = (start or Path.cwd()).resolve()
    for cand in (p, *p.parents):
        if (cand / "config.yaml").exists() or (cand / "pyproject.toml").exists():
            return cand
    return p


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, root: Path | None = None):
        self.root = find_root(root)
        load_dotenv(self.root / ".env")
        cfg_path = self.root / "config.yaml"
        user_cfg = {}
        if cfg_path.exists():
            user_cfg = yaml.safe_load(cfg_path.read_text()) or {}
        self.cfg = _deep_merge(DEFAULT_CONFIG, user_cfg)
        self.data_dir = self.root / "data"
        self.db_path = self.data_dir / "cache.sqlite"
        self.output_dir = self.data_dir / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # --- secrets -------------------------------------------------------
    def key(self, env_name: str | None) -> str | None:
        if not env_name:
            return None
        val = os.environ.get(env_name, "").strip()
        return val or None

    # --- config accessors ---------------------------------------------
    @property
    def roles(self) -> list[str]:
        return list(self.cfg["roles"])

    def stage(self, name: str) -> list[str]:
        return list(self.cfg["stages"].get(name, []))

    @property
    def thresholds(self) -> dict[str, Any]:
        return self.cfg["thresholds"]

    @property
    def budget_per_run(self) -> float:
        env = self.key("BUDGET_PER_RUN_USD")
        if env:
            try:
                return float(env)
            except ValueError:
                pass
        return float(self.cfg["budget"]["per_run_usd"])

    @property
    def generic_fallbacks(self) -> dict[str, list[str]]:
        return self.cfg.get("generic_fallbacks", {})

    @property
    def cache_ttl_days(self) -> int:
        return int(self.cfg.get("cache_ttl_days", 30))

    @property
    def llm_cfg(self) -> dict[str, Any]:
        return self.cfg.get("llm_researcher", {})
