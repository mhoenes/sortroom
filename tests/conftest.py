import os

import pytest

# The built-in schedule would start real runs whenever a test starts the app.
os.environ.setdefault("SORTROOM_SCHEDULER", "off")
# Tests run the same everywhere: a local .env (loaded when email_sorter.api is imported) or a
# login in the shell must not decide whether a credential counts as set. Tests that need one set it.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for _name in ("IMAP_USER", "IMAP_PASSWORD", "CLASSIFIER_API_KEY"):
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def german_ui(monkeypatch):
    """Most UI tests check German texts: German is the default here unless a config sets [ui] language.
    tests/test_i18n.py covers English and switching."""
    from email_sorter import i18n
    monkeypatch.setattr(i18n, "DEFAULT_LANGUAGE", "de")
