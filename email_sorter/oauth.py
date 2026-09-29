"""OAuth2 sign-in for IMAP (XOAUTH2) with Google and Microsoft.

Google: every user registers their own OAuth client (a "Desktop app"), since Gmail's scope is
restricted and a shared app would need Google's security assessment. Microsoft: Sortroom brings its
own app (a public client for personal and work accounts, no secret) - a personal Outlook.com
account can't register apps without an Azure tenant - and a mailbox can still set its own client ID,
e.g. for an organization that only allows its own apps. The client ID is set per mailbox. The sign-in is the authorization-code flow with
PKCE and a loopback redirect to http://localhost:<port>/oauth/callback: when the admin UI runs
on another machine the browser can't open that address, so the user copies it from the address
bar into the UI, which takes the code from it. Google doesn't allow the Gmail scope in the device
flow, so this one flow serves both providers.

The refresh token is kept in the mailbox's secrets.toml ([oauth] refresh_token); an access token
is fetched with it before an IMAP login and kept in memory until shortly before it expires.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

log = logging.getLogger(__name__)

TIMEOUT = 20
REDIRECT_PATH = "/oauth/callback"


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    authorize: str  # {tenant} is filled in for Microsoft
    token: str
    scope: str
    extra: dict = field(default_factory=dict)
    needs_secret: bool = False  # Google's desktop clients send their (not secret) client secret
    default_client_id: str = ""  # Sortroom's own app, used when a mailbox sets no client ID


PROVIDERS = {
    "google": Provider(
        "google", "Google", "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token",
        "https://mail.google.com/", {"access_type": "offline", "prompt": "consent"}, needs_secret=True),
    "microsoft": Provider(
        "microsoft", "Microsoft", "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "https://outlook.office.com/IMAP.AccessAsUser.All offline_access", {"prompt": "select_account"},
        default_client_id="d35f0c72-77b8-4463-81fb-d3bc62074976"),  # "Sortroom", public client
}
DEFAULT_TENANT = "common"  # Microsoft: personal and work accounts


class OAuthError(RuntimeError):
    """The sign-in failed or has to be repeated. The message never contains a token."""


def provider_for_host(host: str) -> str:
    """The sign-in a mailbox on this IMAP server most likely needs: "google", "microsoft" or "password"."""
    host = host.lower().rstrip(".")
    if host.endswith(("gmail.com", "googlemail.com")):
        return "google"
    if host.endswith(("office365.com", "outlook.com", "hotmail.com", "live.com")):
        return "microsoft"
    return "password"


def client_id_for(provider: str, configured: str) -> str:
    """The client ID a mailbox signs in with: its own, else Sortroom's app ("" when there is none)."""
    return configured or PROVIDERS[provider].default_client_id


def redirect_uri(port: int | None) -> str:
    return f"http://localhost:{port}{REDIRECT_PATH}" if port else f"http://localhost{REDIRECT_PATH}"


def _url(template: str, tenant: str) -> str:
    return template.format(tenant=tenant or DEFAULT_TENANT)


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge)"""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(provider: str, client_id: str, tenant: str, redirect: str, state: str, challenge: str,
                  login_hint: str = "") -> str:
    p = PROVIDERS[provider]
    params = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": p.scope,
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256", **p.extra}
    if login_hint:
        params["login_hint"] = login_hint
    return f"{_url(p.authorize, tenant)}?{urlencode(params)}"


def parse_redirect(pasted: str) -> tuple[str, str]:
    """(code, state) from the address the browser was sent to, or the error the provider sent."""
    query = parse_qs(urlsplit(pasted.strip()).query)
    if "error" in query:
        detail = query.get("error_description", query["error"])[0]
        raise OAuthError(f"the sign-in was not completed: {detail}")
    code, state = query.get("code", [""])[0], query.get("state", [""])[0]
    if not code or not state:
        raise OAuthError("that address has no sign-in code – copy the whole address of the page you were sent to")
    return code, state


def _token_request(provider: str, tenant: str, data: dict) -> dict:
    p = PROVIDERS[provider]
    try:
        r = requests.post(_url(p.token, tenant), data=data, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise OAuthError(f"{p.label} could not be reached: {e}") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or "access_token" not in body:
        error = body.get("error", f"HTTP {r.status_code}")
        detail = str(body.get("error_description") or "").split("\n")[0][:200]
        if error == "invalid_grant" and data.get("grant_type") == "refresh_token":
            code = re.match(r"(AADSTS\d+)", detail)  # Microsoft's reason, e.g. AADSTS70000: access removed
            raise OAuthError(f"the sign-in with {p.label} has expired or was revoked – sign in again "
                             f"under the mailbox's settings{f' ({code.group(1)})' if code else ''}")
        raise OAuthError(f"{p.label} refused the sign-in ({error}{': ' + detail if detail else ''})")
    return body


def exchange_code(provider: str, client_id: str, client_secret: str, tenant: str, code: str, redirect: str,
                  verifier: str) -> dict:
    """The tokens for an authorization code; the answer has a refresh_token."""
    data = {"grant_type": "authorization_code", "client_id": client_id, "code": code, "redirect_uri": redirect,
            "code_verifier": verifier}
    if client_secret:
        data["client_secret"] = client_secret
    body = _token_request(provider, tenant, data)
    if not body.get("refresh_token"):
        raise OAuthError(f"{PROVIDERS[provider].label} sent no refresh token – remove Sortroom's access in "
                         "your account's security settings and sign in again")
    return body


# access tokens by (provider, client id, refresh token), so runs don't fetch a new one every time
_cache: dict[tuple, tuple[str, float]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class OAuthLogin:
    """What a mailbox needs to sign in with XOAUTH2."""
    provider: str
    client_id: str
    tenant: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    secrets_path: Path | None = None  # where a new refresh token is stored when the provider rotates it

    def access_token(self) -> str:
        key = (self.provider, self.client_id, self.refresh_token)
        with _cache_lock:
            cached = _cache.get(key)
            if cached and cached[1] > time.time() + 60:
                return cached[0]
        data = {"grant_type": "refresh_token", "client_id": self.client_id, "refresh_token": self.refresh_token,
                "scope": PROVIDERS[self.provider].scope}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        body = _token_request(self.provider, self.tenant, data)
        token, expires = body["access_token"], time.time() + float(body.get("expires_in", 3600))
        with _cache_lock:
            _cache[key] = (token, expires)
        rotated = body.get("refresh_token")
        if rotated and rotated != self.refresh_token and self.secrets_path:
            from .config import write_secrets
            write_secrets(self.secrets_path, "oauth", {"refresh_token": rotated})
            with _cache_lock:
                _cache[(self.provider, self.client_id, rotated)] = (token, expires)
        return token


def sign_in(mailbox, creds, initial_folder: str | None = "INBOX"):
    """Log in to an imap_tools MailBox with the mailbox's password or, with OAuth, XOAUTH2."""
    if creds.oauth:
        return mailbox.xoauth2(creds.imap_user, creds.oauth.access_token(), initial_folder=initial_folder)
    return mailbox.login(creds.imap_user, creds.imap_password, initial_folder=initial_folder)
