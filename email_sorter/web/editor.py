"""Pages that change a mailbox's settings: categories (with a model trial) and mailbox settings.

Writes go through .editing (validated, atomic, with .bak). Every form carries a CSRF token kept in
the session. A mailbox whose file isn't writable (e.g. a config.toml mounted read-only) is shown
read-only.
"""
from __future__ import annotations

import contextvars
import logging
import os
import secrets
import threading
import time
import uuid
from typing import Any
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import jobs, oauth
from ..check import check_imap
from ..config import (INBOX_ACTION, SECRETS_FILE, ConfigError, Mailbox, load_credentials, stored_credentials,
                      write_secrets)
from ..classifier import ClassifierAuthError
from ..i18n import _
from ..removal import MailboxBusy, delete_mailbox
from ..trial import OTHER_SAMPLE, OWN_SAMPLE, run_trial
from . import _box, _sidebar, queries, require_login, router, templates
from .editing import (EditError, connection_from_form, delete_category, save_category, save_rules, save_settings,
                      secrets_writable, writable)

log = logging.getLogger(__name__)

MAX_TRIALS = 20
_trials: dict[str, dict] = {}
_trials_lock = threading.Lock()


# ---------------------------------------------------------------- CSRF and messages

def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(24)
    return token


async def _form(request: Request) -> dict:
    """The posted form, after checking its CSRF token."""
    form = await request.form()
    sent = str(form.get("csrf", ""))
    if not sent or not secrets.compare_digest(sent, request.session.get("csrf", "")):
        raise HTTPException(403, _("Form expired – please reload the page."))
    return {k: form.get(k) for k in form.keys()}


def _flash(request: Request, text: str, tone: str = "ok") -> None:
    request.session["flash"] = [tone, text]


def pop_flash(request: Request):
    return request.session.pop("flash", None)


templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["pop_flash"] = pop_flash  # shown once by base.html


def _page(request: Request, name: str, ctx: dict, status: int = 200):
    return templates.TemplateResponse(request, name, {"csrf": csrf_token(request), **ctx}, status_code=status)


def _shared_path(request: Request):
    return request.app.state.config_path


def credential_view(box: Mailbox) -> tuple[dict, str | None]:
    """For the pages: whether each credential of a mailbox is set - never a password or key -
    plus the stored user; and an error, if any."""
    try:
        found = stored_credentials(box)
    except ConfigError as e:
        return {"imap_user": False, "imap_password": False, "oauth_client_secret": False,
                "oauth_refresh_token": False, "classifier_api_key": False, "stored_user": ""}, str(e)
    return {**{k: bool(v) for k, v in found.items()}, "stored_user": found["imap_user"]}, None


def without_secrets(form: dict, *names: str) -> tuple[dict, bool]:
    """The form to show again after an error: write-only fields are never echoed back.
    The flag says whether one of them had been filled in (so it has to be entered again)."""
    return {k: v for k, v in form.items() if k not in names}, any(form.get(n) for n in names)


def form_number(value) -> str:
    """0.7 -> '0.7', 24.0 -> '24': the value of an <input type=number>, which the browser
    shows with the local decimal comma."""
    return "" if value is None else f"{value:g}"


# ---------------------------------------------------------------- categories

def _category_page(request: Request, box_id: str, cat: str = "", new: bool = False, form: dict | None = None,
                   error: str | None = None, status: int = 200):
    boxes, box = _box(request, box_id)
    db = queries.connect(box.workspace)
    try:
        counts = queries.category_counts(db)
        rates = queries.corrections(db, box.cfg.min_confidence)
    finally:
        if db:
            db.close()
    if not new and not cat and not form:
        cat = next(iter(box.cfg.categories), "")
    if cat and cat not in box.cfg.categories and not new:
        raise HTTPException(404, _("Unknown category"))
    current = box.cfg.categories.get(cat) if not new else None
    if form is None:
        form = {"key": cat, "label": current.label if current else "",
                "description": current.description if current else "",
                "folder": (current.folder or "") if current else "",
                "flag": current.flag if current else False,
                "flag_on_action": current.flag_on_action if current else True,
                "track_expiry": current.track_expiry if current else False,
                "expired_folder": (current.expired_folder or "") if current else ""}
        draft = request.query_params.get("draft")
        job = _trials.get(draft or "")
        if job and job["box"] == box.id and job["key"] == (cat or job["key"]):
            form["description"] = job["description"]
    rules_using = {r.action for r in box.cfg.sender_rules}
    return _page(request, "categories.html", {
        **_sidebar(request, boxes, box, "categories"), "box": box, "cat": cat, "new": new, "form": form,
        "counts": counts, "rates": rates, "error": error, "editable": writable(box), "rules_using": rules_using,
        "sample": (OWN_SAMPLE, OTHER_SAMPLE)}, status)


