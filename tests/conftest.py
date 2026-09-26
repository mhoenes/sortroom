import os

import pytest

# The built-in schedule would start real runs whenever a test starts the app.
os.environ.setdefault("SORTROOM_SCHEDULER", "off")


@pytest.fixture(autouse=True)
def german_ui(monkeypatch):
    """Most UI tests check German texts: German is the default here unless a config sets [ui] language.
    tests/test_i18n.py covers English and switching."""
    from email_sorter import i18n
    monkeypatch.setattr(i18n, "DEFAULT_LANGUAGE", "de")
