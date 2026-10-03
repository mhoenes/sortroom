"""Admin web UI, built-in schedule and an HTTP API for triggering runs from outside.

    uvicorn email_sorter.api:app --host 0.0.0.0 --port 8765

The schedule (email_sorter.scheduler) starts with the app and runs each mailbox every
[schedule] interval_minutes; SORTROOM_SCHEDULER=off disables it for the process.

Every endpoint except /health needs `Authorization: Bearer <API_TOKEN>`.
Each mailbox has its own lock, shared with the CLI; a run on a busy mailbox is skipped (409 when
that mailbox was asked for explicitly).

    GET  /mailboxes        configured mailboxes and whether one is busy
    POST /run              normal run, all mailboxes or {"mailbox": id}         -> summary per mailbox
    POST /backfill         manual backfill from a date, runs in background       -> 202 + job
    GET  /jobs/{id}        status and summary of a backfill job
    POST /recheck-expiry   find expiry dates of already sorted offers            -> summary
    GET  /health           liveness probe, no auth
"""
from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from functools import partial
from datetime import date

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import __version__, i18n, jobs
from .config import ConfigError, Mailbox, load_credentials, load_mailboxes
from .i18n import _
from .removal import MailboxBusy, delete_mailbox
from .runtime import BASE_DIR, default_config_path, is_locked, setup_logging, single_instance
from . import digest, notify
from .scheduler import Scheduler, enabled_by_env
from .sorter import run, run_backfill, run_recheck_expiry

load_dotenv(BASE_DIR / ".env")
setup_logging(verbose=False)
log = logging.getLogger("email_sorter.api")

CONFIG_PATH = default_config_path()


def _mailboxes() -> dict[str, Mailbox]:
    try:
        return load_mailboxes(BASE_DIR, CONFIG_PATH)
    except ConfigError as e:
        raise HTTPException(500, f"configuration error: {e}") from None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        boxes = list(load_mailboxes(BASE_DIR, CONFIG_PATH).values())
    except ConfigError:
        boxes = []
    for box in boxes:  # the PID lock files of versions up to 0.13.1; the lock is in mailboxes/.locks/ now
        (box.workspace / "data" / "run.lock").unlink(missing_ok=True)
    if enabled_by_env():
        app.state.scheduler.start()
    yield
    app.state.scheduler.stop()


# the API documentation is served below, only after logging in to the admin UI
app = FastAPI(title="Sortroom", version=__version__, lifespan=_lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)


def _require_token(authorization: str = Header(default="")) -> None:
    token = os.environ.get("API_TOKEN", "")
    if len(token) < 16:
        raise HTTPException(500, "API_TOKEN is not configured (at least 16 characters)")
    if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "invalid or missing bearer token")


def _busy(box: Mailbox) -> bool:
    return is_locked(box.lock_path)


def _pick(boxes: dict[str, Mailbox], box_id: str | None) -> Mailbox:
    """The mailbox a single-mailbox task works on: the one named, or the only one."""
    broken = getattr(boxes, "broken", {})
    if box_id in broken:
        raise HTTPException(500, f"[{box_id}] configuration error: {broken[box_id]}")
    if box_id:
        if box_id not in boxes:
            raise HTTPException(404, f"unknown mailbox {box_id!r}")
        return boxes[box_id]
    if not boxes:
        raise HTTPException(404, "no mailbox configured yet - add one in the admin UI")
    if len(boxes) == 1:
        return next(iter(boxes.values()))
    raise HTTPException(422, f"several mailboxes configured, name one: {', '.join(boxes)}")


def _credentials(box: Mailbox):
    try:
        return load_credentials(box)
    except ConfigError as e:
        raise HTTPException(500, f"[{box.id}] configuration error: {e}") from None


def _run_locked(box: Mailbox, label: str, fn) -> dict | None:
    """Run fn under the mailbox's lock; None when the mailbox is busy."""
    with single_instance(box.lock_path) as acquired:
        if not acquired:
            return None
        log.info("API [%s]: starting %s", box.id, label)
        try:
            return fn().as_dict()
        except Exception as e:
            log.exception("API [%s]: %s failed", box.id, label)
            raise HTTPException(500, f"[{box.id}] {label} failed: {e}") from None


class RunRequest(BaseModel):
    live: bool = True
    limit: int | None = Field(default=None, ge=1)
    mailbox: str | None = None  # none: every mailbox


class BackfillRequest(BaseModel):
    since: date
    live: bool = False  # like the CLI: a dry run unless asked otherwise
    limit: int | None = Field(default=None, ge=1)
    mailbox: str | None = None


class RecheckRequest(BaseModel):
    live: bool = True
    mailbox: str | None = None


@app.get("/health")
def health() -> dict:
    try:
        busy = any(_busy(b) for b in load_mailboxes(BASE_DIR, CONFIG_PATH).values())
    except ConfigError:
        busy = False
    return {"status": "ok", "busy": busy}


@app.get("/mailboxes", dependencies=[Depends(_require_token)])
def mailboxes() -> list[dict]:
    """The mailboxes; one whose settings don't load has "error" instead of "name" and "busy"."""
    boxes = _mailboxes()
    return ([{"id": b.id, "name": b.name, "busy": _busy(b)} for b in boxes.values()]
            + [{"id": box_id, "error": error} for box_id, error in getattr(boxes, "broken", {}).items()])


