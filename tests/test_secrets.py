"""Logins and the API key set in the UI: stored in secrets.toml files, write-only in the pages."""
import os
import stat
import tomllib

import pytest

from email_sorter.config import ConfigError, load_credentials, load_mailboxes, stored_credentials
from email_sorter.web import admin, editing, editor
from email_sorter.web.editing import EditError, save_settings, write_secrets

from test_admin import _box, _csrf, client, setup  # noqa: F401 - fixtures; setup stores u / p and the key k

SETTINGS = {"name": "Privat", "imap_host": "imap.example.de", "imap_port": "993", "source_folder": "INBOX",
            "min_confidence": "0.7", "action_flag_threshold": "0.8", "expiry_threshold": "0.7",
            "min_age_hours": "24", "lookback_days": "7", "max_per_run": "200", "expired_folder": "",
            "schedule_minutes": "10", "schedule_enabled": "1"}


def _secrets(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_credentials_come_only_from_the_secrets_files(setup, monkeypatch):
    for name, value in (("IMAP_USER", "env-user"), ("IMAP_PASSWORD", "env-pw"), ("CLASSIFIER_API_KEY", "env-key")):
        monkeypatch.setenv(name, value)  # the environment plays no part
    box = _box(setup)
    creds = load_credentials(box)
    assert (creds.imap_user, creds.imap_password, creds.classifier_api_key) == ("u", "p", "k")
    assert "'p'" not in repr(creds) and "'k'" not in repr(creds)

    write_secrets(box.secrets_path, "imap", {"password": None})
    with pytest.raises(ConfigError, match=r"not set: IMAP password \(mailbox settings\)$"):
        load_credentials(box)
    write_secrets(box.secrets_path, "imap", {"user": None})
    assert not box.secrets_path.exists()  # nothing left: the file is gone
    (setup / "secrets.toml").unlink()
    with pytest.raises(ConfigError) as e:
        load_credentials(box)
    assert str(e.value) == ("not set: IMAP user (mailbox settings); IMAP password (mailbox settings); "
                            "API key (Global settings)")
    assert stored_credentials(box) == dict.fromkeys(("imap_user", "imap_password", "classifier_api_key"), "")


def test_broken_secrets_file(setup):
    box = _box(setup)
    box.secrets_path.write_text('[imap]\npassword = "unterminated', encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML") as e:
        load_credentials(box)
    assert "unterminated" not in str(e.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_secrets_file_is_private(setup):
    path = _box(setup).secrets_path
    path.unlink()
    old = os.umask(0)
    try:
        write_secrets(path, "imap", {"user": "u", "password": "p"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_secrets_file_has_no_copies(setup):
    path = _box(setup).secrets_path
    write_secrets(path, "imap", {"password": "two"})
    assert _secrets(path)["imap"] == {"user": "u", "password": "two"}
    assert sorted(p.name for p in path.parent.iterdir() if p.name.startswith("secrets")) == ["secrets.toml"]


def test_login_moves_with_the_mailbox_folder(setup):
    new_id = save_settings(_box(setup), setup / "config.toml", {**SETTINGS, "name": "Büro",
                                                                "imap_user": "buero@example.de", "imap_password": "pw"})
    assert new_id == "buero"
    assert _secrets(setup / "mailboxes" / "buero" / "secrets.toml")["imap"] == {"user": "buero@example.de",
                                                                                "password": "pw"}
    assert not (setup / "mailboxes" / "privat").exists()


def test_login_fields_are_checked(setup):
    with pytest.raises(EditError, match="500 Zeichen") as e:
        save_settings(_box(setup), setup / "config.toml", {**SETTINGS, "imap_user": "x", "imap_password": "a\nb"})
    assert "a\nb" not in str(e.value)
    assert _secrets(_box(setup).secrets_path)["imap"] == {"user": "u", "password": "p"}


# ---------------------------------------------------------------- pages

def test_settings_login_is_write_only(client, setup):
    html = client.get("/ui/m/privat/settings").text
    assert 'name="imap_user" value="u"' in html and 'name="imap_password"' in html and "gesetzt" in html
    form = {**SETTINGS, "csrf": _csrf(html), "imap_user": "me@example.de", "imap_password": "Geheim-123"}
    assert client.post("/ui/m/privat/settings", data=form, follow_redirects=False).status_code == 303
    path = setup / "mailboxes" / "privat" / "secrets.toml"
    assert _secrets(path)["imap"] == {"user": "me@example.de", "password": "Geheim-123"}

    html = client.get("/ui/m/privat/settings").text
    assert "Geheim-123" not in html and 'value="me@example.de"' in html and 'placeholder="unverändert"' in html
    assert "Geheim-123" not in client.get("/ui/m/privat/maintenance").text

    # empty password: the stored one stays
    assert client.post("/ui/m/privat/settings", data={**form, "imap_password": ""},
                       follow_redirects=False).status_code == 303
    assert _secrets(path)["imap"]["password"] == "Geheim-123"

    # an error elsewhere: nothing stored, and the typed password is not echoed back
    r = client.post("/ui/m/privat/settings", data={**form, "imap_password": "Neu-456", "imap_port": "abc"})
    assert r.status_code == 422 and "Neu-456" not in r.text and "Bitte erneut eingeben" in r.text
    assert _secrets(path)["imap"]["password"] == "Geheim-123"


def test_missing_login_is_shown(client, setup):
    (setup / "mailboxes" / "privat" / "secrets.toml").unlink()
    html = client.get("/ui/m/privat/settings").text
    assert html.count('<span class="pill err">fehlt</span>') == 2  # user and password
    assert 'placeholder="unverändert"' not in html


def test_new_mailbox_stores_its_login(client, setup):
    html = client.get("/ui/mailboxes/new").text
    form = {"csrf": _csrf(html), "name": "Work", "imap_host": "imap.example.com", "imap_port": "993",
            "source_folder": "INBOX", "imap_user": "", "imap_password": "Geheim-123", "template": "privat"}
    r = client.post("/ui/mailboxes/new", data=form)
    assert r.status_code == 422 and "Geheim-123" not in r.text and "Bitte erneut eingeben" in r.text
    assert not (setup / "mailboxes" / "work").exists()
    assert client.post("/ui/mailboxes/new", data={**form, "imap_user": "w@example.com"},
                       follow_redirects=False).status_code == 303
    creds = load_credentials(load_mailboxes(setup, setup / "config.toml")["work"])
    assert (creds.imap_user, creds.imap_password) == ("w@example.com", "Geheim-123")


def test_shared_api_key_is_write_only(client, setup):
    html = client.get("/ui/settings").text
    assert 'name="api_key"' in html and 'placeholder="unverändert"' in html
    form = {"csrf": _csrf(html), "endpoint": "https://example.invalid/decisions", "model": "example/model-1",
            "max_body_chars": "3000", "timeout_seconds": "20", "min_interval_seconds": "0", "language": "de",
            "api_key": "sk-Geheim"}
    assert client.post("/ui/settings", data=form, follow_redirects=False).status_code == 303
    assert _secrets(setup / "secrets.toml") == {"classifier": {"api_key": "sk-Geheim"}}
    html = client.get("/ui/settings").text
    assert "sk-Geheim" not in html and "gesetzt" in html
    assert load_credentials(_box(setup)).classifier_api_key == "sk-Geheim"

    assert client.post("/ui/settings", data={**form, "api_key": ""}, follow_redirects=False).status_code == 303
    assert _secrets(setup / "secrets.toml")["classifier"]["api_key"] == "sk-Geheim"  # empty keeps it

    r = client.post("/ui/settings", data={**form, "api_key": "sk-Neu", "endpoint": "http://unsicher"})
    assert r.status_code == 422 and "sk-Neu" not in r.text
    assert _secrets(setup / "secrets.toml")["classifier"]["api_key"] == "sk-Geheim"


def test_read_only_secrets(client, setup, monkeypatch):
    for module in (editing, editor, admin):
        monkeypatch.setattr(module, "secrets_writable", lambda path: False)
    html = client.get("/ui/m/privat/settings").text
    assert "nicht beschreibbar" in html and 'name="imap_password" autocomplete="new-password" disabled' in html
    r = client.post("/ui/m/privat/settings", data={**SETTINGS, "csrf": _csrf(html), "imap_user": "x",
                                                   "imap_password": "pw"})
    assert r.status_code == 422 and "nicht beschreibbar" in r.text
    html = client.get("/ui/settings").text
    assert 'name="api_key" autocomplete="new-password" spellcheck="false" disabled' in html


def test_save_and_check_tests_the_new_login(client, setup, monkeypatch):
    import time
    from email_sorter import jobs
    seen = []
    monkeypatch.setattr(admin, "check_imap", lambda cfg, user, password, out: seen.append(password) or True)
    (setup / "secrets.toml").unlink()  # the mailbox check does not need the API key
    html = client.get("/ui/m/privat/settings").text
    assert 'name="then" value="check"' in html
    form = {**SETTINGS, "csrf": _csrf(html), "imap_user": "u", "imap_password": "Neu-789", "then": "check"}
    r = client.post("/ui/m/privat/settings", data=form, follow_redirects=False)
    assert r.status_code == 303 and "/ui/m/privat/jobs/" in r.headers["location"]
    job_id = r.headers["location"].rsplit("/", 1)[1]
    for _ in range(100):
        if jobs.get(job_id)["status"] != "running":
            break
        time.sleep(0.02)
    assert seen == ["Neu-789"] and jobs.get(job_id)["request"] == {}  # the password is not kept with the job
    assert "Neu-789" not in client.get(r.headers["location"]).text

    # an error: nothing saved, no check started
    r = client.post("/ui/m/privat/settings", data={**form, "imap_password": "x", "imap_port": "abc"})
    assert r.status_code == 422 and seen == ["Neu-789"]


def test_save_and_check_model(client, setup, monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, key, endpoint, model, **kw):
            calls.append(key)

        def decide(self, state, categories):
            from email_sorter.classifier import Decision
            assert "finanzen" in categories  # the standard categories of the UI language
            return Decision("finanzen", 0.93, {"finanzen": 0.93}, 0.1, 0.000021)

    monkeypatch.setattr(admin, "ClassifierClient", FakeClient)
    html = client.get("/ui/settings").text
    form = {"csrf": _csrf(html), "endpoint": "https://example.invalid/decisions", "model": "example/model-1",
            "max_body_chars": "3000", "timeout_seconds": "20", "min_interval_seconds": "0", "language": "de",
            "api_key": "sk-Neu", "then": "check"}
    r = client.post("/ui/settings", data=form)
    assert calls == ["sk-Neu"] and "Modell funktioniert" in r.text and "„finanzen“" in r.text
    assert "sk-Neu" not in r.text

    (setup / "secrets.toml").unlink()
    r = client.post("/ui/settings", data={**form, "api_key": ""})
    assert "API-Schlüssel ist noch nicht gesetzt" in r.text and calls == ["sk-Neu"]
