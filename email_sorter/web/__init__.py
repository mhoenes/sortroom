"""Web UI: server-rendered pages (Jinja2), session login with ADMIN_PASSWORD.

Mounted by email_sorter.api. Pages read each mailbox's state.db read-only; nothing here talks to
IMAP or the classifier, so pages load fast and never change mail.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__, i18n
from ..config import INBOX_ACTION, SECRETS_FILE, ConfigError, Mailbox, default_label, read_secrets
from ..i18n import _
from ..sorter import expired_target
from . import queries

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.add_extension("jinja2.ext.i18n")
# added by the i18n extension at runtime, so a type checker doesn't know it
templates.env.install_gettext_callables(  # type: ignore[attr-defined]
    i18n.translate, i18n.translate_plural, newstyle=True)
templates.env.globals["lang"] = i18n.language
router = APIRouter()

SESSION_DAYS = 7
# failed logins: after MAX_FAILED_PER_ADDRESS from one address, or MAX_FAILED_TOTAL from all of them,
# within FAILED_WINDOW seconds, further attempts are refused at once until the oldest one is old enough
FAILED_WINDOW = 15 * 60
MAX_FAILED_PER_ADDRESS = 5
MAX_FAILED_TOTAL = 50
_failed_logins: dict[str, list[float]] = {}  # address -> times of its recent failed logins
_failed_lock = threading.Lock()
_clock = time.monotonic


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
    """Every request renders in the language and the theme set under Global settings ([ui] language, theme)."""
    token = i18n.set_language(i18n.configured_language(request.app.state.config_path))
    request.state.theme = i18n.configured_theme(request.app.state.config_path)
    try:
        return await call_next(request)
    finally:
        i18n.reset_language(token)


class LoginRequired(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def sessions_ended(request: Request) -> float:
    """When "Log out everywhere" was used last (seconds since 1970), 0 if never: logins from before are void.
    Kept in the secrets.toml next to config.toml ([ui] sessions_ended)."""
    try:
        stored = read_secrets(request.app.state.config_path.with_name(SECRETS_FILE)).get("ui") or {}
        return float(stored.get("sessions_ended") or 0)
    except (ConfigError, TypeError, ValueError):
        return 0


def require_login(request: Request) -> None:
    here = str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")
    if not request.session.get("user"):
        raise LoginRequired(here)
    if request.session.get("since", 0) < sessions_ended(request):  # the cookie is signed, but ended
        request.session.clear()
        raise LoginRequired(here)


def _locked_for(ip: str) -> float:
    """Seconds until a login from this address may be tried again; 0 when it may be tried now.

    Refused attempts are answered at once instead of waiting: a waiting request would hold one of the
    server's worker threads, and a few dozen at a time would stall the whole UI."""
    now = _clock()
    with _failed_lock:
        for address in list(_failed_logins):
            recent = [t for t in _failed_logins[address] if now - t < FAILED_WINDOW]
            if recent:
                _failed_logins[address] = recent
            else:
                del _failed_logins[address]
        mine = _failed_logins.get(ip, [])
        everyone = sorted(t for times in _failed_logins.values() for t in times)
    waits = [0.0]
    if len(mine) >= MAX_FAILED_PER_ADDRESS:
        waits.append(mine[-MAX_FAILED_PER_ADDRESS] + FAILED_WINDOW - now)
    if len(everyone) >= MAX_FAILED_TOTAL:  # many addresses guessing together
        waits.append(everyone[-MAX_FAILED_TOTAL] + FAILED_WINDOW - now)
    return max(waits)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/ui"):
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(next), "error": None, "configured": len(admin_password()) >= 8})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, password: str = Form(""), next: str = Form("/ui")):
    ip = request.client.host if request.client else "?"
    configured = len(admin_password()) >= 8
    wait = _locked_for(ip)
    if wait > 0:  # not even the right password gets in now, or guessing would just go on
        minutes = max(1, math.ceil(wait / 60))
        return templates.TemplateResponse(request, "login.html", {
            "next": _safe_next(next), "configured": configured,
            "error": i18n.ngettext("Too many failed logins. Please try again in %(num)s minute.",
                                   "Too many failed logins. Please try again in %(num)s minutes.", minutes)},
            status_code=429)
    if configured and hmac.compare_digest(password.encode(), admin_password().encode()):
        with _failed_lock:
            _failed_logins.pop(ip, None)
        request.session.clear()
        request.session["user"] = "admin"
        request.session["since"] = time.time()  # with fractions: "Log out everywhere" may come in the same second
        return RedirectResponse(_safe_next(next), status_code=303)
    with _failed_lock:
        _failed_logins.setdefault(ip, []).append(_clock())
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(next), "configured": configured,
        "error": _("Wrong password.") if configured else None}, status_code=401)