@router.get("/ui/m/{box_id}/categories", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def categories(request: Request, box_id: str, cat: str = "", new: str = ""):
    return _category_page(request, box_id, cat=cat, new=bool(new))


@router.post("/ui/m/{box_id}/categories", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def categories_save(request: Request, box_id: str):
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    new = form.get("new") == "1"
    key = str(form.get("key") or "").strip().lower()
    try:
        if form.get("delete") == "1":
            delete_category(box, _shared_path(request), key)
            _flash(request, _("Category \"%(name)s\" deleted.", name=box.cfg.categories[key].label))
            return RedirectResponse(f"/ui/m/{box.id}/categories", status_code=303)
        key = save_category(box, _shared_path(request), key, form, create=new)
    except EditError as e:
        view = {**form, **{k: form.get(k) == "on" for k in ("flag", "flag_on_action", "track_expiry")}}
        return _category_page(request, box_id, cat=key, new=new, form=view, error=str(e), status=422)
    _flash(request, _("Saved. Applies from the next run."))
    return RedirectResponse(f"/ui/m/{box.id}/categories?cat={quote(key)}", status_code=303)


# ---------------------------------------------------------------- model trial

@router.post("/ui/m/{box_id}/categories/test", dependencies=[Depends(require_login)])
async def categories_test(request: Request, box_id: str):
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    key = str(form.get("key") or "").strip().lower()
    description = str(form.get("description") or "").strip()
    new = form.get("new") == "1"
    if not key or not description or len(description) > 4000:
        view = {**form, **{k: form.get(k) == "on" for k in ("flag", "flag_on_action", "track_expiry")}}
        return _category_page(request, box_id, cat=key, new=new, form=view,
                              error=_("The test needs a key and a description."), status=422)
    if any(j["box"] == box.id and j["status"] == "running" for j in _trials.values()):
        _flash(request, _("A test is already running for this mailbox."), "warn")
        return RedirectResponse(f"/ui/m/{box.id}/categories?cat={quote(key)}", status_code=303)
    try:
        creds = load_credentials(box)
    except ConfigError as e:
        raise HTTPException(500, str(e)) from None
    job: dict[str, Any] = {"id": uuid.uuid4().hex[:12], "box": box.id, "key": key, "new": new,
                           "description": description,
           "status": "running", "done": 0, "total": 0, "rows": [], "cost": 0.0, "error": None,
           "started": time.time()}
    with _trials_lock:
        _trials[job["id"]] = job
        for old in [j for j in _trials.values() if j["status"] != "running"][:-MAX_TRIALS]:
            _trials.pop(old["id"], None)

    def progress(done: int, total: int) -> None:
        job.update(done=done, total=total)

    def work() -> None:
        try:
            rows, cost = run_trial(box.cfg, creds, box.workspace / "data" / "state.db", key, description, progress)
            job.update(status="done", rows=rows, cost=cost)
        except ClassifierAuthError as e:
            job.update(status="failed", error=_("The endpoint rejects the API key: %(e)s", e=e))
        except Exception as e:
            log.exception("[%s] category trial failed", box.id)
            job.update(status="failed", error=str(e))

    threading.Thread(target=contextvars.copy_context().run, args=(work,), name=f"trial-{job['id']}",
                     daemon=True).start()
    return RedirectResponse(f"/ui/m/{box.id}/categories/test/{job['id']}", status_code=303)


@router.get("/ui/m/{box_id}/categories/test/{job_id}", response_class=HTMLResponse,
            dependencies=[Depends(require_login)])
def categories_test_result(request: Request, box_id: str, job_id: str):
    boxes, box = _box(request, box_id)
    job = _trials.get(job_id)
    if not job or job["box"] != box.id:
        raise HTTPException(404, _("Unknown test (tests are only kept until the next restart)"))
    key, rows = job["key"], job["rows"]
    tested = [r for r in rows if r.after]
    own = [r for r in tested if r.before == key]
    summary = {
        "tested": len(tested),
        "kept": sum(1 for r in own if r.after == key),
        "lost": [r for r in own if r.after != key],
        "gained": [r for r in tested if r.before != key and r.after == key],
        "moved_elsewhere": [r for r in tested if r.before != key and r.after not in (key, r.before)],
        "own": len(own),
        "errors": sum(1 for r in rows if r.error),
        "corrected": sum(1 for r in tested if r.corrected),
        "corrected_right": sum(1 for r in tested if r.corrected and r.after == r.before),
    }
    cat = box.cfg.categories.get(key)
    return _page(request, "trial.html", {
        **_sidebar(request, boxes, box, "categories"), "box": box, "job": job, "rows": rows, "s": summary,
        "cat_label": cat.label if cat else key,
        "label": lambda k: box.cfg.categories[k].label if k in box.cfg.categories else (
            _("Inbox") if k == INBOX_ACTION else k if k != key else _("%(key)s (new)", key=k))})


# ---------------------------------------------------------------- settings

def auth_methods() -> dict[str, str]:
    return {"password": _("Password"), "google": _("Google (OAuth)"), "microsoft": _("Microsoft (OAuth)")}


def _known_folders(box: Mailbox) -> list[str]:
    """Folders to suggest in folder fields: the inbox, the categories' folders and those mail was sorted into."""
    cfg = box.cfg
    db = queries.connect(box.workspace)
    try:
        known = set(queries.folders(db))
    finally:
        if db:
            db.close()
    known |= {c.folder for c in cfg.categories.values() if c.folder}
    known |= {f for f in (cfg.source_folder, cfg.expired_folder, *(c.expired_folder for c in cfg.categories.values()))
              if f}
    return sorted(known)


# the fields of the mailbox settings that show their own error (settings.html); an error about another one
# – or from the rule tables – shows on top
SETTINGS_FIELDS = {"name", "box_id", "imap_host", "imap_port", "source_folder", "imap_auth", "imap_user",
                   "imap_password", "oauth_client_id", "oauth_client_secret", "oauth_tenant", "min_confidence",
                   "action_flag_threshold",
                   "expiry_threshold", "min_age_hours", "lookback_days", "max_per_run", "expired_folder",
                   "schedule_minutes", "reconcile_hours"}


def _settings_page(request: Request, box_id: str, form: dict | None = None, error: EditError | str | None = None,
                   status: int = 200, retype: bool = False, retype_secret: bool = False,
                   tested: tuple[str, str, str, str] | None = None):
    boxes, box = _box(request, box_id)
    cfg = box.cfg
    login, login_error = credential_view(box)
    if form is None:
        form = {"name": box.name, "imap_host": cfg.imap_host, "imap_port": cfg.imap_port,
                "source_folder": cfg.source_folder, "min_confidence": form_number(cfg.min_confidence),
                "action_flag_threshold": form_number(cfg.action_flag_threshold),
                "expiry_threshold": form_number(cfg.expiry_threshold), "min_age_hours": form_number(cfg.min_age_hours),
                "sort_read_at_once": cfg.sort_read_at_once,
                "schedule_enabled": cfg.schedule_enabled, "schedule_minutes": cfg.schedule_minutes,
                "reconcile_enabled": cfg.reconcile_enabled, "reconcile_hours": cfg.reconcile_hours,
                "lookback_days": cfg.lookback_days, "max_per_run": cfg.max_per_run,
                "expired_folder": cfg.expired_folder or "", "imap_user": login["stored_user"],
                "imap_auth": cfg.imap_auth, "oauth_client_id": cfg.oauth_client_id, "oauth_tenant": cfg.oauth_tenant}
    return _page(request, "settings.html", {
        **_sidebar(request, boxes, box, "settings"), "box": box, "cfg": cfg, "form": form,
        "error": str(error) if error else None,
        "error_field": error.field if isinstance(error, EditError) and error.field in SETTINGS_FIELDS else None,
        "schedule": request.app.state.scheduler.status(box),
        "folders": _known_folders(box), "editable": writable(box),
        "login": login, "login_error": login_error, "login_writable": secrets_writable(box.secrets_path),
        "secrets_file": SECRETS_FILE, "retype": retype, "retype_secret": retype_secret,
        "auth_methods": auth_methods(), "oauth_hint": oauth.provider_for_host(cfg.imap_host),
        "deletable": os.access(box.workspace.parent, os.W_OK), "tested": tested,
        "config_name": box.config_file.name if box.config_file else "–"},
        status)


@router.get("/ui/m/{box_id}/settings", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def settings(request: Request, box_id: str):
    return _settings_page(request, box_id)


@router.post("/ui/m/{box_id}/settings", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def settings_save(request: Request, box_id: str):
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    busy = _busy(request, box)
    try:
        new_id = save_settings(box, _shared_path(request), form, busy=busy)
    except EditError as e:
        form, retype = without_secrets(form, "imap_password")
        form, retype_secret = without_secrets(form, "oauth_client_secret")
        return _settings_page(request, box_id, form=form, error=e, status=422, retype=retype,
                              retype_secret=retype_secret)
    if new_id != box.id:
        _flash(request, _("Settings saved. The mailbox folder is now mailboxes/%(id)s.", id=new_id))
    else:
        _flash(request, _("Settings saved. They apply from the next run."))
    if form.get("then") == "oauth":  # "Save & sign in"
        return RedirectResponse(f"/ui/m/{new_id}/oauth", status_code=303)
    return RedirectResponse(f"/ui/m/{new_id}/settings", status_code=303)


@router.post("/ui/m/{box_id}/settings/test", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def settings_test(request: Request, box_id: str):
    """Check connection: log in with the server and login in the form without saving them, and list the
    target folders. An empty password or secret means the stored one. The page's script asks for JSON and
    shows the result below the button; without JavaScript the page comes back."""
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    lines: list[str] = []
    field = None
    try:
        cfg, creds = connection_from_form(box, _shared_path(request), form)
        ok = await run_in_threadpool(check_imap, cfg, creds, lines.append)
        failure = next((line.split("FAILED: ", 1)[1] for line in lines if "FAILED: " in line), "")
        tone, text = ("ok", _("Connection works.")) if ok else ("err", _("Connection failed: %(e)s", e=failure))
    except (EditError, ConfigError) as e:
        tone, text, field = "err", str(e), getattr(e, "field", None)
    details = "\n".join(line for line in lines if "FAILED: " not in line)  # server, inbox, target folders
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"tone": tone, "text": text, "field": field, "details": details})
    form, retype = without_secrets(form, "imap_password")
    form, retype_secret = without_secrets(form, "oauth_client_secret")
    return _settings_page(request, box_id, form=form, retype=retype, retype_secret=retype_secret,
                          tested=("connection", tone, text, details))


# ---------------------------------------------------------------- the Rules page: sender rules, deletion rules

def _rules_page(request: Request, box_id: str, sender_rules: list[tuple[str, str]] | None = None,
                delete_rules: list[dict] | None = None, error: str | None = None, status: int = 200):
    boxes, box = _box(request, box_id)
    cfg = box.cfg
    if sender_rules is None:
        sender_rules = [(r.match, r.action) for r in cfg.sender_rules]
    if delete_rules is None:
        delete_rules = [{"folder": r.folder, "days": r.days, "only_read": r.only_read, "starred": r.starred}
                        for r in cfg.delete_rules]
    return _page(request, "rules.html", {
        **_sidebar(request, boxes, box, "rules"), "box": box, "cfg": cfg, "rules": sender_rules,
        "delete_rules": delete_rules, "folders": _known_folders(box), "editable": writable(box), "error": error,
        "config_name": box.config_file.name if box.config_file else "–"}, status)


@router.get("/ui/m/{box_id}/rules", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def rules_page(request: Request, box_id: str):
    return _rules_page(request, box_id)


@router.post("/ui/m/{box_id}/rules", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def rules_save(request: Request, box_id: str):
    """Save both lists at once; a row removed in the page leaves a gap in the numbers, which is skipped."""
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    sender_rules = [(str(form.get(f"match_{i}") or ""), str(form.get(f"action_{i}") or INBOX_ACTION))
                    for i in range(int(form.get("rows") or 0))]
    delete_rules: list[dict] = [
        {"folder": str(form.get(f"dfolder_{i}") or ""), "days": str(form.get(f"ddays_{i}") or ""),
         "only_read": bool(form.get(f"dread_{i}")), "starred": bool(form.get(f"dstar_{i}"))}
        for i in range(int(form.get("drows") or 0))]
    try:
        save_rules(box, _shared_path(request), sender_rules, delete_rules)
    except EditError as e:
        return _rules_page(request, box_id, error=str(e), status=422,
                           sender_rules=[r for r in sender_rules if r[0].strip()],
                           delete_rules=[r for r in delete_rules if r["folder"].strip()])
    _flash(request, _("Rules saved. They apply from the next run."))
    return RedirectResponse(f"/ui/m/{box.id}/rules", status_code=303)


def _busy(request: Request, box: Mailbox) -> bool:
    """A run, job or model test is active for the mailbox."""
    return (request.app.state.is_busy(box) or jobs.running(box.id)
            or any(j["box"] == box.id and j["status"] == "running" for j in _trials.values()))


@router.post("/ui/m/{box_id}/delete", dependencies=[Depends(require_login)])
async def mailbox_delete(request: Request, box_id: str):
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    back = RedirectResponse(f"/ui/m/{box.id}/settings", status_code=303)
    if str(form.get("confirm") or "").strip() != box.name:
        _flash(request, _("Not deleted: please type the name of the mailbox exactly."), "err")
        return back
    if _busy(request, box):
        _flash(request, _("Not deleted: something is running for this mailbox. Please try again when it is "
                          "done."), "warn")
        return back
    try:
        result = await run_in_threadpool(delete_mailbox, box)
    except MailboxBusy:
        _flash(request, _("Not deleted: something is running for this mailbox. Please try again when it is "
                          "done."), "warn")
        return back
    except OSError as e:
        log.exception("[%s] deleting the mailbox failed", box.id)
        _flash(request, _("The mailbox could not be deleted completely: %(e)s", e=e), "err")
        return RedirectResponse("/ui", status_code=303)
    text = _('Mailbox "%(name)s" deleted.', name=box.name)
    if result["revoked"] is True:
        text += " " + _("The sign-in with Google was revoked.")
    elif result["revoked"] is False:
        text += " " + _("Revoking the sign-in with Google failed – remove Sortroom's access at "
                        "myaccount.google.com/connections.")
    _flash(request, text, "ok" if result["revoked"] is not False else "warn")
    return RedirectResponse("/ui", status_code=303)

# ---------------------------------------------------------------- OAuth sign-in (see oauth.py)

OAUTH_TTL = 15 * 60
_sign_ins: dict[str, dict] = {}  # state -> the sign-in waiting for its code
_sign_ins_lock = threading.Lock()


def _oauth_page(request: Request, box: Mailbox, state: str, error: str | None = None, status: int = 200):
    boxes = request.app.state.load_mailboxes()
    return _page(request, "oauth.html", {
        **_sidebar(request, boxes, box, "settings"), "box": box, "state": state, "url": _sign_ins[state]["url"],
        "provider": oauth.PROVIDERS[box.cfg.imap_auth].label, "error": error,
        "direct": request.url.hostname in ("localhost", "127.0.0.1", "::1")}, status)


@router.get("/ui/m/{box_id}/oauth", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def oauth_start(request: Request, box_id: str):
    """Start a sign-in: the link to the provider and a field for the address it sends the browser to."""
    _all_boxes, box = _box(request, box_id)
    cfg = box.cfg
    client_id = "" if cfg.imap_auth == "password" else oauth.client_id_for(cfg.imap_auth, cfg.oauth_client_id)
    if not client_id:
        _flash(request, _("Choose Google or Microsoft as the sign-in method and enter the client ID first."), "warn")
        return RedirectResponse(f"/ui/m/{box.id}/settings", status_code=303)
    verifier, challenge = oauth.pkce_pair()
    state, redirect = secrets.token_urlsafe(24), oauth.redirect_uri(request.url.port)
    url = oauth.authorize_url(cfg.imap_auth, client_id, cfg.oauth_tenant, redirect, state, challenge,
                              login_hint=stored_credentials(box)["imap_user"])
    with _sign_ins_lock:
        now = time.time()
        for old in [s for s, v in _sign_ins.items() if v["created"] < now - OAUTH_TTL]:
            del _sign_ins[old]
        _sign_ins[state] = {"box": box.id, "verifier": verifier, "redirect": redirect, "url": url, "created": now}
    return _oauth_page(request, box, state)


def _complete_sign_in(box: Mailbox, state: str, code: str) -> None:
    """Exchange the code for the tokens and keep the refresh token in the mailbox's secrets.toml."""
    pending = _sign_ins[state]
    tokens = oauth.exchange_code(box.cfg.imap_auth, oauth.client_id_for(box.cfg.imap_auth, box.cfg.oauth_client_id),
                                 stored_credentials(box)["oauth_client_secret"], box.cfg.oauth_tenant, code,
                                 pending["redirect"], pending["verifier"])
    write_secrets(box.secrets_path, "oauth", {"refresh_token": tokens["refresh_token"]})
    with _sign_ins_lock:
        _sign_ins.pop(state, None)


def _pending(state: str, box_id: str | None = None) -> dict | None:
    with _sign_ins_lock:
        found = _sign_ins.get(state)
    if not found or found["created"] < time.time() - OAUTH_TTL or (box_id and found["box"] != box_id):
        return None
    return found


def _signed_in(request: Request, box: Mailbox):
    _flash(request, _('Signed in with %(provider)s. "Check connection" tests the connection.',
                      provider=oauth.PROVIDERS[box.cfg.imap_auth].label))
    return RedirectResponse(f"/ui/m/{box.id}/settings", status_code=303)


@router.post("/ui/m/{box_id}/oauth", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def oauth_finish(request: Request, box_id: str):
    """The address the provider sent the browser to, pasted by the user."""
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    state = str(form.get("state") or "")
    if not _pending(state, box.id):
        _flash(request, _("This sign-in has expired. Please start it again."), "warn")
        return RedirectResponse(f"/ui/m/{box.id}/oauth", status_code=303)
    try:
        code, sent_state = oauth.parse_redirect(str(form.get("response_url") or ""))
        if sent_state != state:
            raise oauth.OAuthError("that address belongs to another sign-in – use the one you just opened")
        await run_in_threadpool(_complete_sign_in, box, state, code)
    except oauth.OAuthError as e:
        return _oauth_page(request, box, state, error=_("Not signed in: %(e)s", e=e), status=422)
    return _signed_in(request, box)


@router.get(oauth.REDIRECT_PATH, response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def oauth_callback(request: Request):
    """Where the provider sends the browser when the admin UI itself runs on localhost."""
    state = request.query_params.get("state", "")
    pending = _pending(state)
    if not pending:
        raise HTTPException(404, _("This sign-in has expired. Please start it again."))
    _all_boxes, box = _box(request, pending["box"])
    try:
        code, _state = oauth.parse_redirect(str(request.url))
        await run_in_threadpool(_complete_sign_in, box, state, code)
    except oauth.OAuthError as e:
        return _oauth_page(request, box, state, error=_("Not signed in: %(e)s", e=e), status=422)
    return _signed_in(request, box)
