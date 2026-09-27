"""Logins and the API key set in the UI: stored in secrets.toml files, write-only in the pages."""
import os
import stat
import tomllib

import pytest

from email_sorter.config import ConfigError, credential_sources, load_credentials, load_mailboxes
from email_sorter.web import admin, editing, editor
from email_sorter.web.editing import EditError, save_settings, write_secrets

from test_admin import _box, _csrf, client, setup  # noqa: F401 - fixtures

SETTINGS = {"name": "Privat", "imap_host": "imap.example.de", "imap_port": "993", "source_folder": "INBOX",
            "min_confidence": "0.7", "action_flag_threshold": "0.8", "expiry_threshold": "0.7",
            "min_age_hours": "24", "lookback_days": "7", "max_per_run": "200", "expired_folder": "",
            "schedule_minutes": "10", "schedule_enabled": "1"}


def _secrets(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_stored_values_take_precedence_over_the_environment(setup):
    box = _box(setup)
    assert load_credentials(box).imap_user == "u"  # only .env so far
    write_secrets(box.secrets_path, "imap", {"user": "me@example.de", "password": "file-pw"})
    write_secrets(setup / "secrets.toml", "classifier", {"api_key": "file-key"})
    creds = load_credentials(_box(setup))
    assert (creds.imap_user, creds.imap_password, creds.classifier_api_key) == ("me@example.de", "file-pw", "file-key")
    assert {k: s.source for k, s in credential_sources(_box(setup)).items()} == dict.fromkeys(
        ("imap_user", "imap_password", "classifier_api_key"), "file")
    assert "file-pw" not in repr(creds) and "file-key" not in repr(credential_sources(box))

    write_secrets(box.secrets_path, "imap", {"password": None})  # per value: the password falls back to .env
    creds = load_credentials(_box(setup))
    assert (creds.imap_user, creds.imap_password) == ("me@example.de", "p")
    write_secrets(box.secrets_path, "imap", {"user": None})
    assert not box.secrets_path.exists()  # nothing left: the file is gone


def test_missing_credentials_are_named_without_values(setup, monkeypatch):
    monkeypatch.delenv("IMAP_PASSWORD")
    monkeypatch.delenv("CLASSIFIER_API_KEY")
    with pytest.raises(ConfigError) as e:
        load_credentials(_box(setup))
    assert "IMAP password (mailbox settings or IMAP_PASSWORD in .env)" in str(e.value)
    assert "API key (Global settings or CLASSIFIER_API_KEY in .env)" in str(e.value)
    assert "IMAP user" not in str(e.value)


def test_broken_secrets_file(setup):
    box = _box(setup)
    box.secrets_path.write_text('[imap]\npassword = "unterminated', encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML") as e:
        load_credentials(box)
    assert "unterminated" not in str(e.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_secrets_file_is_private(setup):
    path = _box(setup).secrets_path
    old = os.umask(0)
    try:
        write_secrets(path, "imap", {"user": "u", "password": "p"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_secrets_file_has_no_copies(setup):
    path = _box(setup).secrets_path
    write_secrets(path, "imap", {"user": "u", "password": "one"})
    write_secrets(path, "imap", {"password": "two"})
    assert _secrets(path)["imap"] == {"user": "u", "password": "two"}
    assert sorted(p.name for p in path.parent.iterdir() if p.name.startswith("secrets")) == ["secrets.toml"]


def test_login_moves_with_the_mailbox_folder(setup):
    box = _box(setup)
    new_id = save_settings(box, setup / "config.toml", {**SETTINGS, "name": "Büro", "imap_user": "buero@example.de",
                                                         "imap_password": "pw"})
    assert new_id == "buero"
    assert _secrets(setup / "mailboxes" / "buero" / "secrets.toml")["imap"] == {"user": "buero@example.de",
                                                                                "password": "pw"}
    assert not (setup / "mailboxes" / "privat").exists()


def test_login_fields_are_checked(setup):
    with pytest.raises(EditError, match="500 Zeichen") as e:
        save_settings(_box(setup), setup / "config.toml", {**SETTINGS, "imap_user": "u", "imap_password": "a\nb"})
    assert "a\nb" not in str(e.value)
    assert not _box(setup).secrets_path.exists()


# ---------------------------------------------------------------- pages

def test_settings_login_is_write_only(client, setup):
    html = client.get("/ui/m/privat/settings").text
    assert 'name="imap_password"' in html and "aus .env" in html
    form = {**SETTINGS, "csrf": _csrf(html), "imap_user": "me@example.de", "imap_password": "Geheim-123"}
    assert client.post("/ui/m/privat/settings", data=form, follow_redirects=False).status_code == 303
    path = setup / "mailboxes" / "privat" / "secrets.toml"
    assert _secrets(path)["imap"] == {"user": "me@example.de", "password": "Geheim-123"}

    html = client.get("/ui/m/privat/settings").text
    assert "Geheim-123" not in html and 'value="me@example.de"' in html and "gespeichert" in html
    assert "Gespeicherte Zugangsdaten entfernen" in html
    assert "Geheim-123" not in client.get("/ui/m/privat/maintenance").text

    # empty password: the stored one stays
    assert client.post("/ui/m/privat/settings", data={**form, "imap_password": ""},
                       follow_redirects=False).status_code == 303
    assert _secrets(path)["imap"]["password"] == "Geheim-123"

    # an error elsewhere: nothing stored, and the typed password is not echoed back
    r = client.post("/ui/m/privat/settings", data={**form, "imap_password": "Neu-456", "imap_port": "abc"})
    assert r.status_code == 422 and "Neu-456" not in r.text and "Bitte erneut eingeben" in r.text
    assert _secrets(path)["imap"]["password"] == "Geheim-123"

    r = client.post("/ui/m/privat/login/clear", data={"csrf": form["csrf"]}, follow_redirects=False)
    assert r.status_code == 303 and not path.exists()
    assert "Gespeicherte Zugangsdaten entfernt" in client.get(r.headers["location"]).text


def test_new_mailbox_has_no_env_fallback(client, setup):
    html = client.get("/ui/mailboxes/new").text
    form = {"csrf": _csrf(html), "name": "Work", "imap_host": "imap.example.com", "imap_port": "993",
            "source_folder": "INBOX", "imap_user": "", "imap_password": "Geheim-123", "template": "privat"}
    r = client.post("/ui/mailboxes/new", data=form)
    assert r.status_code == 422 and "Geheim-123" not in r.text and "Bitte erneut eingeben" in r.text
    assert not (setup / "mailboxes" / "work").exists()
    assert client.post("/ui/mailboxes/new", data={**form, "imap_user": "w@example.com"},
                       follow_redirects=False).status_code == 303
    box = load_mailboxes(setup, setup / "config.toml")["work"]
    write_secrets(box.secrets_path, "imap", {"user": None, "password": None})
    with pytest.raises(ConfigError, match=r"IMAP user \(mailbox settings\)"):  # IMAP_USER is set, but not for it
        load_credentials(box)


def test_shared_api_key_is_write_only(client, setup):
    html = client.get("/ui/settings").text
    assert 'name="api_key"' in html and "CLASSIFIER_API_KEY" in html
    form = {"csrf": _csrf(html), "endpoint": "https://example.invalid/decisions", "model": "example/model-1",
            "max_body_chars": "3000", "timeout_seconds": "20", "min_interval_seconds": "0", "language": "de", "api_key": "sk-Geheim"}
    assert client.post("/ui/settings", data=form, follow_redirects=False).status_code == 303
    assert _secrets(setup / "secrets.toml") == {"classifier": {"api_key": "sk-Geheim"}}
    html = client.get("/ui/settings").text
    assert "sk-Geheim" not in html and "gespeichert" in html and 'placeholder="unverändert"' in html
    assert load_credentials(_box(setup)).classifier_api_key == "sk-Geheim"

    r = client.post("/ui/settings", data={**form, "api_key": "sk-Neu", "endpoint": "http://unsicher"})
    assert r.status_code == 422 and "sk-Neu" not in r.text
    assert _secrets(setup / "secrets.toml")["classifier"]["api_key"] == "sk-Geheim"

    assert client.post("/ui/settings/key/clear", data={"csrf": form["csrf"]}, follow_redirects=False).status_code == 303
    assert not (setup / "secrets.toml").exists()


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