@router.post("/logout")
async def logout(request: Request):
    """Log out; with the form's CSRF token, so another site can't log you out."""
    sent = str((await request.form()).get("csrf", ""))
    if not sent or not secrets.compare_digest(sent, request.session.get("csrf", "")):
        raise HTTPException(403, _("Form expired – please reload the page."))
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _safe_next(url: str) -> str:
    r"""Only paths on this site, so the login form can't be used to redirect elsewhere. Browsers read a
    backslash as a slash and drop tabs and line breaks, so "/\evil.example" or "/<tab>/evil.example"
    would lead to another site just like "//evil.example"."""
    if (not url.startswith("/") or url.startswith("//") or "\\" in url
            or any(ord(c) < 0x21 or ord(c) == 0x7F for c in url)):
        return "/ui"
    return url


# ---------------------------------------------------------------- formatting

for _name, _fn in (("num", i18n.num), ("conf", i18n.conf), ("dt", i18n.dt), ("date", i18n.date),
                   ("usd", i18n.usd), ("ago", i18n.ago)):
    templates.env.filters[_name] = _fn
templates.env.globals["version"] = __version__
templates.env.globals["percent"] = i18n.percent


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
            "recheck": _("Expiry check"), "undo": _("Undo"), "cleanup": _("Deletion rules")}.get(kind, kind)


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


# words of the errors that come from the IMAP server or the way to it (login, OAuth, TLS, network)
_IMAP_WORDS = ("login", "authenticat", "credential", "password", "oauth", "token", "imap", "ssl", "certificate",
               "socket", "timed out", "timeout", "connection", "getaddrinfo", "errno", "eof", "network", "unreachable")


def run_problem(run: dict | None) -> str:
    """What a failed run points to: 'model' (the AI service: key, credit, unreachable), 'imap' (the mailbox's
    login or connection), 'mails' (single mails failed, retried by the next run) or '' (an error of its own)."""
    error = ((run or {}).get("error") or "").lower()
    if not error:
        return "mails"
    if error.startswith("http ") or "classification endpoint" in error:
        return "model"
    return "imap" if any(w in error for w in _IMAP_WORDS) else ""


def run_error(text: str | None) -> str:
    """A run's error as the server sent it: without the b'…' around a message passed on as bytes."""
    text = (text or "").strip()
    return text[2:-1] if len(text) > 2 and text[:2] in ("b'", 'b"') and text[-1] == text[1] else text


templates.env.globals["run_problem"] = run_problem
templates.env.filters["run_error"] = run_error


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
    broken = getattr(boxes, "broken", {})
    if box_id in broken:
        raise HTTPException(500, _("The settings of mailbox %(id)s cannot be loaded: %(e)s",
                                   id=box_id, e=broken[box_id]))
    if box_id not in boxes:
        raise HTTPException(404, _("Unknown mailbox"))
    return boxes, boxes[box_id]


def _sidebar(request: Request, boxes: dict[str, Mailbox], current: Mailbox | None, active: str) -> dict:
    busy = request.app.state.is_busy
    # switching to another mailbox keeps the page: from A's mails to B's mails
    page = {"mails": "/mails", "subscriptions": "/subscriptions", "categories": "/categories", "rules": "/rules",
            "maintenance": "/maintenance", "settings": "/settings"}.get(active, "")
    items = []
    for b in boxes.values():
        db = queries.connect(b.workspace)
        try:
            st = queries.stats(db, b.cfg.min_confidence)
        finally:
            if db:
                db.close()
        last = st.last_run
        if busy(b):
            tone, status = "busy", _("Running")
        elif last is None:
            tone, status = "none", _("No run yet")
        elif last.get("exit_code"):
            tone, status = "err", _("Last run with errors · %(when)s", when=i18n.ago(last["started"]))
        else:
            tone, status = "ok", _("Last run OK · %(when)s", when=i18n.ago(last["started"]))
        items.append({"id": b.id, "name": b.name, "host": b.cfg.imap_host, "tone": tone, "status": status,
                      "uncertain": st.uncertain, "href": f"/ui/m/{b.id}{page}"})
    current_item = next((i for i in items if current and i["id"] == current.id), None)
    return {"sidebar_boxes": items, "current": current_item, "active": active,
            "broken_mailboxes": getattr(boxes, "broken", {})}


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
                      "schedule": request.app.state.scheduler.status(b),
                      "categories": len(b.cfg.categories)})
        totals["today"] += st.sorted_today
        totals["uncertain"] += st.uncertain
        totals["cost"] += st.cost_month
    schedules = [c["schedule"] for c in cards]
    status = {  # the page's subtitle: what is going on across all mailboxes
        "running": [c["box"].name for c in cards if c["busy"] or c["schedule"]["running"]],
        "errors": sum(1 for c in cards if c["stats"].errors_today),
        "next": min((s["next"] for s in schedules if s["enabled"] and s["active"] and s["next"]), default=None),
        "scheduled": any(s["enabled"] and s["active"] for s in schedules),
        "process_off": bool(schedules) and not any(s["active"] for s in schedules),
    }
    return templates.TemplateResponse(request, "mailboxes.html", {
        **_sidebar(request, boxes, None, "all"), "cards": cards, "totals": totals, "status": status})


