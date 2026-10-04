import os
import re
import stat
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from email_sorter import api, trial
from email_sorter.config import load_mailboxes
from email_sorter.classifier import Decision
from email_sorter.store import Store
from email_sorter.web import editor
from email_sorter.web.editing import EditError, delete_category, save_category, save_sender_rules, save_settings
from support import example_config

PASSWORD = "richtig-geheim"
SHARED = """[classifier]
endpoint = "https://example.invalid/decisions"
model = "example/model-1"
"""
MAILBOX = """# Privates Postfach
name = "Privat"

[imap]
host = "imap.example.de"
port = 993
source_folder = "INBOX"

[rules]
min_confidence = 0.70   # darunter bleibt die Mail liegen
action_flag_threshold = 0.80
lookback_days = 7
max_per_run = 200

[[sender_rules]]
match = "scanner@brother.com"
action = "inbox"

[categories.finanzen]
description = "Rechnungen und Kontoauszüge"
folder = "INBOX/Finanzen"

# Werbung mit Ablaufdatum
[categories.werbung]
description = "Newsletter und Angebote"
folder = "INBOX/Werbung"
flag_on_action = false
track_expiry = true
"""


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "config.toml").write_text(SHARED, encoding="utf-8")
    box_dir = tmp_path / "mailboxes" / "privat"
    box_dir.mkdir(parents=True)
    (box_dir / "mailbox.toml").write_text(MAILBOX, encoding="utf-8")
    return tmp_path


def _box(base):
    return load_mailboxes(base, base / "config.toml")["privat"]


def _raw(base):
    return tomllib.loads((base / "mailboxes" / "privat" / "mailbox.toml").read_text(encoding="utf-8"))


CAT_FORM = {"label": "", "description": "Vereine, Ehrenamt", "folder": "INBOX/Vereine", "flag_on_action": "on"}


# ---------------------------------------------------------------- file edits

def test_edit_category_keeps_comments_and_writes_backup(setup):
    save_category(_box(setup), setup / "config.toml", "werbung",
                  {"description": "Nur Werbung", "folder": "INBOX/Werbung", "track_expiry": "on",
                   "expired_folder": "INBOX/Werbung/Alt", "label": "Werbung"})
    text = (setup / "mailboxes" / "privat" / "mailbox.toml").read_text(encoding="utf-8")
    assert "# Werbung mit Ablaufdatum" in text and "# darunter bleibt die Mail liegen" in text
    cat = _box(setup).cfg.categories["werbung"]
    assert cat.description == "Nur Werbung" and cat.expired_folder == "INBOX/Werbung/Alt"
    assert not cat.flag_on_action and cat.track_expiry
    assert "label" not in _raw(setup)["categories"]["werbung"]  # default label is not written
    assert (setup / "mailboxes" / "privat" / "mailbox.toml.bak").read_text(encoding="utf-8") == MAILBOX


def test_expired_folder_dropped_without_expiry_tracking(setup):
    save_category(_box(setup), setup / "config.toml", "finanzen",
                  {"description": "Rechnungen", "folder": "INBOX/Finanzen", "expired_folder": "INBOX/X"})
    assert "expired_folder" not in _raw(setup)["categories"]["finanzen"]


def test_create_and_delete_category(setup):
    shared = setup / "config.toml"
    assert save_category(_box(setup), shared, "vereine", CAT_FORM, create=True) == "vereine"
    assert _box(setup).cfg.categories["vereine"].label == "Vereine"
    with pytest.raises(EditError, match="gibt es schon"):
        save_category(_box(setup), shared, "vereine", CAT_FORM, create=True)
    delete_category(_box(setup), shared, "vereine")
    assert "vereine" not in _box(setup).cfg.categories


def test_invalid_changes_leave_file_untouched(setup):
    shared, before = setup / "config.toml", MAILBOX
    with pytest.raises(EditError, match="Schlüssel"):
        save_category(_box(setup), shared, "Neu Kat", CAT_FORM, create=True)
    with pytest.raises(EditError, match="Beschreibung"):
        save_category(_box(setup), shared, "finanzen", {"description": "  "})
    with pytest.raises(EditError, match="mindestens|at least two"):
        delete_category(_box(setup), shared, "finanzen")
    assert (setup / "mailboxes" / "privat" / "mailbox.toml").read_text(encoding="utf-8") == before
    assert not (setup / "mailboxes" / "privat" / "mailbox.toml.bak").exists()


