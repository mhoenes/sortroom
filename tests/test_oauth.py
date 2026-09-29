"""OAuth2 sign-in (XOAUTH2) for Google and Microsoft mailboxes."""
import base64
import hashlib
import re
import tomllib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from email_sorter import oauth
from email_sorter.config import ConfigError, load_credentials, write_secrets
from email_sorter.oauth import OAuthError, OAuthLogin

from test_admin import _box, _csrf, client, setup  # noqa: F401 - fixtures; privat has user "u", key "k"

GOOGLE_ID = "123-abc.apps.googleusercontent.com"
MS_ID = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
SETTINGS = {"name": "Privat", "imap_host": "imap.gmail.com", "imap_port": "993", "source_folder": "INBOX",
            "min_confidence": "0.7", "action_flag_threshold": "0.8", "expiry_threshold": "0.7",
            "min_age_hours": "24", "lookback_days": "7", "max_per_run": "200", "expired_folder": "",
            "imap_user": "u"}


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture
def token_server(monkeypatch):
    """Answers token requests; `answers` is a list consumed in order, `calls` records (url, data)."""
    server = SimpleNamespace(calls=[], answers=[])

    def post(url, data, timeout):
        server.calls.append((url, dict(data)))
        return server.answers.pop(0)
    monkeypatch.setattr(oauth.requests, "post", post)
    oauth._cache.clear()
    return server


def _secrets(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- the protocol

def test_authorize_url_and_pkce():
    verifier, challenge = oauth.pkce_pair()
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = oauth.authorize_url("google", GOOGLE_ID, "", "http://localhost/oauth/callback", "st", challenge, "me@gmail.com")
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert parts.netloc == "accounts.google.com" and q["client_id"] == GOOGLE_ID
    assert q["scope"] == "https://mail.google.com/" and q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["code_challenge_method"] == "S256" and q["state"] == "st" and q["login_hint"] == "me@gmail.com"
    ms = oauth.authorize_url("microsoft", MS_ID, "", "http://localhost:8765/oauth/callback", "st", challenge)
    assert ms.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    assert "IMAP.AccessAsUser.All+offline_access" in ms
    assert "/consumers/" in oauth.authorize_url("microsoft", MS_ID, "consumers", "r", "s", "c")
    assert oauth.redirect_uri(8765) == "http://localhost:8765/oauth/callback"
    assert oauth.redirect_uri(None) == "http://localhost/oauth/callback"


def test_provider_from_host():
    assert oauth.provider_for_host("imap.gmail.com") == "google"
    assert oauth.provider_for_host("outlook.office365.com") == "microsoft"
    assert oauth.provider_for_host("imap-mail.outlook.com.") == "microsoft"
    assert oauth.provider_for_host("imap.example.com") == "password"


def test_the_pasted_address():
    assert oauth.parse_redirect(" http://localhost:8765/oauth/callback?state=s1&code=c1&scope=x ") == ("c1", "s1")
    with pytest.raises(OAuthError, match="not completed: The user denied"):
        oauth.parse_redirect("http://localhost/oauth/callback?error=access_denied&error_description=The+user+denied")
    with pytest.raises(OAuthError, match="no sign-in code"):
        oauth.parse_redirect("http://localhost/oauth/callback")


def test_code_exchange(token_server):
    token_server.answers = [FakeResponse(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3599})]
    tokens = oauth.exchange_code("google", GOOGLE_ID, "gsecret", "", "c1", "http://localhost/oauth/callback", "ver")
    url, data = token_server.calls[0]
    assert url == "https://oauth2.googleapis.com/token" and tokens["refresh_token"] == "rt"
    assert data == {"grant_type": "authorization_code", "client_id": GOOGLE_ID, "code": "c1", "client_secret": "gsecret",
                    "redirect_uri": "http://localhost/oauth/callback", "code_verifier": "ver"}
    token_server.answers = [FakeResponse(200, {"access_token": "at"})]  # no refresh token: useless
    with pytest.raises(OAuthError, match="no refresh token"):
        oauth.exchange_code("microsoft", MS_ID, "", "", "c1", "r", "ver")
    assert "client_secret" not in token_server.calls[1][1]  # a Microsoft public client has none
    token_server.answers = [FakeResponse(400, {"error": "invalid_client", "error_description": "AADSTS7000218: …"})]
    with pytest.raises(OAuthError, match=r"Microsoft refused the sign-in \(invalid_client: AADSTS7000218"):
        oauth.exchange_code("microsoft", MS_ID, "", "", "c1", "r", "ver")


