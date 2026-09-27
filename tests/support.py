"""Shared test helpers."""
from pathlib import Path

from email_sorter.config import EXAMPLE_MAILBOXES, Config, _read_toml, config_from_raw

CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.toml"


def example_config() -> Config:
    """The built-in example mailbox with the repository's shared [classifier] settings."""
    return config_from_raw({**_read_toml(EXAMPLE_MAILBOXES["de"]), "classifier": _read_toml(CONFIG)["classifier"]}, "example")