def test_save_settings(setup):
    form = {"name": "Privat", "imap_host": "imap.example.com", "imap_port": "993", "source_folder": "INBOX",
            "min_confidence": "0,75", "action_flag_threshold": "0.8", "expiry_threshold": "0,7",
            "min_age_hours": "24", "lookback_days": "7", "max_per_run": "150", "expired_folder": "INBOX/Abgelaufen"}
    assert save_settings(_box(setup), setup / "config.toml", form) == "privat"  # same name: folder stays
    box = _box(setup)
    assert box.name == "Privat" and box.cfg.min_confidence == 0.75 and box.cfg.min_age_hours == 24
    assert box.cfg.max_per_run == 150 and box.cfg.expired_folder == "INBOX/Abgelaufen"
    assert not box.cfg.sort_read_at_once and "sort_read_at_once" not in _raw(setup)["rules"]
    save_settings(box, setup / "config.toml", {**form, "sort_read_at_once": "1"})
    assert _box(setup).cfg.sort_read_at_once
    with pytest.raises(EditError, match="Mindest-Konfidenz"):
        save_settings(box, setup / "config.toml", {**form, "min_confidence": "1,5"})
    with pytest.raises(EditError, match="Port"):
        save_settings(box, setup / "config.toml", {**form, "imap_port": "abc"})


def test_sender_rules_saved(setup):
    shared = setup / "config.toml"
    assert _box(setup).cfg.rule_for("scanner@brother.com").action == "inbox"
    save_sender_rules(_box(setup), shared, [("scanner@brother.com", "inbox"), ("@lieferando.de", "werbung"), ("", "inbox")])
    raw = _raw(setup)
    assert raw["sender_rules"] == [{"match": "scanner@brother.com", "action": "inbox"},
                                   {"match": "@lieferando.de", "action": "werbung"}]
    with pytest.raises(EditError, match="Unbekanntes Ziel"):
        save_sender_rules(_box(setup), shared, [("x@y.de", "gibtsnicht")])
    with pytest.raises(EditError, match="Absender-Regeln"):
        delete_category(_box(setup), shared, "werbung")


def test_inbox_is_refused_as_a_category_key(setup):
    shared = setup / "config.toml"
    with pytest.raises(EditError, match="„inbox“ ist reserviert"):
        save_category(_box(setup), shared, "inbox", {"description": "Alles"}, create=True)


def test_sender_rules_keep_comments(setup):
    path = setup / "mailboxes" / "privat" / "mailbox.toml"
    rules = '[[sender_rules]]\nmatch = "scanner@brother.com"  # Scanner\naction = "inbox"\n\n# Kategorien\n'
    path.write_text(MAILBOX.replace('[[sender_rules]]\nmatch = "scanner@brother.com"\naction = "inbox"\n\n', rules), encoding="utf-8")
    shared = setup / "config.toml"
    save_sender_rules(_box(setup), shared, [("scanner@brother.com", "inbox"), ("@shop.de", "werbung")])
    text = path.read_text(encoding="utf-8")
    assert "# Scanner" in text and text.index('"@shop.de"') < text.index("# Kategorien") < text.index("[categories.finanzen]")
    save_sender_rules(_box(setup), shared, [])
    text = path.read_text(encoding="utf-8")
    assert "sender_rules" not in text and text.index("# Kategorien") < text.index("[categories.finanzen]")


def test_read_only_file(setup):
    path = setup / "mailboxes" / "privat" / "mailbox.toml"
    os.chmod(path, stat.S_IREAD)
    try:
        with pytest.raises(EditError, match="schreibgeschützt"):
            save_category(_box(setup), setup / "config.toml", "finanzen", CAT_FORM)
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


# ---------------------------------------------------------------- trial