def test_access_tokens_are_cached_and_rotated_refresh_tokens_kept(token_server, tmp_path):
    path = tmp_path / "secrets.toml"
    write_secrets(path, "oauth", {"refresh_token": "rt1"})
    login = OAuthLogin("microsoft", MS_ID, "", "", "rt1", path)
    token_server.answers = [FakeResponse(200, {"access_token": "at1", "refresh_token": "rt2", "expires_in": 3600})]
    assert login.access_token() == "at1" and login.access_token() == "at1"  # the second from the cache
    assert len(token_server.calls) == 1 and token_server.calls[0][1]["grant_type"] == "refresh_token"
    assert _secrets(path)["oauth"]["refresh_token"] == "rt2"  # Microsoft rotated it
    assert "rt1" not in repr(login)


def test_a_revoked_sign_in_asks_to_sign_in_again(token_server):
    token_server.answers = [FakeResponse(400, {"error": "invalid_grant", "error_description": "Token has been revoked"})]
    with pytest.raises(OAuthError, match="expired or was revoked – sign in again") as e:
        OAuthLogin("google", GOOGLE_ID, "", "gsecret", "rt-secret").access_token()
    assert "rt-secret" not in str(e.value) and "gsecret" not in str(e.value)


def test_sign_in_uses_xoauth2(token_server):
    calls = []
    mailbox = SimpleNamespace(login=lambda *a, **k: calls.append(("login", a)) or mailbox,
                              xoauth2=lambda *a, **k: calls.append(("xoauth2", a, k)) or mailbox)
    token_server.answers = [FakeResponse(200, {"access_token": "at", "expires_in": 3600})]
    creds = SimpleNamespace(imap_user="me@gmail.com", imap_password="",
                            oauth=OAuthLogin("google", GOOGLE_ID, "", "s", "rt"))
    oauth.sign_in(mailbox, creds, "INBOX")
    assert calls == [("xoauth2", ("me@gmail.com", "at"), {"initial_folder": "INBOX"})]
    oauth.sign_in(mailbox, SimpleNamespace(imap_user="u", imap_password="p", oauth=None), "INBOX")
    assert calls[-1] == ("login", ("u", "p"))


# ---------------------------------------------------------------- settings and credentials

def _use_google(setup, client_id=GOOGLE_ID):
    path = setup / "mailboxes" / "privat" / "mailbox.toml"
    text = path.read_text(encoding="utf-8").replace('host = "imap.example.de"',
                                                    f'host = "imap.gmail.com"\nauth = "google"\noauth_client_id = "{client_id}"')
    path.write_text(text, encoding="utf-8")


def test_credentials_of_an_oauth_mailbox(setup):
    _use_google(setup)
    with pytest.raises(ConfigError) as e:
        load_credentials(_box(setup))
    assert "Google client secret (mailbox settings)" in str(e.value) and "sign-in with Google" in str(e.value)
    assert "IMAP password" not in str(e.value)  # the password isn't needed
    write_secrets(_box(setup).secrets_path, "oauth", {"client_secret": "gs", "refresh_token": "rt"})
    creds = load_credentials(_box(setup))
    assert creds.imap_user == "u" and creds.imap_password == "" and creds.oauth.refresh_token == "rt"
    assert creds.oauth.secrets_path == _box(setup).secrets_path


