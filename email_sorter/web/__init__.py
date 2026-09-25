"""Web UI: server-rendered pages (Jinja2), session login with ADMIN_PASSWORD.

Mounted by email_sorter.api. Pages read each mailbox's state.db read-only; nothing here talks to
IMAP or Jev, so pages load fast and never change mail.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import ConfigError, Mailbox, default_label
from . import queries

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
router = APIRouter()

SESSION_DAYS = 7
_failed_logins: dict[str, list[float]] = {}
_failed_lock = threading.Lock()


# ---------------------------------------------------------------- auth

def admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "")


def session_secret() -> str:
    """Stable across restarts without another setting; changes when the password or token changes."""
    explicit = os.environ.get("SESSION_SECRET")
    if explicit:
        return explicit
    seed = f"email-sorter-session:{admin_password()}:{os.environ.get('API_TOKEN', '')}"
    return hashlib.sha256(seed.encode()).hexdigest()


class LoginRequired(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def require_login(request: Request) -> None:
    if not request.session.get("user"):
        raise LoginRequired(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))


def _throttle(ip: str) -> None:
    """After failed logins from an address, each further attempt waits a little longer (max 10 s)."""
    with _failed_lock:
        recent = [t for t in _failed_logins.get(ip, []) if time.time() - t < 900]
        _failed_logins[ip] = recent
    time.sleep(min(len(recent), 10))


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/ui"):
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(next), "error": None, "configured": len(admin_password()) >= 8})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, password: str = Form(""), next: str = Form("/ui")):
    ip = request.client.host if request.client else "?"
    configured = len(admin_password()) >= 8
    _throttle(ip)
    if configured and hmac.compare_digest(password.encode(), admin_password().encode()):
        with _failed_lock:
            _failed_logins.pop(ip, None)
        request.session.clear()
        request.session["user"] = "admin"
        request.session["since"] = int(time.time())
        return RedirectResponse(_safe_next(next), status_code=303)
    with _failed_lock:
        _failed_logins.setdefault(ip, []).append(time.time())
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(next), "configured": configured,
        "error": "Falsches Passwort." if configured else None}, status_code=401)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _safe_next(url: str) -> str:
    """Only local paths, so the login form can't be used to redirect elsewhere."""
    return url if url.startswith("/") and not url.startswith("//") else "/ui"


# ---------------------------------------------------------------- formatting

def de_num(value, decimals: int = 0) -> str:
    if value is None:
        return "–"
    s = f"{value:,.{decimals}f}"
    return s.replace(",", " ").replace(".", ",")


def de_conf(value) -> str:
    return "–" if value is None else f"{value:.2f}".replace(".", ",")


def de_dt(value: str | None, with_year: bool = False) -> str:
    if not value:
        return "–"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%d.%m.%Y %H:%M" if with_year else "%d.%m. %H:%M")


def de_date(value: str | None) -> str:
    if not value:
        return "–"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value


def usd(value, decimals: int = 2) -> str:
    return f"${(value or 0):.{decimals}f}"


def ago(value: str | None) -> str:
    if not value:
        return "noch nie"
    try:
        delta = datetime.now() - datetime.fromisoformat(value)
    except ValueError:
        return value
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "gerade eben"
    if minutes < 60:
        return f"vor {minutes} Min"
    if minutes < 48 * 60:
        return f"vor {minutes // 60} Std"
    return f"vor {minutes // 1440} Tagen"


for _name, _fn in (("de_num", de_num), ("de_conf", de_conf), ("de_dt", de_dt), ("de_date", de_date),
                   ("usd", usd), ("ago", ago)):
    templates.env.filters[_name] = _fn
templates.env.globals["version"] = __version__

RUN_KINDS = {"run": "Lauf", "backfill": "Backfill", "resort": "Re-Sort", "recheck": "Ablauf-Prüfung"}


def run_status(run: dict) -> tuple[str, str]:
    """(label, tone) for a run's status pill."""
    if run.get("error"):
        return ("Abgebrochen", "err")
    if run.get("exit_code", 0) != 0:
        return (f"{run.get('failed') or ''} Fehler".strip(), "warn")
    if not run.get("live"):
        return ("Probelauf", "neutral")
    if not run.get("classified"):
        return ("Nichts zu tun", "neutral")
    return ("OK", "ok")


templates.env.globals["run_status"] = run_status
templates.env.globals["run_kinds"] = RUN_KINDS


# ---------------------------------------------------------------- page helpers

def _boxes(request: Request) -> dict[str, Mailbox]:
    try:
        return request.app.state.load_mailboxes()
    except ConfigError as e:
        raise HTTPException(500, f"Konfigurationsfehler: {e}") from None


def _box(request: Request, box_id: str) -> tuple[dict[str, Mailbox], Mailbox]:
    boxes = _boxes(request)
    if box_id not in boxes:
        raise HTTPException(404, "Unbekanntes Postfach")
    return boxes, boxes[box_id]


