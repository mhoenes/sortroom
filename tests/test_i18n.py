import json
import re
import sys
import time
from datetime import date
from pathlib import Path

from email_sorter import i18n, jobs
from email_sorter.config import load_mailboxes
from test_editing import _csrf, client, setup  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import i18n_check  # noqa: E402

PLACEHOLDER = re.compile(r"%\((\w+)\)s")


def _language(base: Path, code: str) -> None:
    path = base / "config.toml"
    text = re.sub(r"\n\[ui\]\nlanguage = \"\w+\"\n", "\n", path.read_text(encoding="utf-8"))
    path.write_text(text + f'\n[ui]\nlanguage = "{code}"\n', encoding="utf-8")


def test_every_message_is_translated_with_the_same_placeholders():
    wanted = i18n_check.messages()
    for code, catalog in i18n.CATALOGS.items():
        assert sorted(wanted - set(catalog)) == [], f"missing in {code}.json"
        assert sorted(set(catalog) - wanted) == [], f"unused in {code}.json"
        for message, translation in catalog.items():
            for text in translation if isinstance(translation, list) else [translation]:
                assert set(PLACEHOLDER.findall(text)) <= set(PLACEHOLDER.findall(message)) | {"num"}, message


def test_catalog_files_are_sorted_json():
    for code in i18n.CATALOGS:
        path = ROOT / "email_sorter" / "locale" / f"{code}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert list(data) == sorted(data)


def test_english_is_the_default_and_german_is_one_setting_away(client, setup, monkeypatch):
    monkeypatch.setattr(i18n, "DEFAULT_LANGUAGE", "en")  # the product default; conftest pins German
    html = client.get("/ui").text
    assert '<html lang="en">' in html and "All mailboxes" in html and "Alle Postfächer" not in html
    html = client.get("/ui/settings").text
    assert '<option value="en" selected>English</option>' in html
    form = {"csrf": _csrf(html), "endpoint": "https://example.invalid/decisions", "model": "example/model-1",
            "max_body_chars": "3000", "timeout_seconds": "20", "min_interval_seconds": "0", "language": "de"}
    assert client.post("/ui/settings", data=form, follow_redirects=False).status_code == 303
    html = client.get("/ui").text
    assert '<html lang="de">' in html and "Alle Postfächer" in html
    assert '[ui]\nlanguage = "de"' in (setup / "config.toml").read_text(encoding="utf-8")


def test_unknown_language_is_refused(client, setup):
    html = client.get("/ui/settings").text
    form = {"csrf": _csrf(html), "endpoint": "https://example.invalid/decisions", "model": "example/model-1",
            "max_body_chars": "3000", "timeout_seconds": "20", "min_interval_seconds": "0", "language": "xx"}
    r = client.post("/ui/settings", data=form)
    assert r.status_code == 422 and "Unbekannte Sprache" in r.text


def test_pages_and_messages_in_english(client, setup):
    _language(setup, "en")
    for path in ("/ui", "/ui/m/privat", "/ui/m/privat/mails?period=all", "/ui/m/privat/categories",
                 "/ui/m/privat/maintenance", "/ui/m/privat/settings", "/ui/mailboxes/new", "/ui/settings"):
        r = client.get(path)
        assert r.status_code == 200 and '<html lang="en">' in r.text, path
    html = client.get("/ui/m/privat/categories").text
    assert "Test with the model" in html and "Mit dem Modell testen" not in html
    # a form error comes back in English too
    r = client.post("/ui/m/privat/categories", data={"csrf": _csrf(html), "new": "1", "key": "finanzen",
                                                     "description": "x"})
    assert r.status_code == 422 and 'The category "finanzen" already exists.' in r.text.replace("&#34;", '"')
    assert "Standard categories, English" in client.get("/ui/mailboxes/new").text


def test_mailbox_from_the_english_standard_categories(client, setup, monkeypatch):
    from email_sorter import api
    monkeypatch.setattr(api.app.state, "base_dir", setup)  # the new mailbox folder goes there
    html = client.get("/ui/mailboxes/new").text
    form = {"csrf": _csrf(html), "name": "Work", "id": "work", "imap_host": "imap.example.com", "imap_port": "993",
            "source_folder": "INBOX", "imap_user": "me@work.example", "imap_password": "pw",
            "template": "_example_en"}
    assert client.post("/ui/mailboxes/new", data=form, follow_redirects=False).status_code == 303
    box = load_mailboxes(setup, setup / "config.toml")["work"]
    assert {"purchases", "promotions", "updates"} <= box.cfg.categories.keys()
    assert box.cfg.categories["finance"].folder == "INBOX/Finance" and box.cfg.categories["portal"].folder == "INBOX/Updates"
    assert box.cfg.expired_folder == "INBOX/Promotions/Expired"


def test_english_formatting():
    token = i18n.set_language("en")
    try:
        assert i18n.num(12345) == "12,345" and i18n.conf(0.456) == "0.46"
        year = date.today().year
        assert i18n.dt(f"{year}-09-24T21:58:00") == "24 Sep 21:58"
        assert i18n.dt("2020-01-14T16:32:00") == "14 Jan 2020"
        assert i18n.date("2026-09-24") == "24 Sep 2026"
        assert i18n.ngettext("%(num)s entry", "%(num)s entries", 1) == "1 entry"
        assert i18n.ngettext("%(num)s entry", "%(num)s entries", 3) == "3 entries"
    finally:
        i18n.reset_language(token)
    token = i18n.set_language("de")
    try:
        assert i18n.ngettext("%(num)s entry", "%(num)s entries", 3) == "3 Einträge"
        assert i18n.date("2026-09-24") == "24.09.2026"
    finally:
        i18n.reset_language(token)


def test_background_jobs_keep_the_language_of_their_request(setup):
    box = load_mailboxes(setup, setup / "config.toml")["privat"]
    token = i18n.set_language("en")
    try:
        job = jobs.start(box, "check", "x", lambda: {"ok": True, "summary": i18n._("dry run")}, needs_lock=False)
    finally:
        i18n.reset_language(token)
    for _ in range(100):
        if job["status"] != "running":
            break
        time.sleep(0.01)
    assert job["result"]["summary"] == "dry run"  # not the tests' German default
