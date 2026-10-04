"""Pages that act on mail: maintenance jobs, single-mail corrections, adding a mailbox, shared settings."""
from __future__ import annotations

import json
import logging
import time
import tomllib
from pathlib import Path
from datetime import date
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import jobs
from ..check import check_imap, check_model
from ..classifier import ClassifierClient, ClassifierError
from .. import i18n
from ..config import (EXAMPLE_MAILBOXES, INBOX_ACTION, SECRETS_FILE, ConfigError, Mailbox, _read_toml,
                      classifier_key, imap_credentials, load_credentials, write_secrets)
from ..i18n import _
from ..maintenance import relocate_category, rename_category, rename_folder
from ..oauth import PROVIDERS
from ..manual import ManualError, move_mail, set_star
from ..reconcile import run_reconcile
from ..resort import run_resort
from ..runtime import single_instance
from ..sorter import RunResult, expired_target, run, run_backfill, run_recheck_expiry
from ..cleanup import run_cleanup
from ..mail import MailError, MailSettings, mail_settings, send, smtp_password
from ..undo import changed_since, run_undo
from ..store import Store
from . import _box, _boxes, _sidebar, queries, require_login, router
from .editing import (EditError, add_sender_rule, can_add_mailbox, classifier_from_form, create_mailbox,
                      mail_from_form, rename_category_key, rename_folder_refs, reserved_key_text, save_shared,
                      secrets_writable, shared_writable, with_kind, writable)
from .editor import _flash, _form, _page, _shared_path, form_number, without_secrets

log = logging.getLogger(__name__)

EXAMPLE = "_example_"  # template choice prefix: the built-in standard categories of a language

TASKS = {"run", "backfill", "recheck", "resort", "relocate", "rename_folder", "rename_category", "reconcile", "check",
         "undo", "cleanup"}
RISKY_TASKS = {"rename_folder", "rename_category"}  # change the server and the settings


# ---------------------------------------------------------------- maintenance

