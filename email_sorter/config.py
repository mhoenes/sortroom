from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_KEY_RE = re.compile(r"^[a-z0-9_]+$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Category:
    key: str
    description: str
    folder: str | None
    flag: bool
    flag_on_action: bool


@dataclass(frozen=True)
class Config:
    imap_host: str
    imap_port: int
    source_folder: str
    jev_endpoint: str
    jev_model: str
    jev_api_key_env: str
    max_body_chars: int
    timeout_seconds: float
    min_interval_seconds: float
    min_confidence: float
    action_flag_threshold: float
    lookback_days: int
    max_per_run: int
    categories: dict[str, Category]

    @property
    def descriptions(self) -> dict[str, str]:
        return {key: cat.description for key, cat in self.categories.items()}

    def jev_client(self, api_key: str):
        from .jev import JevClient

        return JevClient(api_key, self.jev_endpoint, self.jev_model,
                         timeout=self.timeout_seconds, min_interval=self.min_interval_seconds)


@dataclass(frozen=True)
class Credentials:
    imap_user: str
    imap_password: str
    jev_api_key: str


def load_config(path: Path) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None

    try:
        imap, jev, rules = raw["imap"], raw["jev"], raw["rules"]
        categories = {}
        for key, c in raw["categories"].items():
            if not _KEY_RE.match(key):
                raise ConfigError(f"category key {key!r} must be lowercase letters, digits or _")
            if not c.get("description", "").strip():
                raise ConfigError(f"category {key!r} needs a description")
            categories[key] = Category(
                key=key,
                description=c["description"].strip(),
                folder=c.get("folder") or None,
                flag=bool(c.get("flag", False)),
                flag_on_action=bool(c.get("flag_on_action", True)),
            )
        cfg = Config(
            imap_host=imap["host"],
            imap_port=int(imap.get("port", 993)),
            source_folder=imap.get("source_folder", "INBOX"),
            jev_endpoint=jev["endpoint"],
            jev_model=jev["model"],
            jev_api_key_env=jev.get("api_key_env", "AI_GATEWAY_API_KEY"),
            max_body_chars=int(jev.get("max_body_chars", 3000)),
            timeout_seconds=float(jev.get("timeout_seconds", 20)),
            min_interval_seconds=float(jev.get("min_interval_seconds", 0.0)),
            min_confidence=float(rules["min_confidence"]),
            action_flag_threshold=float(rules["action_flag_threshold"]),
            lookback_days=int(rules["lookback_days"]),
            max_per_run=int(rules["max_per_run"]),
            categories=categories,
        )
    except KeyError as e:
        raise ConfigError(f"{path}: missing setting {e}") from None

    if len(cfg.categories) < 2:
        raise ConfigError("at least two categories are required")
    for name in ("min_confidence", "action_flag_threshold"):
        if not 0.0 <= getattr(cfg, name) <= 1.0:
            raise ConfigError(f"{name} must be between 0 and 1")
    return cfg


def load_credentials(cfg: Config) -> Credentials:
    names = ("IMAP_USER", "IMAP_PASSWORD", cfg.jev_api_key_env)
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise ConfigError(f"missing in .env / environment: {', '.join(missing)}")
    return Credentials(*(os.environ[n] for n in names))
