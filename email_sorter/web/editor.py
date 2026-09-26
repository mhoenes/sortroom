"""Pages that change a mailbox's settings: categories (with a model trial) and mailbox settings.

Writes go through .editing (validated, atomic, with .bak). Every form carries a CSRF token kept in
the session. A mailbox whose file isn't writable (e.g. a config.toml mounted read-only) is shown
read-only.
"""
from __future__ import annotations

import contextvars
import logging
import secrets
import threading
import time
import uuid
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import INBOX_ACTION, ConfigError, Mailbox, load_credentials
from ..classifier import ClassifierAuthError
from ..i18n import _
from ..trial import OTHER_SAMPLE, OWN_SAMPLE, run_trial
from . import _box, _sidebar, queries, require_login, router, templates
from .editing import (EditError, delete_category, save_category, save_sender_rules,
                      save_settings, writable)

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
        "counts": counts, "error": error, "editable": writable(box), "rules_using": rules_using,
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
        creds = load_credentials(box.cfg)
    except ConfigError as e:
        raise HTTPException(500, str(e)) from None
    job = {"id": uuid.uuid4().hex[:12], "box": box.id, "key": key, "new": new, "description": description,
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
    }
    cat = box.cfg.categories.get(key)
    return _page(request, "trial.html", {
        **_sidebar(request, boxes, box, "categories"), "box": box, "job": job, "rows": rows, "s": summary,
        "cat_label": cat.label if cat else key,
        "label": lambda k: box.cfg.categories[k].label if k in box.cfg.categories else (
            k if k != key else _("%(key)s (new)", key=k))})


# ---------------------------------------------------------------- settings

def _settings_page(request: Request, box_id: str, form: dict | None = None, error: str | None = None,
                   rules: list[tuple[str, str]] | None = None, status: int = 200):
    boxes, box = _box(request, box_id)
    cfg = box.cfg
    if form is None:
        form = {"name": box.name, "imap_host": cfg.imap_host, "imap_port": cfg.imap_port,
                "source_folder": cfg.source_folder, "min_confidence": form_number(cfg.min_confidence),
                "action_flag_threshold": form_number(cfg.action_flag_threshold),
                "expiry_threshold": form_number(cfg.expiry_threshold), "min_age_hours": form_number(cfg.min_age_hours),
                "sort_read_at_once": cfg.sort_read_at_once,
                "schedule_enabled": cfg.schedule_enabled, "schedule_minutes": cfg.schedule_minutes,
                "lookback_days": cfg.lookback_days, "max_per_run": cfg.max_per_run,
                "expired_folder": cfg.expired_folder or ""}
    if rules is None:
        rules = [(r.match, r.action) for r in cfg.sender_rules]
    return _page(request, "settings.html", {
        **_sidebar(request, boxes, box, "settings"), "box": box, "cfg": cfg, "form": form, "error": error,
        "schedule": request.app.state.scheduler.status(box),
        "rules": rules, "editable": writable(box),
        "config_name": box.config_file.name if box.config_file else "–"}, status)


@router.get("/ui/m/{box_id}/settings", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def settings(request: Request, box_id: str):
    return _settings_page(request, box_id)


@router.post("/ui/m/{box_id}/settings", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def settings_save(request: Request, box_id: str):
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    try:
        save_settings(box, _shared_path(request), form)
    except EditError as e:
        return _settings_page(request, box_id, form=form, error=str(e), status=422)
    _flash(request, _("Settings saved. They apply from the next run."))
    return RedirectResponse(f"/ui/m/{box.id}/settings", status_code=303)


@router.post("/ui/m/{box_id}/settings/sender-rules", response_class=HTMLResponse,
             dependencies=[Depends(require_login)])
async def sender_rules_save(request: Request, box_id: str):
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    rules = []
    for i in range(int(form.get("rows") or 0)):  # removed rows leave gaps: empty, skipped when saving
        rules.append((str(form.get(f"match_{i}") or ""), str(form.get(f"action_{i}") or INBOX_ACTION)))
    try:
        save_sender_rules(box, _shared_path(request), rules)
    except EditError as e:
        return _settings_page(request, box_id, error=str(e), rules=[r for r in rules if r[0].strip()], status=422)
    _flash(request, _("Sender rules saved. They apply from the next run."))
    return RedirectResponse(f"/ui/m/{box.id}/settings#regeln", status_code=303)