@app.delete("/mailboxes/{box_id}", dependencies=[Depends(_require_token)])
def mailbox_delete(box_id: str) -> dict:
    """Delete a mailbox for good (settings, login, log, reports; nothing on the IMAP server).
    "revoked": whether its Google sign-in was revoked (null: nothing to revoke)."""
    box = _pick(_mailboxes(), box_id)
    if _busy(box) or jobs.running(box.id):
        raise HTTPException(409, f"[{box.id}] something is running for this mailbox")
    try:
        result = delete_mailbox(box)
    except MailboxBusy:
        raise HTTPException(409, f"[{box.id}] something is running for this mailbox") from None
    log.info("API: mailbox %s deleted", box.id)
    return {"deleted": box.id, **result}


@app.post("/run", dependencies=[Depends(_require_token)])
def run_now(req: RunRequest | None = None) -> dict:
    req = req or RunRequest()
    boxes = _mailboxes()
    selected = [_pick(boxes, req.mailbox)] if req.mailbox else list(boxes.values())
    results: dict[str, dict] = {}
    for box in selected:
        creds = _credentials(box)
        result = _run_locked(box, "LIVE run" if req.live else "dry run",
                             partial(run, box.cfg, creds, box.workspace, live=req.live, limit=req.limit))
        if result is None:
            if req.mailbox:
                raise HTTPException(409, f"[{box.id}] another run is active")
            result = {"ok": True, "skipped": "another run is active"}
        results[box.id] = result
    if not req.mailbox:
        for box_id, error in getattr(boxes, "broken", {}).items():
            results[box_id] = {"ok": False, "error": f"configuration error: {error}"}
    return {"ok": all(r["ok"] for r in results.values()), "results": results}


@app.post("/recheck-expiry", dependencies=[Depends(_require_token)])
def recheck(req: RecheckRequest | None = None) -> dict:
    req = req or RecheckRequest()
    box = _pick(_mailboxes(), req.mailbox)
    creds = _credentials(box)
    result = _run_locked(box, "expiry recheck",
                         partial(run_recheck_expiry, box.cfg, creds, box.workspace, live=req.live))
    if result is None:
        raise HTTPException(409, f"[{box.id}] another run is active")
    return result


@app.post("/backfill", status_code=202, dependencies=[Depends(_require_token)])
def backfill(req: BackfillRequest) -> dict:
    if req.since > date.today():
        raise HTTPException(422, "since must not be in the future")
    box = _pick(_mailboxes(), req.mailbox)
    if _busy(box):
        raise HTTPException(409, f"[{box.id}] another run is active")
    creds = _credentials(box)
    # the label shows in the admin UI's job list, in its language (set by the language middleware)
    label = (_("Backfill since %(date)s", date=i18n.date(req.since.isoformat()))
             + ("" if req.live else f" ({_('dry run')})"))
    job = jobs.start(box, "backfill", label,
                     partial(run_backfill, box.cfg, creds, box.workspace, live=req.live, since=req.since,
                             limit=req.limit),
                     request=req.model_dump(mode="json"))
    return jobs.public(job)


@app.get("/jobs/{job_id}", dependencies=[Depends(_require_token)])
def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job (jobs are kept in memory until the container restarts)")
    return jobs.public(job)


# ---------------------------------------------------------------- web UI (session login, see email_sorter/web)

from urllib.parse import quote  # noqa: E402

from fastapi import Request  # noqa: E402
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html  # noqa: E402
from fastapi.responses import FileResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

from . import web  # noqa: E402

app.state.load_mailboxes = lambda: load_mailboxes(BASE_DIR, CONFIG_PATH)
app.state.is_busy = _busy
app.state.config_path = CONFIG_PATH
app.state.base_dir = BASE_DIR
app.state.scheduler = Scheduler(
    lambda: app.state.load_mailboxes(),
    # mail on failed scheduled tasks and the daily summary, both under Global settings → Mail
    report=lambda box, task, failed, why: notify.report(app.state.config_path, box, task, failed, why),
    digest_due=lambda now: digest.due(app.state.config_path, app.state.base_dir / "mailboxes", now),
    send_digest=lambda: digest.send_digest(app.state.config_path, app.state.load_mailboxes,
                                           app.state.base_dir / "mailboxes"))
app.add_middleware(SessionMiddleware, secret_key=web.session_secret(), session_cookie="email_sorter_session",
                   max_age=web.SESSION_DAYS * 86400, same_site="lax",
                   https_only=os.environ.get("UI_SECURE_COOKIES") == "1")
app.middleware("http")(web.language_middleware)
app.mount("/ui/static", StaticFiles(directory=str(web.HERE / "static")), name="static")
app.include_router(web.router)


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(web.require_login)])
def _openapi() -> dict:
    return app.openapi()


@app.get("/docs", include_in_schema=False, dependencies=[Depends(web.require_login)])
def _docs():
    """Swagger UI; it loads its viewer from cdn.jsdelivr.net (see PRIVACY.md)."""
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Sortroom API")


@app.get("/redoc", include_in_schema=False, dependencies=[Depends(web.require_login)])
def _redoc():
    return get_redoc_html(openapi_url="/openapi.json", title="Sortroom API")


@app.get("/favicon.ico", include_in_schema=False)
def _favicon():
    return FileResponse(web.HERE / "static" / "icon-32.png", media_type="image/png")


@app.exception_handler(web.LoginRequired)
def _to_login(request: Request, exc: web.LoginRequired):
    return RedirectResponse(f"/login?next={quote(exc.next_url, safe='/')}", status_code=303)
