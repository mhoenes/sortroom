"""Deleting a mailbox: its folder under mailboxes/ with settings, login, log and reports.

Nothing on the IMAP server is touched. A Google sign-in is revoked at Google first (Microsoft has
no endpoint for that). Runs under the mailbox's lock, so no run can start meanwhile.
"""
from __future__ import annotations

import logging
import shutil

from . import jobs, oauth
from .config import ConfigError, Mailbox, stored_credentials
from .runtime import single_instance

log = logging.getLogger(__name__)


class MailboxBusy(RuntimeError):
    """A run or job is active for the mailbox."""


def delete_mailbox(box: Mailbox) -> dict:
    """Delete the mailbox for good. Returns {"revoked": True/False/None} - None: nothing to revoke
    (password sign-in, or Microsoft, which offers no revocation); False: Google's revocation failed."""
    with single_instance(box.lock_path) as acquired:
        if not acquired:
            raise MailboxBusy(box.id)
        revoked = None
        if box.cfg.imap_auth != "password":
            try:
                token = stored_credentials(box)["oauth_refresh_token"]
            except ConfigError:
                token = ""
            if token and oauth.PROVIDERS[box.cfg.imap_auth].revoke:
                revoked = oauth.revoke(box.cfg.imap_auth, token)
        shutil.rmtree(box.workspace)
    box.lock_path.unlink(missing_ok=True)  # released above; no run can start for a mailbox that's gone
    jobs.remove_mailbox(box.id)
    log.info("[%s] mailbox deleted%s", box.id,
             "" if revoked is None else "; sign-in revoked" if revoked else "; revoking the sign-in failed")
    return {"revoked": revoked}
