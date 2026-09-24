"""HTTP API so n8n (or anything else) can trigger runs.

    uvicorn email_sorter.api:app --host 0.0.0.0 --port 8765

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
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from .config import ConfigError, Mailbox, load_credentials, load_mailboxes
from .runtime import BASE_DIR, _lock_is_stale, setup_logging, single_instance
from .sorter import run, run_backfill, run_recheck_expiry

load_dotenv(BASE_DIR / ".env")
setup_logging(verbose=False)
log = logging.getLogger("email_sorter.api")

CONFIG_PATH = Path(os.environ.get("EMAIL_SORTER_CONFIG", BASE_DIR / "config.toml"))
MAX_JOBS = 50  # finished backfill jobs kept in memory for GET /jobs

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _mailboxes() -> dict[str, Mailbox]:
    try:
        return load_mailboxes(BASE_DIR, CONFIG_PATH)
    except ConfigError as e:
        raise HTTPException(500, f"configuration error: {e}") from None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # In a container the server is PID 1 after every restart, so a lock left behind by a
    # crashed process would look alive forever. No run can be active before startup.
    try:
        boxes = load_mailboxes(BASE_DIR, CONFIG_PATH).values()
    except ConfigError:
        boxes = []
    for box in boxes:
        if box.lock_path.exists():
            log.info("[%s] removing run lock left over from a previous process", box.id)
            box.lock_path.unlink(missing_ok=True)
    yield


app = FastAPI(title="email-sorter", version=__version__, lifespan=_lifespan)


def _require_token(authorization: str = Header(default="")) -> None:
    token = os.environ.get("API_TOKEN", "")
    if len(token) < 16:
        raise HTTPException(500, "API_TOKEN is not configured (at least 16 characters)")
    if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "invalid or missing bearer token")


def _busy(box: Mailbox) -> bool:
    return box.lock_path.exists() and not _lock_is_stale(box.lock_path)


def _pick(boxes: dict[str, Mailbox], box_id: str | None) -> Mailbox:
    """The mailbox a single-mailbox task works on: the one named, or the only one."""
    if box_id:
        if box_id not in boxes:
            raise HTTPException(404, f"unknown mailbox {box_id!r}")
        return boxes[box_id]
    if len(boxes) == 1:
        return next(iter(boxes.values()))
    raise HTTPException(422, f"several mailboxes configured, name one: {', '.join(boxes)}")


def _credentials(box: Mailbox):
    try:
        return load_credentials(box.cfg)
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
    return [{"id": b.id, "name": b.name, "busy": _busy(b)} for b in _mailboxes().values()]


@app.post("/run", dependencies=[Depends(_require_token)])
def run_now(req: RunRequest | None = None) -> dict:
    req = req or RunRequest()
    boxes = _mailboxes()
    selected = [_pick(boxes, req.mailbox)] if req.mailbox else list(boxes.values())
    results: dict[str, dict] = {}
    for box in selected:
        creds = _credentials(box)
        result = _run_locked(box, "LIVE run" if req.live else "dry run",
                             lambda: run(box.cfg, creds, box.workspace, live=req.live, limit=req.limit))
        if result is None:
            if req.mailbox:
                raise HTTPException(409, f"[{box.id}] another run is active")
            result = {"ok": True, "skipped": "another run is active"}
        results[box.id] = result
    return {"ok": all(r["ok"] for r in results.values()), "results": results}


@app.post("/recheck-expiry", dependencies=[Depends(_require_token)])
def recheck(req: RecheckRequest | None = None) -> dict:
    req = req or RecheckRequest()
    box = _pick(_mailboxes(), req.mailbox)
    creds = _credentials(box)
    result = _run_locked(box, "expiry recheck", lambda: run_recheck_expiry(box.cfg, creds, box.workspace, live=req.live))
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
    job = {"id": uuid.uuid4().hex[:12], "mailbox": box.id, "status": "running", "started": _now(), "finished": None,
           "request": req.model_dump(mode="json"), "result": None, "error": None}
    with _jobs_lock:
        _jobs[job["id"]] = job
        for old in [j for j in _jobs.values() if j["status"] != "running"][:-MAX_JOBS]:
            _jobs.pop(old["id"], None)

    def work() -> None:
        try:
            with single_instance(box.lock_path) as acquired:
                if not acquired:
                    job.update(status="rejected", error="another run is active")
                    return
                log.info("API [%s]: starting %s backfill since %s", box.id, "LIVE" if req.live else "dry", req.since)
                result = run_backfill(box.cfg, creds, box.workspace, live=req.live, since=req.since, limit=req.limit)
                job.update(status="done", result=result.as_dict())
        except Exception as e:
            log.exception("API [%s]: backfill failed", box.id)
            job.update(status="failed", error=str(e))
        finally:
            job["finished"] = _now()

    threading.Thread(target=work, name=f"backfill-{job['id']}", daemon=True).start()
    return job


@app.get("/jobs/{job_id}", dependencies=[Depends(_require_token)])
def get_job(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job (jobs are kept in memory until the container restarts)")
    return job


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- web UI (session login, see email_sorter/web)

from urllib.parse import quote  # noqa: E402

from fastapi import Request  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

from . import web  # noqa: E402

app.state.load_mailboxes = lambda: load_mailboxes(BASE_DIR, CONFIG_PATH)
app.state.is_busy = _busy
app.state.config_path = CONFIG_PATH
app.add_middleware(SessionMiddleware, secret_key=web.session_secret(), session_cookie="email_sorter_session",
                   max_age=web.SESSION_DAYS * 86400, same_site="lax",
                   https_only=os.environ.get("UI_SECURE_COOKIES") == "1")
app.mount("/ui/static", StaticFiles(directory=str(web.HERE / "static")), name="static")
app.include_router(web.router)


@app.exception_handler(web.LoginRequired)
def _to_login(request: Request, exc: web.LoginRequired):
    return RedirectResponse(f"/login?next={quote(exc.next_url, safe='/')}", status_code=303)
