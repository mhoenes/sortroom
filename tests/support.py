"""Shared test helpers."""
from pathlib import Path

from email_sorter.config import EXAMPLE_MAILBOX, Config, _read_toml, config_from_raw

CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.toml"


def example_config() -> Config:
    """The built-in example mailbox with the repository's shared [jev] settings."""
    return config_from_raw({**_read_toml(EXAMPLE_MAILBOX), "jev": _read_toml(CONFIG)["jev"]}, "example")