def _sidebar(request: Request, boxes: dict[str, Mailbox], current: Mailbox | None, active: str) -> dict:
    busy = request.app.state.is_busy
    items = []
    for b in boxes.values():
        db = queries.connect(b.workspace)
        try:
            last = queries.stats(db, b.cfg.min_confidence).last_run
        finally:
            if db:
                db.close()
        tone = "busy" if busy(b) else ("err" if last and last.get("exit_code") else "ok")
        items.append({"id": b.id, "name": b.name, "host": b.cfg.imap_host, "tone": tone})
    current_item = next((i for i in items if current and i["id"] == current.id), None)
    return {"sidebar_boxes": items, "current": current_item, "active": active}


def _label(box: Mailbox, key: str) -> str:
    if not key:
        return "Posteingang"
    cat = box.cfg.categories.get(key)
    return cat.label if cat else default_label(key)


def _folder_label(path: str | None) -> str:
    if not path:
        return "Posteingang"
    return path.split("/")[-1] if path.count("/") <= 1 else "/".join(path.split("/")[1:])


templates.env.filters["folder_label"] = _folder_label


# ---------------------------------------------------------------- pages

@router.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui", status_code=303)


@router.get("/ui", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def all_mailboxes(request: Request):
    boxes = _boxes(request)
    cards, totals = [], {"today": 0, "uncertain": 0, "cost": 0.0}
    for b in boxes.values():
        db = queries.connect(b.workspace)
        try:
            st = queries.stats(db, b.cfg.min_confidence)
        finally:
            if db:
                db.close()
        cards.append({"box": b, "stats": st, "busy": request.app.state.is_busy(b),
                      "categories": len(b.cfg.categories)})
        totals["today"] += st.sorted_today
        totals["uncertain"] += st.uncertain
        totals["cost"] += st.cost_month
    return templates.TemplateResponse(request, "mailboxes.html", {
        **_sidebar(request, boxes, None, "all"), "cards": cards, "totals": totals,
        "month": datetime.now().strftime("%B")})


@router.get("/ui/m/{box_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def overview(request: Request, box_id: str):
    boxes, box = _box(request, box_id)
    cfg = box.cfg
    db = queries.connect(box.workspace)
    try:
        st = queries.stats(db, cfg.min_confidence)
        dist = queries.distribution(db, 7)
        runs = queries.recent_runs(db, 8)
        review = queries.uncertain_mails(db, cfg.min_confidence, 5)
        expired = queries.expired_moved(db, 7)
    finally:
        if db:
            db.close()
    total_7d = sum(n for _, n in dist)
    top = max((n for _, n in dist), default=0)
    bars = [{"label": _label(box, c), "n": n, "pct": round(100 * n / top, 1) if top else 0,
             "tone": "inbox" if c == "" else ("danger" if c == "verdaechtig" else "")} for c, n in dist]
    return templates.TemplateResponse(request, "overview.html", {
        **_sidebar(request, boxes, box, "overview"), "box": box, "stats": st, "bars": bars,
        "total_7d": total_7d, "runs": runs, "review": review, "expired_7d": expired,
        "label": lambda k: _label(box, k)})


@router.get("/ui/m/{box_id}/mails", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def mails(request: Request, box_id: str, q: str = "", category: str = "", folder: str = "", period: str = "7d",
          uncertain: str = "", flagged: str = "", page: int = 1, key: str = ""):
    boxes, box = _box(request, box_id)
    f = queries.MailFilter(q=q.strip()[:200], category=category, folder=folder,
                           period=period if period in queries.PERIODS else "7d",
                           uncertain=bool(uncertain), flagged=bool(flagged), page=max(page, 1))
    db = queries.connect(box.workspace)
    try:
        rows, total = queries.mails(db, f, box.cfg.min_confidence)
        selected = queries.mail(db, key)
        folder_list = queries.folders(db)
    finally:
        if db:
            db.close()
    params = {k: v for k, v in {"q": f.q, "category": f.category, "folder": f.folder, "period": f.period,
                                "uncertain": "1" if f.uncertain else "", "flagged": "1" if f.flagged else ""}.items() if v}
    pages = max(1, -(-total // queries.PAGE_SIZE))
    return templates.TemplateResponse(request, "mails.html", {
        **_sidebar(request, boxes, box, "mails"), "box": box, "f": f, "rows": rows, "total": total,
        "selected": selected, "folders": folder_list, "pages": pages, "periods": queries.PERIODS,
        "query": urlencode(params), "label": lambda k: _label(box, k),
        "link_key": lambda k: quote(k, safe=""), "min_conf": box.cfg.min_confidence})


from . import admin, editor  # noqa: E402,F401  (register their pages on router)
