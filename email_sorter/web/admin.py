"""Pages that act on mail: maintenance jobs, single-mail corrections, adding a mailbox, shared settings."""
from __future__ import annotations

import logging
import os
import tomllib
from datetime import date
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import jobs
from ..check import check
from ..config import EXAMPLE_MAILBOX, INBOX_ACTION, ConfigError, Mailbox, _read_toml, load_credentials
from ..maintenance import relocate_category, rename_category, rename_folder
from ..manual import ManualError, move_mail
from ..reconcile import run_reconcile
from ..resort import run_resort
from ..runtime import single_instance
from ..sorter import RunResult, expired_target, run, run_backfill, run_recheck_expiry
from ..store import Store
from . import _box, _boxes, _sidebar, queries, require_login, router
from .editing import (EditError, add_sender_rule, can_add_mailbox, create_mailbox, rename_category_key,
                      rename_folder_refs, save_shared, shared_writable, writable)
from .editor import _flash, _form, _page, _shared_path, form_number

log = logging.getLogger(__name__)

EXAMPLE = "_example"  # template choice: the built-in example mailbox

TASKS = {
    "run": "Lauf",
    "backfill": "Backfill",
    "recheck": "Ablaufdaten nachprüfen",
    "resort": "Ordner neu einsortieren",
    "relocate": "Kategorie in ihren Ordner nachziehen",
    "rename_folder": "Ordner umbenennen",
    "rename_category": "Kategorie-Schlüssel umbenennen",
    "reconcile": "Protokoll mit Postfach abgleichen",
    "check": "Verbindung prüfen",
}
RISKY_TASKS = {"rename_folder", "rename_category"}  # change the server and the settings


def _env_status(box: Mailbox) -> list[tuple[str, bool]]:
    cfg = box.cfg
    return [(name, bool(os.environ.get(name))) for name in (cfg.imap_user_env, cfg.imap_password_env, cfg.jev_api_key_env)]


# ---------------------------------------------------------------- maintenance

