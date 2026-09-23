"""HTTP API so n8n (or anything else) can trigger runs.

    uvicorn email_sorter.api:app --host 0.0.0.0 --port 8765

Every endpoint except /health needs `Authorization: Bearer <API_TOKEN>`.
Runs share the same lock as the CLI; a second run while one is active gets 409.

    POST /run              normal run (new mail of the last lookback_days)   -> summary
    POST /backfill         manual backfill from a date, runs in background   -> 202 + job
    GET  /jobs/{id}        status and summary of a backfill job
    POST /recheck-expiry   find expiry dates of already sorted offers        -> summary
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

from .config import ConfigError, load_config, load_credentials
from .runtime import BASE_DIR, LOCK_PATH, _lock_is_stale, setup_logging, single_instance
from .sorter import run, run_backfill, run_recheck_expiry

load_dotenv(BASE_DIR / ".env")
setup_logging(verbose=False)
log = logging.getLogger("email_sorter.api")

CONFIG_PATH = Path(os.environ.get("EMAIL_SORTER_CONFIG", BASE_DIR / "config.toml"))
MAX_JOBS = 50  # finished backfill jobs kept in memory for GET /jobs

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # In a container the server is PID 1 after every restart, so a lock left behind by a
    # crashed process would look alive forever. No run can be active before startup.
    if LOCK_PATH.exists():
        log.info("removing run lock left over from a previous process")
        LOCK_PATH.unlink(missing_ok=True)
    yield


app = FastAPI(title="email-sorter", version="1.0", lifespan=_lifespan)


def _require_token(authorization: str = Header(default="")) -> None:
    token = os.environ.get("API_TOKEN", "")
    if len(token) < 16:
        raise HTTPException(500, "API_TOKEN is not configured (at least 16 characters)")
    if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "invalid or missing bearer token")


def _load():
    try:
        cfg = load_config(CONFIG_PATH)
        return cfg, load_credentials(cfg)
    except ConfigError as e:
        raise HTTPException(500, f"configuration error: {e}") from None


def _busy() -> bool:
    return LOCK_PATH.exists() and not _lock_is_stale(LOCK_PATH)


def _run_locked(label: str, fn) -> dict:
    with single_instance(LOCK_PATH) as acquired:
        if not acquired:
            raise HTTPException(409, "another run is active")
        log.info("API: starting %s", label)
        try:
            return fn().as_dict()
        except Exception as e:
            log.exception("API: %s failed", label)
            raise HTTPException(500, f"{label} failed: {e}") from None


class RunRequest(BaseModel):
    live: bool = True
    limit: int | None = Field(default=None, ge=1)


class BackfillRequest(BaseModel):
    since: date
    live: bool = False  # like the CLI: a dry run unless asked otherwise
    limit: int | None = Field(default=None, ge=1)


class RecheckRequest(BaseModel):
    live: bool = True


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "busy": _busy()}


@app.post("/run", dependencies=[Depends(_require_token)])
def run_now(req: RunRequest | None = None) -> dict:
    req = req or RunRequest()
    cfg, creds = _load()
    return _run_locked("LIVE run" if req.live else "dry run",
                       lambda: run(cfg, creds, BASE_DIR, live=req.live, limit=req.limit))


@app.post("/recheck-expiry", dependencies=[Depends(_require_token)])
def recheck(req: RecheckRequest | None = None) -> dict:
    req = req or RecheckRequest()
    cfg, creds = _load()
    return _run_locked("expiry recheck", lambda: run_recheck_expiry(cfg, creds, BASE_DIR, live=req.live))


@app.post("/backfill", status_code=202, dependencies=[Depends(_require_token)])
def backfill(req: BackfillRequest) -> dict:
    if req.since > date.today():
        raise HTTPException(422, "since must not be in the future")
    if _busy():
        raise HTTPException(409, "another run is active")
    cfg, creds = _load()
    job = {"id": uuid.uuid4().hex[:12], "status": "running", "started": _now(), "finished": None,
           "request": req.model_dump(mode="json"), "result": None, "error": None}
    with _jobs_lock:
        _jobs[job["id"]] = job
        for old in [j for j in _jobs.values() if j["status"] != "running"][:-MAX_JOBS]:
            _jobs.pop(old["id"], None)

    def work() -> None:
        try:
            with single_instance(LOCK_PATH) as acquired:
                if not acquired:
                    job.update(status="rejected", error="another run is active")
                    return
                log.info("API: starting %s backfill since %s", "LIVE" if req.live else "dry", req.since)
                result = run_backfill(cfg, creds, BASE_DIR, live=req.live, since=req.since, limit=req.limit)
                job.update(status="done", result=result.as_dict())
        except Exception as e:
            log.exception("API: backfill failed")
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
