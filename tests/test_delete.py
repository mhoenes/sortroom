"""Deleting a mailbox: folder, jobs, Google revocation; in the UI, the HTTP API and the CLI."""
from types import SimpleNamespace

import pytest

from email_sorter import __main__ as cli, jobs, oauth
from email_sorter.config import write_secrets
from email_sorter.removal import MailboxBusy, delete_mailbox
from email_sorter.runtime import single_instance

from test_admin import _box, _csrf, client, setup  # noqa: F401 - fixtures
from test_api import AUTH, make_client  # noqa: F401 - fixture


@pytest.fixture
def revocations(monkeypatch):
    """Records revoke requests to Google; `status` is what Google answers."""
    seen = SimpleNamespace(calls=[], status=200)

    def post(url, data, timeout):
        seen.calls.append((url, dict(data)))
        return SimpleNamespace(status_code=seen.status)
    monkeypatch.setattr(oauth.requests, "post", post)
    return seen


def _as(setup, auth):
    path = setup / "mailboxes" / "privat" / "mailbox.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('host = "imap.example.de"',
                                                             f'host = "imap.example.de"\nauth = "{auth}"'),
                    encoding="utf-8")
    write_secrets(_box(setup).secrets_path, "oauth", {"refresh_token": "rt-1"})


def test_delete_a_password_mailbox(setup, revocations):
    box = _box(setup)
    jobs._jobs["old"] = {"id": "old", "mailbox": "privat", "status": "done"}
    assert delete_mailbox(box) == {"revoked": None}
    assert not box.workspace.exists() and "old" not in jobs._jobs
    assert revocations.calls == []


def test_a_busy_mailbox_is_not_deleted(setup):
    box = _box(setup)
    with single_instance(box.lock_path):
        with pytest.raises(MailboxBusy):
            delete_mailbox(box)
    assert box.workspace.exists()


def test_the_google_sign_in_is_revoked(setup, revocations):
    _as(setup, "google")
    assert delete_mailbox(_box(setup)) == {"revoked": True}
    assert revocations.calls == [("https://oauth2.googleapis.com/revoke", {"token": "rt-1"})]


def test_deleted_even_when_google_refuses_the_revocation(setup, revocations):
    _as(setup, "google")
    revocations.status = 400
    box = _box(setup)
    assert delete_mailbox(box) == {"revoked": False} and not box.workspace.exists()


def test_microsoft_has_nothing_to_revoke(setup, revocations):
    _as(setup, "microsoft")
    assert delete_mailbox(_box(setup)) == {"revoked": None} and revocations.calls == []


# ---------------------------------------------------------------- the admin UI

def test_delete_in_the_settings(client, setup, revocations):
    _as(setup, "google")
    html = client.get("/ui/m/privat/settings").text
    assert 'action="/ui/m/privat/delete"' in html and "Anmeldung bei Google wird dabei widerrufen" in html
    r = client.post("/ui/m/privat/delete", data={"csrf": _csrf(html), "confirm": "privat"}, follow_redirects=False)
    assert r.headers["location"] == "/ui/m/privat/settings" and (setup / "mailboxes" / "privat").exists()
    assert "Namen des Postfachs genau" in client.get(r.headers["location"]).text

    r = client.post("/ui/m/privat/delete", data={"csrf": _csrf(html), "confirm": " Privat "}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui"
    assert not (setup / "mailboxes" / "privat").exists()
    html = client.get("/ui").text
    assert "Postfach „Privat“ gelöscht" in html and "bei Google wurde widerrufen" in html
    assert "Noch kein Postfach" in html  # the last one: the empty state again


def test_delete_waits_for_a_running_job(client, setup, monkeypatch):
    html = client.get("/ui/m/privat/settings").text
    monkeypatch.setattr(jobs, "running", lambda box_id: True)
    r = client.post("/ui/m/privat/delete", data={"csrf": _csrf(html), "confirm": "Privat"}, follow_redirects=False)
    assert r.headers["location"] == "/ui/m/privat/settings" and (setup / "mailboxes" / "privat").exists()


# ---------------------------------------------------------------- HTTP API and CLI

def test_delete_via_the_api(make_client, tmp_path):
    c = make_client(("gmail", "privat"))
    for box in c.boxes.values():
        box.workspace.mkdir(parents=True)
    assert c.delete("/mailboxes/privat").status_code == 401
    assert c.delete("/mailboxes/nope", headers=AUTH).status_code == 404
    with single_instance(c.boxes["gmail"].lock_path):
        assert c.delete("/mailboxes/gmail", headers=AUTH).status_code == 409
    r = c.delete("/mailboxes/privat", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"deleted": "privat", "revoked": None}
    assert not (tmp_path / "privat").exists() and (tmp_path / "gmail").exists()


def test_delete_via_the_cli(setup, monkeypatch, capsys):
    monkeypatch.setattr(cli, "BASE_DIR", setup)
    args = ["--config", str(setup / "config.toml"), "--delete-mailbox", "privat"]
    monkeypatch.setattr("builtins.input", lambda prompt: "wrong")
    assert cli.main(args) == 1 and (setup / "mailboxes" / "privat").exists()
    monkeypatch.setattr("builtins.input", lambda prompt: "privat")
    assert cli.main(args) == 0 and not (setup / "mailboxes" / "privat").exists()
    assert 'Mailbox "Privat" deleted.' in capsys.readouterr().out
    assert cli.main(["--config", str(setup / "config.toml"), "--delete-mailbox", "privat", "--yes"]) == 2  # gone