@router.get("/ui/m/{box_id}/maintenance", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def maintenance(request: Request, box_id: str):
    boxes, box = _box(request, box_id)
    db = queries.connect(box.workspace)
    try:
        folders = queries.folders(db)
    finally:
        if db:
            db.close()
    cat_folders = sorted({c.folder for c in box.cfg.categories.values() if c.folder} | set(folders))
    return _page(request, "maintenance.html", {
        **_sidebar(request, boxes, box, "maintenance"), "box": box, "tasks": TASKS, "env": _env_status(box),
        "jobs": jobs.recent(box.id, 15), "folders": cat_folders, "busy": request.app.state.is_busy(box),
        "editable": writable(box), "today": date.today().isoformat()})


def _flag(form: dict, name: str) -> bool:
    return form.get(name) in ("1", "on", "true")


def _limit(form: dict) -> int | None:
    raw = str(form.get("limit") or "").strip()
    if not raw:
        return None
    if not raw.isdigit() or not 1 <= int(raw) <= 100000:
        raise EditError("Anzahl: eine ganze Zahl ab 1.")
    return int(raw)


def _job_for(request: Request, box: Mailbox, task: str, form: dict):
    """(label, fn, needs_lock) for a maintenance form."""
    cfg, ws, live = box.cfg, box.workspace, _flag(form, "live")
    mode = "" if live else " (Probelauf)"
    creds = load_credentials(cfg)
    shared = _shared_path(request)
    if task == "run":
        limit = _limit(form)
        return f"Lauf{mode}", lambda: run(cfg, creds, ws, live=live, limit=limit), True
    if task == "backfill":
        try:
            since = date.fromisoformat(str(form.get("since") or ""))
        except ValueError:
            raise EditError("Backfill: bitte ein Startdatum angeben.") from None
        if since > date.today():
            raise EditError("Backfill: das Startdatum liegt in der Zukunft.")
        limit = _limit(form)
        return (f"Backfill seit {since:%d.%m.%Y}{mode}",
                lambda: run_backfill(cfg, creds, ws, live=live, since=since, limit=limit), True)
    if task == "recheck":
        return f"Ablaufdaten nachprüfen{mode}", lambda: run_recheck_expiry(cfg, creds, ws, live=live), True
    if task == "resort":
        folder = str(form.get("folder") or "").strip()
        if not folder:
            raise EditError("Bitte einen Ordner wählen.")
        limit = _limit(form)
        return (f"{folder} neu einsortieren{mode}",
                lambda: run_resort(cfg, creds, ws, folder, live=live, limit=limit), True)
    if task == "relocate":
        category = str(form.get("category") or "")
        if category not in cfg.categories or not cfg.categories[category].folder:
            raise EditError("Bitte eine Kategorie mit Zielordner wählen.")
        return (f"{cfg.categories[category].label} nach {cfg.categories[category].folder} nachziehen{mode}",
                lambda: relocate_category(cfg, creds, category, live=live, base_dir=ws), True)
    if task == "rename_folder":
        old, new = (str(form.get(k) or "").strip().strip("/") for k in ("old", "new"))
        if not old or not new or old == new:
            raise EditError("Bitte alten und neuen Ordnernamen angeben.")
        if old.upper() == "INBOX":
            raise EditError("Der Posteingang selbst kann nicht umbenannt werden.")
        if live and not writable(box):
            raise EditError("Die Einstellungen sind schreibgeschützt – die Ordner in den Kategorien "
                            "ließen sich nicht nachziehen.")

        def rename() -> RunResult:
            result = rename_folder(cfg, creds, old, new, live=live, base_dir=ws)
            if live and result.exit_code == 0:
                n = rename_folder_refs(box, shared, old, new)
                log.info("%d folder setting(s) now point to %s", n, new)
            return result
        return f"Ordner {old} → {new}{mode}", rename, True
    if task == "rename_category":
        old, new = str(form.get("old") or ""), str(form.get("new") or "").strip().lower()
        if old not in cfg.categories:
            raise EditError("Bitte eine Kategorie wählen.")
        if not new or new in cfg.categories:
            raise EditError("Der neue Schlüssel fehlt oder ist schon vergeben.")
        if live and not writable(box):
            raise EditError("Die Einstellungen sind schreibgeschützt.")

        def rename_key() -> RunResult:
            if live:
                rename_category_key(box, shared, old, new)
            return rename_category(old, new, live=live, base_dir=ws)
        return f"Kategorie {old} → {new}{mode}", rename_key, True
    if task == "reconcile":
        return f"Protokoll abgleichen{mode}", lambda: run_reconcile(cfg, creds, ws, live=live), True
    if task == "check":
        def run_check() -> dict:
            code = check(cfg, creds, out=lambda line: log.info("%s", line.strip("\n")))
            return {"ok": code == 0, "exit_code": code}
        return "Verbindung prüfen", run_check, False
    raise HTTPException(404, "Unbekannte Aufgabe")


@router.post("/ui/m/{box_id}/maintenance/{task}", dependencies=[Depends(require_login)])
async def maintenance_start(request: Request, box_id: str, task: str):
    form = await _form(request)
    _, box = _box(request, box_id)
    if task not in TASKS:
        raise HTTPException(404, "Unbekannte Aufgabe")
    try:
        label, fn, needs_lock = _job_for(request, box, task, form)
    except (EditError, ConfigError) as e:
        _flash(request, str(e), "err")
        return RedirectResponse(f"/ui/m/{box.id}/maintenance", status_code=303)
    if needs_lock and (request.app.state.is_busy(box) or jobs.running(box.id)):
        _flash(request, "Für dieses Postfach läuft gerade etwas. Bitte warten, bis es fertig ist.", "warn")
        return RedirectResponse(f"/ui/m/{box.id}/maintenance", status_code=303)
    job = jobs.start(box, task, label, fn, request={k: v for k, v in form.items() if k != "csrf"},
                     needs_lock=needs_lock)
    return RedirectResponse(f"/ui/m/{box.id}/jobs/{job['id']}", status_code=303)


@router.get("/ui/m/{box_id}/jobs/{job_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def job_page(request: Request, box_id: str, job_id: str):
    boxes, box = _box(request, box_id)
    job = jobs.get(job_id)
    if not job or job["mailbox"] != box.id:
        raise HTTPException(404, "Unbekannter Job (Jobs werden nur bis zum Neustart aufbewahrt)")
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
    _, box = _box(request, box_id)
    key, action = str(form.get("key") or ""), str(form.get("action") or "")
    back = str(form.get("back") or "")
    back = f"/ui/m/{box.id}/mails?{back}" if back and not back.startswith(("/", "http")) else \
        f"/ui/m/{box.id}/mails?period=all&key={quote(key, safe='')}"
    category = str(form.get("category") or "")

    if action == "expiry":  # only the log changes, no IMAP and no lock needed
        try:
            _flash(request, _set_expiry(box, key, form))
        except EditError as e:
            _flash(request, str(e), "err")
        return RedirectResponse(back, status_code=303)

    def act() -> str:
        creds = load_credentials(box.cfg)
        if action == "rule":
            match = str(form.get("match") or "").strip().lower()
            if not match:
                raise EditError("Bitte einen Absender angeben.")
            add_sender_rule(box, _shared_path(request), match, category)
            text = f"Absender-Regel für {match} gespeichert."
            if _flag(form, "apply"):
                move_mail(box.cfg, creds, box.workspace, key, category)
                text += " Die Mail wurde verschoben."
            return text
        if action in ("accept", "move"):
            folder = move_mail(box.cfg, creds, box.workspace, key, category)
            return f"Verschoben nach {folder}." if folder else "Liegt jetzt im Posteingang."
        raise EditError("Unbekannte Aktion.")

    def locked() -> str:
        with single_instance(box.lock_path) as acquired:
            if not acquired:
                raise EditError("Für dieses Postfach läuft gerade ein Lauf. Bitte gleich noch einmal versuchen.")
            return act()

    try:
        _flash(request, await run_in_threadpool(locked))
    except (EditError, ManualError, ConfigError) as e:
        _flash(request, str(e), "err")
    except Exception as e:
        log.exception("[%s] mail action failed", box.id)
        _flash(request, f"Fehlgeschlagen: {e}", "err")
    return RedirectResponse(back, status_code=303)


def _set_expiry(box: Mailbox, key: str, form: dict) -> str:
    raw = "" if form.get("clear") else str(form.get("expires") or "").strip()
    try:
        expires = date.fromisoformat(raw) if raw else None
    except ValueError:
        raise EditError("Bitte ein gültiges Datum angeben.") from None
    store = Store(box.workspace / "data" / "state.db")
    try:
        row = store.get(key)
        if row is None or not store.set_manual_expiry(key, expires):
            raise EditError("Diese Mail steht nicht im Protokoll.")
    finally:
        store.close()
    if expires is None:
        return "Ablaufdatum entfernt."
    text = f"Gültig bis {expires:%d.%m.%Y} gespeichert."
    target = expired_target(box.cfg, row["category"])
    if expires < date.today() and target and row["expired_tagged"] != 1:
        text += f" Der nächste Lauf verschiebt die Mail nach {target}."
    return text


# ---------------------------------------------------------------- new mailbox

def _new_mailbox_page(request: Request, form: dict | None = None, error: str | None = None, status: int = 200):
    boxes = _boxes(request)
    blocked = can_add_mailbox(request.app.state.base_dir)
    return _page(request, "mailbox_new.html", {
        **_sidebar(request, boxes, None, "all"), "boxes": boxes, "blocked": blocked, "error": error,
        "example_categories": len(tomllib.loads(EXAMPLE_MAILBOX.read_text(encoding="utf-8"))["categories"]),
        "form": form or {"imap_port": "993", "source_folder": "INBOX", "template": next(iter(boxes), EXAMPLE)}},
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
        choice = str(form.get("template") or "")
        if choice == EXAMPLE:
            template_file, template_name = EXAMPLE_MAILBOX, "Standard-Kategorien"
        elif choice in boxes:
            template_file, template_name = boxes[choice].config_file, boxes[choice].name
        else:
            raise EditError("Bitte eine Vorlage für die Kategorien wählen.")
        box_id = create_mailbox(base, _shared_path(request), template_file, template_name, form)
    except EditError as e:
        return _new_mailbox_page(request, form=form, error=str(e), status=422)
    _flash(request, "Postfach angelegt. Zugangsdaten in die .env eintragen, Container neu starten und dann "
                    "„Verbindung prüfen“.")
    return RedirectResponse(f"/ui/m/{box_id}/maintenance", status_code=303)


# ---------------------------------------------------------------- shared settings

def _shared_page(request: Request, form: dict | None = None, error: str | None = None, status: int = 200):
    boxes = _boxes(request)
    path = _shared_path(request)
    if form is None:
        jev = _read_toml(path)["jev"]
        form = {"endpoint": jev.get("endpoint", ""), "model": jev.get("model", ""),
                "api_key_env": jev.get("api_key_env", "AI_GATEWAY_API_KEY"),
                "max_body_chars": jev.get("max_body_chars", 3000),
                "timeout_seconds": form_number(float(jev.get("timeout_seconds", 20))),
                "min_interval_seconds": form_number(float(jev.get("min_interval_seconds", 0)))}
    return _page(request, "shared.html", {
        **_sidebar(request, boxes, None, "shared"), "form": form, "error": error, "editable": shared_writable(path),
        "config_name": path.name, "key_set": bool(os.environ.get(str(form.get("api_key_env") or "")))}, status)


@router.get("/ui/settings", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def shared_settings(request: Request):
    return _shared_page(request)


@router.post("/ui/settings", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def shared_settings_save(request: Request):
    form = await _form(request)
    try:
        save_shared(request.app.state.base_dir, _shared_path(request), form)
    except EditError as e:
        return _shared_page(request, form=form, error=str(e), status=422)
    _flash(request, "Gespeichert. Gilt für alle Postfächer ab dem nächsten Lauf.")
    return RedirectResponse("/ui/settings", status_code=303)