def test_trial_descriptions_and_sample(tmp_path):
    cfg = example_config()
    d = trial.with_category(cfg, "werbung", "NEU")
    assert d["werbung"] == "NEU" and d["finanzen"] == cfg.categories["finanzen"].description
    assert trial.with_category(cfg, "vereine", "Vereine")["vereine"] == "Vereine"
    store = Store(tmp_path / "state.db")
    for i, cat in enumerate(["werbung", "werbung", "finanzen", "werbung"]):
        store.record(SimpleNamespace(key=f"<{i}@x>", received=None, sender="a@b", subject=f"s{i}",
                                     decision=Decision(cat, 0.9, {cat: 0.9}, 0.1, 0.0001),
                                     folder="INBOX/X", flag=False, expires=None, source="classifier"))
    store.close()
    rows = trial.sample(tmp_path / "state.db", "werbung", own=2, other=5)
    assert [r[5] for r in rows] == ["werbung", "werbung", "finanzen"]
    store = Store(tmp_path / "state.db")
    store.set_correction("<2@x>", "finanzen", "werbung", "reconcile")  # filed as finance, moved to promotions
    store.close()
    rows = trial.sample(tmp_path / "state.db", "werbung", own=2, other=5)
    assert (rows[0][0], rows[0][5], rows[0][6]) == ("<2@x>", "werbung", True)  # first, with where it belongs
    assert [r[0] for r in rows].count("<2@x>") == 1 and all(not r[6] for r in rows[1:])


# ---------------------------------------------------------------- pages