def test_settings_store_the_sign_in_method(client, setup):
    html = client.get("/ui/m/privat/settings").text
    assert 'name="imap_auth"' in html and "Google (OAuth)" in html
    form = {**SETTINGS, "csrf": _csrf(html), "imap_auth": "google", "oauth_client_id": GOOGLE_ID,
            "oauth_client_secret": "g-secret-1"}
    r = client.post("/ui/m/privat/settings", data={**form, "then": "oauth"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/privat/oauth"
    cfg = _box(setup).cfg
    assert (cfg.imap_auth, cfg.oauth_client_id, cfg.oauth_tenant) == ("google", GOOGLE_ID, "")
    path = setup / "mailboxes" / "privat" / "secrets.toml"
    assert _secrets(path)["oauth"] == {"client_secret": "g-secret-1"}
    assert "g-secret-1" not in client.get("/ui/m/privat/settings").text

    write_secrets(path, "oauth", {"refresh_token": "rt"})
    client.post("/ui/m/privat/settings", data={**form, "oauth_client_secret": ""})  # unchanged: stays signed in
    assert _secrets(path)["oauth"] == {"client_secret": "g-secret-1", "refresh_token": "rt"}
    client.post("/ui/m/privat/settings", data={**form, "oauth_client_id": "456-def.apps.googleusercontent.com",
                                               "oauth_client_secret": ""})
    assert "refresh_token" not in _secrets(path)["oauth"]  # another app: sign in again

    r = client.post("/ui/m/privat/settings", data={**form, "oauth_client_id": "x y", "oauth_client_secret": "g-2"})
    assert r.status_code == 422 and "Client-ID" in r.text and "g-2" not in r.text


def test_the_sign_in_flow(client, setup, token_server):
    _use_google(setup)
    write_secrets(setup / "mailboxes" / "privat" / "secrets.toml", "oauth", {"client_secret": "gs"})
    html = client.get("/ui/m/privat/oauth").text
    link = re.search(r'href="(https://accounts\.google\.com/[^"]+)"', html).group(1).replace("&amp;", "&")
    q = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    assert q["redirect_uri"] == "http://localhost/oauth/callback" and q["login_hint"] == "u"
    state = q["state"]
    assert 'name="state" value="%s"' % state in html

    r = client.post("/ui/m/privat/oauth", data={"csrf": _csrf(html), "state": state,
                                                "response_url": "http://localhost/oauth/callback?state=other&code=c"})
    assert r.status_code == 422 and "another sign-in" in r.text  # an address of another sign-in

    token_server.answers = [FakeResponse(200, {"access_token": "at", "refresh_token": "rt-new", "expires_in": 3600})]
    r = client.post("/ui/m/privat/oauth", data={"csrf": _csrf(html), "state": state,
                                                "response_url": f"http://localhost/oauth/callback?state={state}&code=c9"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/privat/settings"
    assert token_server.calls[0][1]["code"] == "c9" and token_server.calls[0][1]["client_secret"] == "gs"
    assert _secrets(setup / "mailboxes" / "privat" / "secrets.toml")["oauth"]["refresh_token"] == "rt-new"
    html = client.get(r.headers["location"]).text
    assert "Mit Google angemeldet" in html and "rt-new" not in html

    r = client.post("/ui/m/privat/oauth", data={"csrf": _csrf(html), "state": state, "response_url": "x"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/privat/oauth"  # used up: start again


def test_the_direct_callback(client, setup, token_server):
    _use_google(setup)
    html = client.get("/ui/m/privat/oauth").text
    state = re.search(r'name="state" value="([^"]+)"', html).group(1)
    token_server.answers = [FakeResponse(200, {"access_token": "at", "refresh_token": "rt-cb"})]
    r = client.get(f"/oauth/callback?state={state}&code=c1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/privat/settings"
    assert _secrets(setup / "mailboxes" / "privat" / "secrets.toml")["oauth"]["refresh_token"] == "rt-cb"
    assert client.get(f"/oauth/callback?state={state}&code=c1").status_code == 404  # used up


def test_sign_in_needs_the_method_first(client):
    r = client.get("/ui/m/privat/oauth", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/privat/settings"


def test_new_microsoft_mailbox_goes_to_the_sign_in(client, setup):
    html = client.get("/ui/mailboxes/new").text
    assert "Microsoft (OAuth)" in html and "login-method.js" in html
    form = {"csrf": _csrf(html), "name": "Work", "imap_host": "outlook.office365.com", "imap_port": "993",
            "source_folder": "INBOX", "imap_user": "me@work.example", "imap_auth": "microsoft",
            "oauth_client_id": MS_ID, "oauth_tenant": "consumers", "template": "privat"}
    r = client.post("/ui/mailboxes/new", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/m/work/oauth"
    raw = tomllib.loads((setup / "mailboxes" / "work" / "mailbox.toml").read_text(encoding="utf-8"))
    assert raw["imap"]["auth"] == "microsoft" and raw["imap"]["oauth_tenant"] == "consumers"
    assert _secrets(setup / "mailboxes" / "work" / "secrets.toml") == {"imap": {"user": "me@work.example"}}
    assert "consumers/oauth2/v2.0/authorize" in client.get(r.headers["location"]).text
