"""One-click unsubscribe (RFC 8058): a POST of "List-Unsubscribe=One-Click" to the sender's https link.

Only sent when you confirm it in the admin UI. The link comes from a mail, so it is only followed to a
public address - never into your own network - and without cookies, redirects or other context.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

import requests

from . import __version__
from .i18n import _

log = logging.getLogger(__name__)

TIMEOUT = 15  # seconds


class UnsubscribeError(Exception):
    pass


def one_click_link(links: list[str]) -> str | None:
    return next((link for link in links if link.lower().startswith("https://")), None)


def _public(host: str, port: int) -> None:
    """Raise unless every address the host resolves to is a public one."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise UnsubscribeError(_("Could not reach %(host)s: %(error)s", host=host, error=e)) from None
    for info in infos:
        if not ipaddress.ip_address(str(info[4][0]).split("%")[0]).is_global:
            raise UnsubscribeError(_("%(host)s is not a public address; Sortroom does not send the request there.",
                                     host=host))


def one_click(url: str) -> int:
    """Send the unsubscribe request; returns the HTTP status. Raises UnsubscribeError."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise UnsubscribeError(_("One-click unsubscribe needs an https link."))
    _public(parts.hostname, parts.port or 443)
    try:
        r = requests.post(url, data={"List-Unsubscribe": "One-Click"}, timeout=TIMEOUT, allow_redirects=False,
                          headers={"User-Agent": f"Sortroom/{__version__}"})
    except requests.RequestException as e:
        raise UnsubscribeError(_("Could not reach %(host)s: %(error)s", host=parts.hostname, error=e)) from None
    log.info("one-click unsubscribe at %s: HTTP %s", parts.hostname, r.status_code)
    if r.status_code >= 400:
        raise UnsubscribeError(_("%(host)s answered with error %(status)s.", host=parts.hostname,
                                 status=r.status_code))
    return r.status_code