@router.get("/ui/m/{box_id}/maintenance", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def maintenance(request: Request, box_id: str, category: str = ""):
    """With `category`: that one chosen under "Rename a category key" (a link from the Categories page)."""
    boxes, box = _box(request, box_id)
    db = queries.connect(box.workspace)
    try:
        folders = queries.folders(db)
        undoable = queries.undoable_runs(db)
    finally:
        if db:
            db.close()
    cat_folders = sorted({c.folder for c in box.cfg.categories.values() if c.folder} | set(folders))
    return _page(request, "maintenance.html", {
        **_sidebar(request, boxes, box, "maintenance"), "box": box, "tasks": TASKS,
        "jobs": jobs.recent(box.id, 15), "folders": cat_folders, "busy": request.app.state.is_busy(box),
        "editable": writable(box), "today": date.today().isoformat(), "undoable": undoable,
        "rename_category": category})


@router.get("/ui/m/{box_id}/undo", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def undo_page(request: Request, box_id: str, run: str = ""):
    """The mails of a run: which can still be undone, and why the others can't any more."""
    boxes, box = _box(request, box_id)
    db = queries.connect(box.workspace)
    try:
        info = next((r for r in queries.undoable_runs(db) if r["started"] == run), None)
        mails = queries.run_mails(db, run) if info else []
    finally:
        if db:
            db.close()
    if info is None:
        _flash(request, _("This run cannot be undone (any more)."), "warn")
        return RedirectResponse(f"/ui/m/{box.id}/maintenance", status_code=303)
    for m in mails:
        m["blocked"] = changed_since(m if m["known"] else None, bool(m["later"]), m["run_moved_to"])
    return _page(request, "undo.html", {
        **_sidebar(request, boxes, box, "maintenance"), "box": box, "run": info, "mails": mails,
        "undoable": sum(1 for m in mails if not m["blocked"]), "busy": request.app.state.is_busy(box)})


def _flag(form: dict, name: str) -> bool:
    return form.get(name) in ("1", "on", "true")


def _limit(form: dict) -> int | None:
    raw = str(form.get("limit") or "").strip()
    if not raw:
        return None
    if not raw.isdigit() or not 1 <= int(raw) <= 100000:
        raise EditError(_("Count: a whole number from 1."))
    return int(raw)


def _job_for(request: Request, box: Mailbox, task: str, form: dict):
    """(label, fn, needs_lock) for a maintenance form."""
    cfg, ws, live = box.cfg, box.workspace, _flag(form, "live")
    mode = "" if live else f" ({_('dry run')})"
    if task == "check":  # only the mailbox; the model is checked under Global settings
        try:
            creds = imap_credentials(box)
        except ConfigError:
            if cfg.imap_auth == "password":
                raise ConfigError(_("The login of this mailbox is not set yet.")) from None
            raise ConfigError(_("Sign in with %(provider)s first, in the connection section.",
                                provider=PROVIDERS[cfg.imap_auth].label)) from None

        def run_check() -> dict:
            ok = check_imap(cfg, creds, out=lambda line: log.info("%s", line.strip("\n")))
            return {"ok": ok, "exit_code": 0 if ok else 1}
        return _("Check connection"), run_check, False
    if task == "undo":  # only the mailbox, no model
        stamp, sort_again = str(form.get("run") or ""), _flag(form, "sort_again")
        if not stamp:
            raise EditError(_("Please choose a run."))
        keys: list[str] | None = None  # None: the whole run
        if form.get("keys") is not None:
            try:
                keys = [str(k) for k in json.loads(str(form["keys"]))]
            except (ValueError, TypeError):
                raise EditError(_("Please choose at least one mail.")) from None
            if not keys:
                raise EditError(_("Please choose at least one mail."))
        creds = imap_credentials(box)
        label = _("Undo the run of %(when)s", when=i18n.dt(stamp))
        if keys is not None:
            label += " · " + i18n.ngettext("%(num)s mail", "%(num)s mails", len(keys))
        return (label + mode,
                lambda: run_undo(cfg, creds, ws, stamp, live=live, sort_again=sort_again, keys=keys), True)
    if task == "cleanup":  # only the mailbox, no model
        if not cfg.delete_rules:
            raise EditError(_("This mailbox has no deletion rules (Rules)."))
        creds = imap_credentials(box)
        return _("Apply the deletion rules") + mode, lambda: run_cleanup(cfg, creds, ws, live=live), True
    creds = load_credentials(box)
    shared = _shared_path(request)
    if task == "run":
        limit = _limit(form)
        return _("Run") + mode, lambda: run(cfg, creds, ws, live=live, limit=limit), True
    if task == "backfill":
        try:
            since = date.fromisoformat(str(form.get("since") or ""))
        except ValueError:
            raise EditError(_("Backfill: please enter a start date.")) from None
        if since > date.today():
            raise EditError(_("Backfill: the start date is in the future."))
        limit = _limit(form)
        return (_("Backfill since %(date)s", date=i18n.date(since.isoformat())) + mode,
                lambda: run_backfill(cfg, creds, ws, live=live, since=since, limit=limit), True)
    if task == "recheck":
        return _("Check expiry dates") + mode, lambda: run_recheck_expiry(cfg, creds, ws, live=live), True
    if task == "resort":
        folder = str(form.get("folder") or "").strip()
        if not folder:
            raise EditError(_("Please choose a folder."))
        limit = _limit(form)
        return (_("Re-sort %(folder)s", folder=folder) + mode,
                lambda: run_resort(cfg, creds, ws, folder, live=live, limit=limit), True)
    if task == "relocate":
        category = str(form.get("category") or "")
        if category not in cfg.categories or not cfg.categories[category].folder:
            raise EditError(_("Please choose a category with a target folder."))
        return (_("Move %(category)s into %(folder)s", category=cfg.categories[category].label,
                  folder=cfg.categories[category].folder) + mode,
                lambda: relocate_category(cfg, creds, category, live=live, base_dir=ws), True)
    if task == "rename_folder":
        old, new = (str(form.get(k) or "").strip().strip("/") for k in ("old", "new"))
        if not old or not new or old == new:
            raise EditError(_("Please enter the old and the new folder name."))
        if old.upper() == "INBOX":
            raise EditError(_("The inbox itself cannot be renamed."))
        if live and not writable(box):
            raise EditError(_("The settings are read-only – the folders of the categories could not be updated."))

        def rename() -> RunResult:
            result = rename_folder(cfg, creds, old, new, live=live, base_dir=ws)
            if live and result.exit_code == 0:
                n = rename_folder_refs(box, shared, old, new)
                log.info("%d folder setting(s) now point to %s", n, new)
            return result
        return _("Folder %(old)s → %(new)s", old=old, new=new) + mode, rename, True
    if task == "rename_category":
        old, new = str(form.get("old") or ""), str(form.get("new") or "").strip().lower()
        if old not in cfg.categories:
            raise EditError(_("Please choose a category."))
        if new == INBOX_ACTION:
            raise EditError(reserved_key_text())
        if not new or new in cfg.categories:
            raise EditError(_("The new key is missing or already taken."))
        if live and not writable(box):
            raise EditError(_("The settings are read-only."))

        def rename_key() -> RunResult:
            if live:
                rename_category_key(box, shared, old, new)
            return rename_category(old, new, live=live, base_dir=ws)
        return _("Category %(old)s → %(new)s", old=old, new=new) + mode, rename_key, True
    if task == "reconcile":
        return _("Reconcile the log") + mode, lambda: run_reconcile(cfg, creds, ws, live=live), True
    raise HTTPException(404, _("Unknown task"))


@router.post("/ui/m/{box_id}/maintenance/{task}", dependencies=[Depends(require_login)])
async def maintenance_start(request: Request, box_id: str, task: str):
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    if task not in TASKS:
        raise HTTPException(404, _("Unknown task"))
    back = f"/ui/m/{box.id}/maintenance"
    if task == "undo" and form.pop("chosen", None):  # the run's page: one checkbox per mail, maybe none ticked
        form.pop("key", None)
        form["keys"] = json.dumps((await request.form()).getlist("key"))
        back = f"/ui/m/{box.id}/undo?run={quote(str(form.get('run') or ''))}"
    try:
        label, fn, needs_lock = _job_for(request, box, task, form)
    except (EditError, ConfigError) as e:
        _flash(request, str(e), "err")
        return RedirectResponse(back, status_code=303)
    if needs_lock and (request.app.state.is_busy(box) or jobs.running(box.id)):
        _flash(request, _("Something is running for this mailbox. Please wait until it is done."), "warn")
        return RedirectResponse(back, status_code=303)
    job = jobs.start(box, task, label, fn, request={k: v for k, v in form.items() if k != "csrf"},
                     needs_lock=needs_lock)
    return RedirectResponse(f"/ui/m/{box.id}/jobs/{job['id']}", status_code=303)


@router.get("/ui/m/{box_id}/jobs/{job_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def job_page(request: Request, box_id: str, job_id: str):
    boxes, box = _box(request, box_id)
    job = jobs.get(job_id)
    if not job or job["mailbox"] != box.id:
        raise HTTPException(404, _("Unknown job (jobs are only kept until the next restart)"))
    return _page(request, "job.html", {**_sidebar(request, boxes, box, "maintenance"), "box": box, "job": job,
                                       "rerun": _live_rerun(job), "risky": job["kind"] in RISKY_TASKS,
                                       "editable": writable(box)})


def _live_rerun(job: dict) -> dict | None:
    """The form fields that start a successful dry run again for real, or None.

    They go through maintenance_start like the maintenance page's own forms, so everything is
    checked again; the dry run's settings (date, folder, limit …) carry over unchanged."""
    result = job.get("result")
    if (job["kind"] not in TASKS or job["status"] != "done" or not isinstance(result, dict)
            or result.get("live") is not False or result.get("ok") is not True):
        return None
    return {k: "" if v is None else str(v) for k, v in (job.get("request") or {}).items() if k != "live"}


# ---------------------------------------------------------------- single mails

@router.post("/ui/m/{box_id}/mails/action", dependencies=[Depends(require_login)])
async def mail_action(request: Request, box_id: str):
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    key, action = str(form.get("key") or ""), str(form.get("action") or "")
    back = str(form.get("back") or "")
    back = f"/ui/m/{box.id}/mails?{back}" if back and not back.startswith(("/", "http")) else \
        f"/ui/m/{box.id}/mails?period=all&key={quote(key, safe='')}"
    then = str(form.get("then") or "")  # the list with the next mail open: where accepting it goes on to
    category = str(form.get("category") or "")

    if action == "move" and not category:  # "Correct" with nothing chosen
        _flash(request, _("Please choose a category."), "err")
        return RedirectResponse(back, status_code=303)
    if action == "expiry":  # only the log changes, no IMAP and no lock needed
        try:
            _flash(request, _set_expiry(box, key, form))
        except EditError as e:
            _flash(request, str(e), "err")
        return RedirectResponse(back, status_code=303)

    def act() -> str:
        creds = load_credentials(box)
        if action == "rule":
            match = str(form.get("match") or "").strip().lower()
            if not match:
                raise EditError(_("Please enter a sender."))
            add_sender_rule(box, _shared_path(request), match, category)
            text = _("Sender rule for %(match)s saved.", match=match)
            if _flag(form, "apply"):
                move_mail(box.cfg, creds, box.workspace, key, category)
                text += " " + _("The mail was moved.")
            return text
        if action in ("accept", "move"):
            folder = move_mail(box.cfg, creds, box.workspace, key, category)
            return _("Moved to %(folder)s.", folder=folder) if folder else _("Now in the inbox.")
        if action == "star":
            starred = _flag(form, "starred")
            set_star(box.cfg, creds, box.workspace, key, starred)
            return _("Star set.") if starred else _("Star removed.")
        raise EditError(_("Unknown action."))

    def locked() -> str:
        with single_instance(box.lock_path) as acquired:
            if not acquired:
                raise EditError(_("A run is in progress for this mailbox. Please try again in a moment."))
            return act()

    try:
        text = await run_in_threadpool(locked)
        if action == "move":  # corrected by hand: offer a rule, so the sender's next mails go there too
            back += f"&rule={quote(category, safe='')}"
        elif then and not then.startswith(("/", "http")):  # done with this one: on to the next mail
            text = _("\"%(subject)s\": %(result)s", subject=_subject(box, key), result=text)
            back = f"/ui/m/{box.id}/mails?{then}"
        _flash(request, text)
    except (EditError, ManualError, ConfigError) as e:
        _flash(request, str(e), "err")
    except Exception as e:
        log.exception("[%s] mail action failed", box.id)
        _flash(request, _("Failed: %(e)s", e=e), "err")
    return RedirectResponse(back, status_code=303)


def _subject(box: Mailbox, key: str) -> str:
    """The subject of a mail in the log, for a message about it shown with another mail open."""
    db = queries.connect(box.workspace)
    try:
        m = queries.mail(db, key)
    finally:
        if db:
            db.close()
    return (m and m["subject"]) or _("(no subject)")


def _set_expiry(box: Mailbox, key: str, form: dict) -> str:
    raw = "" if form.get("clear") else str(form.get("expires") or "").strip()
    try:
        expires = date.fromisoformat(raw) if raw else None
    except ValueError:
        raise EditError(_("Please enter a valid date.")) from None
    store = Store(box.workspace / "data" / "state.db")
    try:
        row = store.get(key)
        if row is None or not store.set_manual_expiry(key, expires):
            raise EditError(_("This mail is not in the log."))
    finally:
        store.close()
    if expires is None:
        return _("Expiry date removed.")
    text = _("Valid until %(date)s saved.", date=i18n.date(expires.isoformat()))
    target = expired_target(box.cfg, row["category"])
    if expires < date.today() and target and row["expired_tagged"] != 1:
        text += " " + _("The next run moves the mail to %(folder)s.", folder=target)
    return text


# ---------------------------------------------------------------- new mailbox

def _new_mailbox_page(request: Request, form: dict | None = None, error: str | None = None, status: int = 200,
                      retype: bool = False, retype_secret: bool = False):
    boxes = _boxes(request)
    blocked = can_add_mailbox(request.app.state.base_dir)
    root = request.app.state.base_dir / "mailboxes"
    return _page(request, "mailbox_new.html", {
        **_sidebar(request, boxes, None, "all"), "boxes": boxes, "blocked": blocked, "error": error,
        "example_categories": {code: len(tomllib.loads(path.read_text(encoding="utf-8"))["categories"])
                               for code, path in EXAMPLE_MAILBOXES.items()},
        "languages": i18n.LANGUAGES, "retype": retype, "retype_secret": retype_secret, "secrets_file": SECRETS_FILE,
        "kinds": {"gmail": {"label": _("Gmail"), "detail": _("imap.gmail.com · sign-in with Google"),
                            "auth": "google"},
                  "outlook": {"label": _("Outlook.com / Microsoft 365"),
                              "detail": _("outlook.office365.com · sign-in with Microsoft"), "auth": "microsoft"},
                  "imap": {"label": _("Other IMAP server"), "detail": _("any provider · user and password"),
                           "auth": "password"}},
        "taken_ids": sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else [],
        "form": form or {"imap_port": "993", "source_folder": "INBOX",
                         "template": next(iter(boxes), EXAMPLE + i18n.language())}},
        status)


@router.get("/ui/mailboxes/new", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def mailbox_new(request: Request):
    return _new_mailbox_page(request)


@router.post("/ui/mailboxes/new", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def mailbox_create(request: Request):
    form = await _form(request)
    boxes = _boxes(request)
    base = request.app.state.base_dir
    try:
        reason = can_add_mailbox(base)
        if reason:
            raise EditError(reason)
        form = with_kind(form)
        choice = str(form.get("template") or "")
        template_file: Path | None
        template_name = ""
        if choice.startswith(EXAMPLE) and choice[len(EXAMPLE):] in EXAMPLE_MAILBOXES:
            lang = choice[len(EXAMPLE):]
            template_file = EXAMPLE_MAILBOXES[lang]
            template_name = _("Standard categories, %(language)s", language=i18n.LANGUAGES[lang])
        elif choice in boxes and boxes[choice].config_file:
            template_file, template_name = boxes[choice].config_file, boxes[choice].name
        else:
            template_file = None
        if template_file is None:
            raise EditError(_("Please choose a template for the categories."))
        box_id = create_mailbox(base, _shared_path(request), template_file, template_name, form)
    except EditError as e:
        form, retype = without_secrets(form, "imap_password")
        form, retype_secret = without_secrets(form, "oauth_client_secret")
        return _new_mailbox_page(request, form=form, error=str(e), status=422, retype=retype,
                                 retype_secret=retype_secret)
    if str(form.get("imap_auth") or "password") != "password":  # next: the sign-in with the account
        _flash(request, _("Mailbox created. Now sign in with your account."))
        return RedirectResponse(f"/ui/m/{box_id}/oauth", status_code=303)
    _flash(request, _('Mailbox created. "Check connection" in the connection section tests it.'))
    return RedirectResponse(f"/ui/m/{box_id}/settings", status_code=303)


# ---------------------------------------------------------------- shared settings

# the fields of Global settings that show their own error (shared.html); an error about another one shows on top
SHARED_FIELDS = {"endpoint", "model", "api_key", "max_body_chars", "timeout_seconds", "min_interval_seconds",
                 "smtp_host", "smtp_security", "smtp_port", "smtp_user", "smtp_password", "mail_sender",
                 "mail_recipient", "ui_url", "digest_time"}


def _shared_page(request: Request, form: dict | None = None, error: EditError | None = None, status: int = 200,
                 retype: bool = False, retype_smtp: bool = False, tested: tuple[str, str, str] | None = None):
    boxes = _boxes(request)
    path = _shared_path(request)
    secrets = path.with_name(SECRETS_FILE)
    try:
        key_set, key_error = bool(classifier_key(secrets)), None
    except ConfigError as e:
        key_set, key_error = False, str(e)
    if form is None:
        classifier = _read_toml(path)["classifier"]
        form = {"endpoint": classifier.get("endpoint", ""), "model": classifier.get("model", ""),
                "max_body_chars": classifier.get("max_body_chars", 3000),
                "timeout_seconds": form_number(float(classifier.get("timeout_seconds", 20))),
                "min_interval_seconds": form_number(float(classifier.get("min_interval_seconds", 0))),
                "language": i18n.configured_language(path), "theme": i18n.configured_theme(path)}
        mail = mail_settings(path)
        form.update({"smtp_host": mail.host, "smtp_port": mail.port, "smtp_security": mail.security,
                     "smtp_user": mail.user, "mail_sender": mail.sender, "mail_recipient": mail.recipient,
                     "ui_url": mail.ui_url, "notify_failures": mail.notify_failures, "digest": mail.digest,
                     "digest_time": mail.digest_time})
    return _page(request, "shared.html", {
        **_sidebar(request, boxes, None, "shared"), "form": form, "editable": shared_writable(path),
        "config_name": path.name, "languages": i18n.LANGUAGES, "secrets_file": SECRETS_FILE, "retype": retype,
        "key_set": key_set, "key_error": key_error, "smtp_password_set": bool(smtp_password(path)),
        "retype_smtp": retype_smtp, "key_writable": secrets_writable(secrets),
        "tested": tested, "here": str(request.base_url).rstrip("/"),
        # an error about one field shows at that field, which gets the focus; others at the top
        "error": str(error) if error else None,
        "error_field": error.field if error and error.field in SHARED_FIELDS else None}, status)


@router.get("/ui/settings", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def shared_settings(request: Request):
    return _shared_page(request)


@router.post("/ui/sessions/end", dependencies=[Depends(require_login)])
async def end_sessions(request: Request):
    """Log out every browser: logins from before now are void (see web.sessions_ended)."""
    await _form(request)
    path = _shared_path(request).with_name(SECRETS_FILE)
    if not secrets_writable(path):
        _flash(request, _("%(file)s is not writable, so the sessions cannot be ended.", file=SECRETS_FILE), "err")
        return RedirectResponse("/ui/settings", status_code=303)
    write_secrets(path, "ui", {"sessions_ended": repr(time.time())})
    log.info("all admin UI sessions ended")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.post("/ui/settings", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def shared_settings_save(request: Request):
    form = await _form(request)
    try:
        save_shared(request.app.state.base_dir, _shared_path(request), form)
    except EditError as e:
        form, retype, retype_smtp = _shared_form_again(form)
        return _shared_page(request, form=form, error=e, status=422, retype=retype, retype_smtp=retype_smtp)
    _flash(request, _("Saved. Applies to all mailboxes from the next run."))
    return RedirectResponse("/ui/settings", status_code=303)


def _shared_form_again(form: dict) -> tuple[dict, bool, bool]:
    """The Global settings form to show again: (form, retype the API key, retype the SMTP password)."""
    form, retype = without_secrets(form, "api_key")
    form, retype_smtp = without_secrets(form, "smtp_password")
    for name in ("notify_failures", "digest"):  # shown again as they were ticked
        form[name] = _flag(form, name)
    return form, retype, retype_smtp


@router.post("/ui/settings/test/{card}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def shared_settings_test(request: Request, card: str):
    """Check model / Send test mail: try the values in the form without saving them, so a wrong one never
    reaches the scheduled runs. An empty API key or password means the stored one. The page's script asks
    for JSON and shows the result below the button; without JavaScript the page comes back."""
    if card not in ("model", "mail"):
        raise HTTPException(404)
    form = await _form(request)
    path = _shared_path(request)
    field = None
    try:
        if card == "model":
            values, key = classifier_from_form(form)
            tone, text = await run_in_threadpool(
                _check_model, values, key or classifier_key(path.with_name(SECRETS_FILE)))
        else:
            mail, password = mail_from_form(form)
            tone, text = await run_in_threadpool(_test_mail, MailSettings(**mail), password or smtp_password(path))
    except (EditError, ConfigError) as e:
        tone, text, field = "err", str(e), getattr(e, "field", None)
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"tone": tone, "text": text, "field": field})
    form, retype, retype_smtp = _shared_form_again(form)
    return _shared_page(request, form=form, retype=retype, retype_smtp=retype_smtp, tested=(card, tone, text))


def _test_mail(settings: MailSettings, password: str) -> tuple[str, str]:
    if not settings.ready:
        return "err", _("Please fill in the SMTP server, From and To first.")
    try:
        send(settings, password, _("Sortroom: test mail"),
             _("This is a test mail from Sortroom. Sending mail works.") + "\n")
    except MailError as e:
        return "err", _("The test mail could not be sent: %(e)s", e=e)
    return "ok", _("Test mail sent to %(recipient)s.", recipient=settings.recipient)


def _check_model(classifier: dict, key: str) -> tuple[str, str]:
    """Send the model one sample mail with the standard categories of the UI language."""
    if not key:
        return "err", _("The API key is not set yet.")
    client = ClassifierClient(key, classifier["endpoint"], classifier["model"],
                              timeout=float(classifier["timeout_seconds"]), retries=2)
    example = tomllib.loads(EXAMPLE_MAILBOXES[i18n.language()].read_text(encoding="utf-8"))["categories"]
    try:
        d = check_model(client, {k: v["description"] for k, v in example.items()})
    except ClassifierError as e:
        return "err", _("Model check failed: %(e)s", e=e)
    return "ok", _("Model works: a sample invoice was filed under \"%(category)s\" (confidence %(conf)s, "
                   "cost %(cost)s).",
                   category=d.category, conf=i18n.conf(d.confidence), cost=i18n.usd(d.cost, 6))