@router.get("/ui/m/{box_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def overview(request: Request, box_id: str):
    from .suggestions import category_hints, rule_suggestions  # imports this module

    boxes, box = _box(request, box_id)
    cfg = box.cfg
    db = queries.connect(box.workspace)
    try:
        st = queries.stats(db, cfg.min_confidence)
        dist = queries.distribution(db, 7)
        runs = queries.recent_runs(db, 8)
        notable = queries.notable_runs(db)
        today = queries.runs_today(db)
        failure = queries.last_failure(db)
        review = queries.uncertain_mails(db, cfg.min_confidence, 5, days=30)  # as stats().uncertain
        filed, corrected = map(sum, zip(*queries.corrections(db, cfg.min_confidence).values() or [(0, 0)], strict=True))
        expired = queries.expired_moved(db, 7)
        rule_tips = rule_suggestions(db, cfg)
        hints = category_hints(db, cfg, explained=rule_tips)
    finally:
        if db:
            db.close()
    total_7d = sum(n for _c, n in dist)
    top = max((n for _c, n in dist), default=0)
    bars = [{"label": _label(box, c), "n": n, "pct": round(100 * n / top, 1) if top else 0,
             "tone": "inbox" if c == "" else ("danger" if c in DANGER_CATEGORIES else "")} for c, n in dist]
    return templates.TemplateResponse(request, "overview.html", {
        **_sidebar(request, boxes, box, "overview"), "box": box, "stats": st, "bars": bars,
        "total_7d": total_7d, "runs": runs, "notable": notable, "today": today, "failure": failure,
        "review": review, "expired_7d": expired,
        "filed_30d": filed, "corrected_30d": corrected, "rule_tips": rule_tips, "hints": hints,
        "schedule": request.app.state.scheduler.status(box), "label": lambda k: _label(box, k)})


@router.get("/ui/m/{box_id}/mails", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def mails(request: Request, box_id: str, q: str = "", category: str = "", folder: str = "", period: str = "7d",
          uncertain: str = "", flagged: str = "", gone: str = "", page: int = 1, key: str = "", rule: str = ""):
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
    # the mails before and after the open one in the list: the arrows in its details, and where accepting it
    # goes on to (the next one, else the one before)
    keys = [r["message_key"] for r in rows]
    prev_key = next_key = None
    if selected and selected["message_key"] in keys:
        i = keys.index(selected["message_key"])
        prev_key = keys[i - 1] if i else None
        next_key = keys[i + 1] if i + 1 < len(keys) else None
    # with a mail open, what an action did shows in its details instead of at the top of the page
    notice = request.session.pop("flash", None) if selected else None
    return templates.TemplateResponse(request, "mails.html", {
        **_sidebar(request, boxes, box, "mails"), "box": box, "f": f, "rows": rows, "total": total,
        "selected": selected, "folders": folder_list, "pages": pages, "periods": queries.PERIODS,
        "query": urlencode(params), "label": lambda k: _label(box, k),
        "link_key": lambda k: quote(k, safe=""), "min_conf": box.cfg.min_confidence,
        "today": datetime.now().date().isoformat(), "offer_rule": rule if rule in box.cfg.categories
        or rule == INBOX_ACTION else "",
        "expired_to": lambda cat: expired_target(box.cfg, cat), "prev_key": prev_key, "next_key": next_key,
        "notice": notice})


from . import admin, editor, subscriptions, suggestions  # noqa: E402,F401  (register their pages on router)
