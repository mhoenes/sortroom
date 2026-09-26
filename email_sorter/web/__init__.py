"""Web UI: server-rendered pages (Jinja2), session login with ADMIN_PASSWORD.

Mounted by email_sorter.api. Pages read each mailbox's state.db read-only; nothing here talks to
IMAP or the classifier, so pages load fast and never change mail.
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

from .. import __version__, i18n
from ..config import ConfigError, Mailbox, default_label
from ..i18n import _
from ..sorter import expired_target
from . import queries

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_gettext_callables(i18n.translate, i18n.translate_plural, newstyle=True)
templates.env.globals["lang"] = i18n.language
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


async def language_middleware(request: Request, call_next):
    """Every request renders in the language set under Globale Einstellungen ([ui] language)."""
    token = i18n.set_language(i18n.configured_language(request.app.state.config_path))
    try:
        return await call_next(request)
    finally:
        i18n.reset_language(token)


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
        "error": _("Wrong password.") if configured else None}, status_code=401)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _safe_next(url: str) -> str:
    """Only local paths, so the login form can't be used to redirect elsewhere."""
    return url if url.startswith("/") and not url.startswith("//") else "/ui"


# ---------------------------------------------------------------- formatting

def usd(value, decimals: int = 2) -> str:
    return f"${(value or 0):.{decimals}f}"


for _name, _fn in (("num", i18n.num), ("conf", i18n.conf), ("dt", i18n.dt), ("date", i18n.date),
                   ("usd", usd), ("ago", i18n.ago)):
    templates.env.filters[_name] = _fn
templates.env.globals["version"] = __version__


def _asset_urls(static: Path) -> dict[str, str]:
    """name -> /ui/static/name?v=<content hash>. The URL changes whenever the file does, so a
    browser never keeps an outdated stylesheet or icon, independent of the version number."""
    return {p.name: f"/ui/static/{p.name}?v={hashlib.sha256(p.read_bytes()).hexdigest()[:10]}"
            for p in static.iterdir() if p.is_file()}


_ASSETS = _asset_urls(HERE / "static")
templates.env.globals["asset"] = _ASSETS.__getitem__  # unknown name: fails loudly while rendering
# AGPL-3.0 section 13: users of the web UI are offered the source. Point this at your own
# repository if you run a modified version for others.
templates.env.globals["source_url"] = os.environ.get("SOURCE_URL", "https://github.com/mhoenes/sortroom")

def run_kind(kind: str) -> str:
    return {"run": _("Run"), "backfill": _("Backfill"), "resort": _("Re-sort"),
            "recheck": _("Expiry check")}.get(kind, kind)


def run_status(run: dict) -> tuple[str, str]:
    """(label, tone) for a run's status pill."""
    if run.get("error"):
        return (_("Aborted"), "err")
    if run.get("exit_code", 0) != 0:
        failed = run.get("failed") or 0
        return (i18n.ngettext("%(num)s error", "%(num)s errors", failed) if failed else _("Errors"), "warn")
    if not run.get("live"):
        return (_("Dry run"), "neutral")
    if not run.get("classified"):
        return (_("Nothing to do"), "neutral")
    return ("OK", "ok")


# categories shown in red: suspicious mail (key of the German and the English standard categories)
DANGER_CATEGORIES = {"verdaechtig", "suspicious"}

templates.env.globals["run_status"] = run_status
templates.env.globals["run_kind"] = run_kind
templates.env.globals["danger"] = DANGER_CATEGORIES.__contains__


# ---------------------------------------------------------------- page helpers

def _boxes(request: Request) -> dict[str, Mailbox]:
    try:
        return request.app.state.load_mailboxes()
    except ConfigError as e:
        raise HTTPException(500, _("Configuration error: %(e)s", e=e)) from None


def _box(request: Request, box_id: str) -> tuple[dict[str, Mailbox], Mailbox]:
    boxes = _boxes(request)
    if box_id not in boxes:
        raise HTTPException(404, _("Unknown mailbox"))
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
        return _("Inbox")
    cat = box.cfg.categories.get(key)
    return cat.label if cat else default_label(key)


def _folder_label(path: str | None) -> str:
    if not path or path.strip("/").upper() == "INBOX":
        return _("Inbox")
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
        **_sidebar(request, boxes, None, "all"), "cards": cards, "totals": totals})


@router.get("/ui/m/{box_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def overview(request: Request, box_id: str):
    boxes, box = _box(request, box_id)
    cfg = box.cfg
    db = queries.connect(box.workspace)
    try:
        st = queries.stats(db, cfg.min_confidence)
        dist = queries.distribution(db, 7)
        runs = queries.recent_runs(db, 8)
        review = queries.uncertain_mails(db, cfg.min_confidence, 5, days=30)  # as stats().uncertain
        expired = queries.expired_moved(db, 7)
    finally:
        if db:
            db.close()
    total_7d = sum(n for _c, n in dist)
    top = max((n for _c, n in dist), default=0)
    bars = [{"label": _label(box, c), "n": n, "pct": round(100 * n / top, 1) if top else 0,
             "tone": "inbox" if c == "" else ("danger" if c in DANGER_CATEGORIES else "")} for c, n in dist]
    return templates.TemplateResponse(request, "overview.html", {
        **_sidebar(request, boxes, box, "overview"), "box": box, "stats": st, "bars": bars,
        "total_7d": total_7d, "runs": runs, "review": review, "expired_7d": expired,
        "schedule": request.app.state.scheduler.status(box), "label": lambda k: _label(box, k)})


@router.get("/ui/m/{box_id}/mails", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def mails(request: Request, box_id: str, q: str = "", category: str = "", folder: str = "", period: str = "7d",
          uncertain: str = "", flagged: str = "", gone: str = "", page: int = 1, key: str = ""):
    boxes, box = _box(request, box_id)
    f = queries.MailFilter(q=q.strip()[:200], category=category, folder=folder,
                           period=period if period in queries.PERIODS else "7d",
                           uncertain=bool(uncertain), flagged=bool(flagged), show_gone=bool(gone),
                           page=max(page, 1))
    db = queries.connect(box.workspace)
    try:
        rows, total = queries.mails(db, f, box.cfg.min_confidence)
        selected = queries.mail(db, key)
        folder_list = queries.folders(db)
    finally:
        if db:
            db.close()
    params = {k: v for k, v in {"q": f.q, "category": f.category, "folder": f.folder, "period": f.period,
                                "uncertain": "1" if f.uncertain else "", "flagged": "1" if f.flagged else "",
                                "gone": "1" if f.show_gone else ""}.items() if v}
    pages = max(1, -(-total // queries.PAGE_SIZE))
    return templates.TemplateResponse(request, "mails.html", {
        **_sidebar(request, boxes, box, "mails"), "box": box, "f": f, "rows": rows, "total": total,
        "selected": selected, "folders": folder_list, "pages": pages, "periods": queries.PERIODS,
        "query": urlencode(params), "label": lambda k: _label(box, k),
        "link_key": lambda k: quote(k, safe=""), "min_conf": box.cfg.min_confidence,
        "today": datetime.now().date().isoformat(),
        "expired_to": lambda cat: expired_target(box.cfg, cat)})


from . import admin, editor  # noqa: E402,F401  (register their pages on router)