@pytest.fixture
def client(setup, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(api, "BASE_DIR", setup)
    monkeypatch.setattr(api, "CONFIG_PATH", setup / "config.toml")
    monkeypatch.setattr(api.app.state, "config_path", setup / "config.toml")
    monkeypatch.setattr(api.app.state, "load_mailboxes", lambda: load_mailboxes(setup, setup / "config.toml"))
    with TestClient(api.app) as c:
        c.post("/login", data={"password": PASSWORD})
        yield c


def _csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_categories_page_and_save(client, setup):
    html = client.get("/ui/m/privat/categories").text
    assert "Kategorien · Privat" in html and "Rechnungen und Kontoauszüge" in html and "Mit dem Modell testen" in html
    r = client.post("/ui/m/privat/categories", data={"csrf": _csrf(html), "key": "finanzen", "description": "Geld",
                                                     "folder": "INBOX/Geld"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/privat/categories?cat=finanzen"
    html = client.get(r.headers["location"]).text
    assert "Gespeichert" in html and "INBOX/Geld" in html
    assert _box(setup).cfg.categories["finanzen"].folder == "INBOX/Geld"


def test_category_form_name_first_key_from_the_name_one_star_choice(client, setup):
    from email_sorter.web.editing import key_from_label
    assert [key_from_label(x) for x in ("Vereine & Clubs", "Größe/Übersicht", "Café ", "  ", "a" * 50)] == [
        "vereine_clubs", "groesse_uebersicht", "cafe", "", "a" * 40]

    html = client.get("/ui/m/privat/categories?new=1").text
    # a new one: the name first, the key made from it (by the page's script, or here without it)
    assert html.index('name="label"') < html.index('name="key"') and "data-key-source" in html
    assert "0 von 4000 Zeichen" in html and 'placeholder="Gehört hierher: …&#10;Gehört nicht hierher: …' in html
    r = client.post("/ui/m/privat/categories", data={"csrf": _csrf(html), "new": "1", "key": "", "label": "Vereine & Clubs",
                                                     "description": "Vereinspost", "star": "never"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/privat/categories?cat=vereine_clubs"
    cat = _box(setup).cfg.categories["vereine_clubs"]
    assert cat.label == "Vereine & Clubs" and not cat.flag and not cat.flag_on_action

    # one there: the key one quiet line, renamed under Maintenance; the star one choice
    html = client.get("/ui/m/privat/categories?cat=finanzen").text
    assert '<code>finanzen</code> · <a href="/ui/m/privat/maintenance?category=finanzen#rename-category">' in html
    assert 'name="star" value="action" checked' in html and "27 von 4000 Zeichen" in html
    form = {"csrf": _csrf(html), "key": "finanzen", "description": "Rechnungen und Kontoauszüge"}
    client.post("/ui/m/privat/categories", data={**form, "star": "always"})
    assert _box(setup).cfg.categories["finanzen"].flag
    client.post("/ui/m/privat/categories", data={**form, "star": "action"})
    cat = _box(setup).cfg.categories["finanzen"]
    assert not cat.flag and cat.flag_on_action and "flag_on_action" not in _raw(setup)["categories"]["finanzen"]
    html = client.get("/ui/m/privat/maintenance?category=werbung").text
    assert 'id="rename-category"' in html and '<option value="werbung" selected>' in html


def test_move_category(setup):
    from email_sorter.web.editing import move_category
    shared = setup / "config.toml"
    save_category(_box(setup), shared, "vereine", CAT_FORM, create=True)
    move_category(_box(setup), shared, "vereine", -1)
    assert list(_box(setup).cfg.categories) == ["finanzen", "vereine", "werbung"]
    move_category(_box(setup), shared, "finanzen", -1)  # already first: nothing
    move_category(_box(setup), shared, "finanzen", 1)
    assert list(_box(setup).cfg.categories) == ["vereine", "finanzen", "werbung"]
    text = (setup / "mailboxes" / "privat" / "mailbox.toml").read_text(encoding="utf-8")
    assert "# Privates Postfach" in text and "min_confidence = 0.70   # darunter bleibt die Mail liegen" in text
    werbung = _box(setup).cfg.categories["werbung"]
    assert werbung.track_expiry and not werbung.flag_on_action  # each category keeps what it had
    with pytest.raises(EditError, match="gibt es nicht"):
        move_category(_box(setup), shared, "gibtsnicht", 1)


def test_categories_list_and_order(client, setup):
    save_sender_rules(_box(setup), setup / "config.toml", [("@shop.de", "werbung")])
    html = client.get("/ui/m/privat/categories?cat=werbung").text
    # marks after the name: the star when action is needed (finanzen), expiry (werbung), with their meaning
    assert 'title="Stern: Bei Handlungsbedarf"' in html and 'title="Ablaufdatum verfolgen"' in html
    # the count opens the mails; "+ Neue Kategorie" below the list
    assert '<a href="/ui/m/privat/mails?category=werbung&amp;period=30d"' in html
    assert 'class="cats-foot"><a class="btn" href="/ui/m/privat/categories?new=1#edit">+ Neue Kategorie</a>' in html
    # a category used by a rule: the rules a link away; the order: up (werbung is last: not down)
    assert 'Von <a href="/ui/m/privat/rules#sender-rules">Absender-Regeln</a> verwendet' in html
    assert 'value="-1"' in html and 'name="step" value="1" disabled' in html
    r = client.post("/ui/m/privat/categories/move", data={"csrf": _csrf(html), "key": "werbung", "step": "-1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/privat/categories?cat=werbung#edit"
    assert list(_box(setup).cfg.categories) == ["werbung", "finanzen"]


def test_categories_page_links_folders_and_the_test(client, setup):
    html = client.get("/ui/m/privat/categories?cat=finanzen").text
    # a click in the list goes on to the editor where it is below the list (#edit, the page's script)
    assert 'href="/ui/m/privat/categories?cat=werbung#edit"' in html and "?new=1#edit" in html
    # the folders known offered, a new one noted (by the script); empty: the inbox
    assert '<datalist id="known-folders">' in html and '<option value="INBOX/Finanzen">' in html
    assert html.count('list="known-folders"') == 2 and 'placeholder="leer: bleibt im Posteingang"' in html
    assert "Neuer Ordner – wird beim ersten Lauf auf dem Server angelegt." in html
    # the model test like the other tests, right below the description
    test = html.index('formaction="/ui/m/privat/categories/test"')
    assert html.index("desc-count") < test < html.index('name="folder"')
    assert 'class="btn tonal" type="submit" formaction="/ui/m/privat/categories/test"' in html
    assert "Verschiebt nichts, speichert nichts" in html


def test_forms_need_csrf_token(client, setup):
    r = client.post("/ui/m/privat/categories", data={"csrf": "falsch", "key": "finanzen", "description": "x"})
    assert r.status_code == 403
    assert _box(setup).cfg.categories["finanzen"].description == "Rechnungen und Kontoauszüge"


def test_help_tips_with_quotes_and_brackets_stay_whole(client):
    html = client.get("/ui/settings").text  # translations count as safe HTML: the tip escapes them anyway
    assert 'data-tip="Der Absender der Mails, z. B. „Sortroom &lt;ich@example.com&gt;“. Viele Anbieter' in html


def test_category_error_is_shown(client):
    html = client.get("/ui/m/privat/categories?new=1").text
    r = client.post("/ui/m/privat/categories", data={"csrf": _csrf(html), "new": "1", "key": "finanzen",
                                                     "description": "x"})
    assert r.status_code == 422 and "gibt es schon" in r.text
    assert "data-dirty" in r.text  # what was entered came back, not saved


def test_dry_run_of_the_deletion_rules_in_the_page(client, setup, monkeypatch):
    import logging
    from email_sorter.web import editor
    seen = []

    def fake_cleanup(cfg, creds, workspace, live):
        seen.append(([(r.folder, r.days, r.only_read) for r in cfg.delete_rules], live))
        logging.getLogger("email_sorter.cleanup").info("INBOX/Werbung: 2 mail(s) older than 30 day(s)")
        return {"exit_code": 0, "summary": "2 Mail(s) würden in den Papierkorb gehen – Probelauf, nichts geändert"}

    monkeypatch.setattr(editor, "run_cleanup", fake_cleanup)
    html = client.get("/ui/m/privat/rules").text
    assert 'formaction="/ui/m/privat/rules/dry-run"' in html
    mailbox_toml = (setup / "mailboxes" / "privat" / "mailbox.toml").read_text(encoding="utf-8")
    form = {"csrf": _csrf(html), "rows": "0", "drows": "1", "dfolder_0": "INBOX/Werbung", "ddays_0": "30",
            "dread_0": "1"}
    json = {"Accept": "application/json"}  # as the page's script asks
    r = client.post("/ui/m/privat/rules/dry-run", data=form, headers=json)
    assert r.json()["tone"] == "err" and seen == []                          # no login stored yet
    from email_sorter.web.editing import write_secrets
    write_secrets(setup / "mailboxes" / "privat" / "secrets.toml", "imap", {"user": "u", "password": "p"})
    r = client.post("/ui/m/privat/rules/dry-run", data=form, headers=json)
    assert seen == [([("INBOX/Werbung", 30, True)], False)]               # the rules in the page, not live
    assert r.json() == {"tone": "ok", "text": "2 Mail(s) würden in den Papierkorb gehen – Probelauf, nichts geändert",
                        "field": None, "details": "INBOX/Werbung: 2 mail(s) older than 30 day(s)"}
    assert (setup / "mailboxes" / "privat" / "mailbox.toml").read_text(encoding="utf-8") == mailbox_toml  # unsaved

    r = client.post("/ui/m/privat/rules/dry-run", data={**form, "dfolder_0": "INBOX"}, headers=json)
    assert r.json()["tone"] == "err" and "Posteingang" in r.json()["text"] and len(seen) == 1
    r = client.post("/ui/m/privat/rules/dry-run", data={**form, "drows": "0"}, headers=json)
    assert "keine Lösch-Regel" in r.json()["text"] and len(seen) == 1
    # without JavaScript the page comes back with the result and the rules as they were in the page
    r = client.post("/ui/m/privat/rules/dry-run", data=form)
    assert "nichts geändert" in r.text and 'name="dfolder_0" value="INBOX/Werbung"' in r.text


def test_rules_page(client, setup):
    html = client.get("/ui/m/privat/rules").text
    assert 'href="/ui/m/privat/rules" aria-current="page"' in html
    assert "scanner@brother.com" in html and html.count('name="match_') == 2  # the rule and the row template
    assert '<template id="rule-row">' in html and '<template id="delete-row">' in html and 'name="match___i__"' in html
    assert "Keine Lösch-Regeln" in html and '<option value="INBOX/Werbung">' in html  # folders are suggested
    settings = client.get("/ui/m/privat/settings").text
    assert 'name="match_' not in settings and 'name="dfolder_' not in settings  # no longer on the settings page

    # both lists in one form: row 0 of the sender rules removed in the page, an empty row added
    form = {"csrf": _csrf(html), "rows": "3", "match_1": "@shop.de", "action_1": "werbung", "match_2": "",
            "action_2": "inbox", "drows": "3", "dfolder_0": "INBOX/Werbung", "ddays_0": "30", "dread_0": "1",
            "dfolder_1": "", "ddays_1": "7", "dfolder_2": "INBOX/Werbung/Abgelaufen", "ddays_2": "7", "dstar_2": "1"}
    r = client.post("/ui/m/privat/rules", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/privat/rules"
    cfg = _box(setup).cfg
    assert [(x.match, x.action) for x in cfg.sender_rules] == [("@shop.de", "werbung")]
    assert [(x.folder, x.days, x.only_read, x.starred) for x in cfg.delete_rules] == [
        ("INBOX/Werbung", 30, True, False), ("INBOX/Werbung/Abgelaufen", 7, False, True)]
    text = (setup / "mailboxes" / "privat" / "mailbox.toml").read_text(encoding="utf-8")
    assert '[[delete_rules]]\nfolder = "INBOX/Werbung"\ndays = 30\nonly_read = true' in text
    html = client.get("/ui/m/privat/rules").text
    assert 'name="dfolder_1" value="INBOX/Werbung/Abgelaufen"' in html

    # an error saves neither list, and the page keeps what was entered
    for bad, message in (({"dfolder_0": "INBOX", "ddays_0": "3"}, "Posteingang"),
                         ({"dfolder_0": "X", "ddays_0": "0"}, "ganze Zahl")):
        r = client.post("/ui/m/privat/rules", data={"csrf": _csrf(html), "rows": "1", "match_0": "@neu.de",
                                                    "action_0": "inbox", "drows": "1", **bad})
        assert r.status_code == 422 and message in r.text and 'value="@neu.de"' in r.text
        assert [(x.match, x.action) for x in _box(setup).cfg.sender_rules] == [("@shop.de", "werbung")]
    client.post("/ui/m/privat/rules", data={"csrf": _csrf(html), "rows": "0", "drows": "0"})
    assert _box(setup).cfg.sender_rules == () and _box(setup).cfg.delete_rules == ()
    assert "delete_rules" not in (setup / "mailboxes" / "privat" / "mailbox.toml").read_text(encoding="utf-8")


def test_read_only_mailbox_page(client, setup):
    path = setup / "mailboxes" / "privat" / "mailbox.toml"
    os.chmod(path, stat.S_IREAD)
    try:
        html = client.get("/ui/m/privat/settings").text
        assert "Nur lesen" in html and "<fieldset disabled" in html and "Einstellungen speichern" not in html
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def test_trial_job_page(client, setup, monkeypatch):
    from email_sorter.web.editing import write_secrets
    write_secrets(setup / "secrets.toml", "classifier", {"api_key": "k"})
    write_secrets(setup / "mailboxes" / "privat" / "secrets.toml", "imap", {"user": "u", "password": "p"})
    rows = [trial.TrialRow(None, "a@b", "Rabatt", "werbung", "werbung", 0.9),
            trial.TrialRow(None, "c@d", "Rechnung", "finanzen", "werbung", 0.6),
            trial.TrialRow(None, "e@f", "Weg", "werbung", error="nicht mehr im Ordner")]
    monkeypatch.setattr(editor, "run_trial", lambda *a, **k: (rows, 0.0002))
    editor._trials.clear()
    html = client.get("/ui/m/privat/categories?cat=werbung").text
    assert "data-dirty" not in html
    # other fields changed but not saved go along with the test
    form = {"csrf": _csrf(html), "key": "werbung", "description": "Entwurf", "label": "Angebote",
            "folder": "INBOX/Angebote", "flag": "on", "track_expiry": "on", "expired_folder": "INBOX/Alt"}
    r = client.post("/ui/m/privat/categories/test", data=form, follow_redirects=False)
    assert r.status_code == 303
    job = r.headers["location"].rsplit("/", 1)[1]
    deadline = time.monotonic() + 5
    while editor._trials[job]["status"] == "running" and time.monotonic() < deadline:
        pass  # the worker thread finishes at once
    html = client.get(r.headers["location"]).text
    assert "Kämen neu dazu" in html and "Rechnung" in html and "nicht mehr im Ordner" in html
    back = f"/ui/m/privat/categories?cat=werbung&amp;draft={job}"
    # "Back to the form" and "Put the draft into the form"; on a tablet or phone on to the editor (#edit)
    assert html.count(f'href="{back}#edit"') == 2
    # back in the form: every field as sent to the test, marked as not saved
    html = client.get(back.replace("&amp;", "&")).text
    assert ">Entwurf</textarea>" in html and 'value="Angebote"' in html and 'value="INBOX/Angebote"' in html
    assert 'name="star" value="always" checked' in html and 'name="track_expiry" id="track_expiry" checked' in html
    assert 'value="INBOX/Alt"' in html and "data-dirty" in html
    assert _box(setup).cfg.categories["werbung"].label == "Werbung"  # nothing saved
    html = client.get("/ui/m/privat/categories?cat=werbung&draft=weg").text  # e.g. after a restart
    assert "Der Entwurf aus dem Test ist nicht mehr da" in html and "data-dirty" not in html

    # a test already running: the form stays as it was entered
    editor._trials[job]["status"] = "running"
    r = client.post("/ui/m/privat/categories/test", data={**form, "description": "Noch einer"})
    assert r.status_code == 409 and "Es läuft schon ein Test" in r.text
    assert ">Noch einer</textarea>" in r.text and 'value="Angebote"' in r.text and "data-dirty" in r.text
    editor._trials[job]["status"] = "done"


def test_schedule_settings_saved(client, setup):
    html = client.get("/ui/m/privat/settings").text
    assert "Zeitplan" in html and 'name="schedule_minutes" min="1" max="1440" step="1" value="10"' in html
    # number inputs take a dot in value=; the browser shows the local decimal comma
    assert re.search(r'type="number" name="min_confidence" [^>]*value="0\.7"', html)
    form = {"csrf": _csrf(html), "name": "Privat", "imap_host": "imap.example.de", "imap_port": "993",
            "source_folder": "INBOX", "min_confidence": "0.7", "action_flag_threshold": "0,8",
            "expiry_threshold": "0,7", "min_age_hours": "24", "lookback_days": "7", "max_per_run": "200",
            "expired_folder": "", "schedule_minutes": "30", "reconcile_hours": "12"}  # checkboxes not sent: off
    assert client.post("/ui/m/privat/settings", data=form, follow_redirects=False).status_code == 303
    cfg = _box(setup).cfg
    assert not cfg.schedule_enabled and cfg.schedule_minutes == 30
    assert not cfg.reconcile_enabled and cfg.reconcile_hours == 12
    assert _raw(setup)["schedule"] == {"enabled": False, "interval_minutes": 30, "reconcile_enabled": False,
                                       "reconcile_hours": 12}
    assert "Zeitplan aus" in client.get("/ui/m/privat").text


# ---------------------------------------------------------------- the folder name (id) of a mailbox

def test_mailbox_ids_from_names():
    from email_sorter.config import mailbox_id_for
    assert mailbox_id_for("mh@hoenes.de") == "mh-hoenes-de"
    assert mailbox_id_for("Büro Müller") == "buero-mueller" and mailbox_id_for("Café") == "cafe"
    assert mailbox_id_for("  --Privat!! ") == "privat" and mailbox_id_for("") == "mailbox"
    assert mailbox_id_for("Privat", {"privat"}) == "privat-2" and mailbox_id_for("Privat", {"privat", "privat-2"}) == "privat-3"
    assert len(mailbox_id_for("x" * 60, {"x" * 40})) == 40


def _settings_form(html, **changes):
    return {"csrf": _csrf(html), "name": "Privat", "imap_host": "imap.example.de", "imap_port": "993",
            "source_folder": "INBOX", "min_confidence": "0.7", "action_flag_threshold": "0.8",
            "expiry_threshold": "0.7", "min_age_hours": "0", "lookback_days": "7", "max_per_run": "200",
            "expired_folder": "", "schedule_minutes": "10", **changes}


def test_a_new_display_name_keeps_the_folder(client, setup):
    html = client.get("/ui/m/privat/settings").text
    assert 'name="box_id" value="privat"' in html and 'data-current="privat"' in html
    r = client.post("/ui/m/privat/settings", data=_settings_form(html, name="mh@hoenes.de", box_id="privat"),
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/privat/settings"
    assert load_mailboxes(setup, setup / "config.toml")["privat"].name == "mh@hoenes.de"


def test_a_new_folder_name_moves_the_folder(client, setup):
    Store(setup / "mailboxes" / "privat" / "data" / "state.db").close()  # the log moves along
    html = client.get("/ui/m/privat/settings").text
    r = client.post("/ui/m/privat/settings", data=_settings_form(html, box_id="Mh-Hoenes"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/mh-hoenes/settings"
    assert not (setup / "mailboxes" / "privat").exists()
    assert (setup / "mailboxes" / "mh-hoenes" / "data" / "state.db").exists()
    assert load_mailboxes(setup, setup / "config.toml")["mh-hoenes"].name == "Privat"  # the name stays
    assert "Ordner des Postfachs heißt jetzt mailboxes/mh-hoenes" in client.get(r.headers["location"]).text
    assert client.get("/ui/m/privat").status_code == 404


@pytest.mark.parametrize("box_id, message", [("-arbeit", "nur Kleinbuchstaben"), ("büro", "nur Kleinbuchstaben"),
                                             ("x" * 41, "zu lang"), (".locks", "nur Kleinbuchstaben")])
def test_invalid_folder_names_are_refused(client, setup, box_id, message):
    html = client.get("/ui/m/privat/settings").text
    r = client.post("/ui/m/privat/settings", data=_settings_form(html, box_id=box_id))
    assert r.status_code == 422 and message in r.text
    assert (setup / "mailboxes" / "privat").exists()


def test_a_taken_folder_name_is_refused(client, setup):
    (setup / "mailboxes" / "arbeit").mkdir()
    html = client.get("/ui/m/privat/settings").text
    r = client.post("/ui/m/privat/settings", data=_settings_form(html, box_id="arbeit"))
    assert r.status_code == 422 and "mailboxes/arbeit gibt es schon" in r.text


def test_an_error_shows_at_its_field(client, setup):
    html = client.get("/ui/m/privat/settings").text
    # folder fields suggest the folders there are
    assert '<datalist id="known-folders">' in html and '<option value="INBOX">' in html
    assert re.search(r'name="expired_folder"[^>]*list="known-folders"', html)
    for field, value in (("lookback_days", "999"), ("box_id", "-arbeit")):
        r = client.post("/ui/m/privat/settings", data=_settings_form(html, **{field: value}))
        assert r.status_code == 422 and f'aria-describedby="error-{field}" autofocus' in r.text
        assert f'id="error-{field}">' in r.text and 'class="banner err"' not in r.text  # at the field, not on top
        assert "data-dirty" in r.text  # what was entered is not saved: unsaved.js says so
    assert '<span class="static rename-view" hidden>' in html and "data-dirty" not in html  # "Rename" (JS)


def test_rename_is_refused_while_the_mailbox_is_busy(client, setup):
    from email_sorter.runtime import single_instance
    with single_instance(_box(setup).lock_path) as held:  # a run of this very process holds the lock
        assert held
        html = client.get("/ui/m/privat/settings").text
        r = client.post("/ui/m/privat/settings", data=_settings_form(html, name="Arbeit", box_id="arbeit"))
        assert r.status_code == 422 and "weder Name noch Ordner" in r.text
    assert (setup / "mailboxes" / "privat").exists() and not (setup / "mailboxes" / "arbeit").exists()
    assert load_mailboxes(setup, setup / "config.toml")["privat"].name == "Privat"  # nothing saved


def test_reconcile_status_in_the_settings(client, setup, monkeypatch):
    from datetime import datetime, timedelta
    from email_sorter.scheduler import Scheduler
    from email_sorter.store import Store
    monkeypatch.setattr(Scheduler, "active", property(lambda s: True))
    html = client.get("/ui/m/privat/settings").text
    assert 'name="reconcile_enabled" value="1" checked' in html and 'name="reconcile_hours"' in html
    assert "startet in Kürze" in html.split("Automatisch abgleichen", 1)[1]  # never reconciled yet
    last = datetime.now() - timedelta(hours=3)
    store = Store(setup / "mailboxes" / "privat" / "data" / "state.db")
    store.set_meta("last_reconcile", last.isoformat(timespec="seconds"))
    store.close()
    html = client.get("/ui/m/privat/settings").text
    assert "zuletzt vor 3 Std · nächster" in html


def test_concurrent_edits_of_a_mailbox_keep_both(setup, monkeypatch):
    import threading
    from email_sorter.web import editing
    shared = setup / "config.toml"
    first_read, go_on = threading.Event(), threading.Event()
    real_doc = editing._doc

    def slow_doc(box):  # the rules are read, then the category save starts while they are still unsaved
        doc = real_doc(box)
        if threading.current_thread().name == "rules":
            first_read.set()
            go_on.wait(2)
        return doc

    monkeypatch.setattr(editing, "_doc", slow_doc)
    rules = threading.Thread(target=save_sender_rules, name="rules",
                             args=(_box(setup), shared, [("@shop.de", "werbung")]))
    rules.start()
    first_read.wait(2)
    category = threading.Thread(target=save_category, args=(_box(setup), shared, "werbung",
                                                            {"description": "Neu", "folder": "INBOX/Werbung"}))
    category.start()
    go_on.set()
    rules.join()
    category.join()
    box = _box(setup)
    assert box.cfg.categories["werbung"].description == "Neu" and box.cfg.rule_for("x@shop.de")
