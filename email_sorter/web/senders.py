"""The Subscriptions page: who sends the most mail with an unsubscribe link, and unsubscribing from them."""
from __future__ import annotations

import logging
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..i18n import _
from ..store import Store
from ..unsubscribe import UnsubscribeError, one_click, one_click_link
from . import DANGER_CATEGORIES, _box, _label, _sidebar, queries, require_login, router
from .editor import _flash, _form, _page

log = logging.getLogger(__name__)


def _links(sender: dict) -> dict:
    """What the page offers for a sender: the one-click link, a web link and a mailto link."""
    links = sender["links"]
    return {"one_click": one_click_link(links) if sender["one_click"] else None,
            "one_click_host": urlsplit(one_click_link(links) or "").hostname,
            "web": next((x for x in links if x.lower().startswith(("https://", "http://"))), None),
            "mailto": next((x for x in links if x.lower().startswith("mailto:")), None)}


# the status filter: what is still to do (the default: not unsubscribed, or mail still comes after you did),
# mail still coming after unsubscribing, unsubscribed, all
STATUSES = ("", "coming", "done", "all")


def _status(sender: dict) -> str:
    """open (not unsubscribed), coming (unsubscribed, but mail came since) or done."""
    if not sender["unsubscribed"]:
        return "open"
    return "coming" if sender["since"] else "done"


def _shown(status: str, wanted: str) -> bool:
    return {"": status != "done", "coming": status == "coming", "done": status != "open"}.get(wanted, True)


def _back(box_id: str, category: str, status: str) -> str:
    query = {k: v for k, v in (("category", category), ("status", status)) if v}
    return f"/ui/m/{box_id}/senders" + (f"?{urlencode(query)}" if query else "")


@router.get("/ui/m/{box_id}/senders", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def senders(request: Request, box_id: str, category: str = "", status: str = ""):
    boxes, box = _box(request, box_id)
    category = category if category in box.cfg.categories else ""
    status = status if status in STATUSES else ""
    db = queries.connect(box.workspace)
    try:
        everyone = queries.senders(db, category, danger=DANGER_CATEGORIES)
    finally:
        if db:
            db.close()
    for row in everyone:
        row.update(_links(row), status=_status(row))
    counts = {s: sum(_shown(r["status"], s) for r in everyone) for s in STATUSES}
    # mail still coming after you unsubscribed: on top, it needs a look
    rows = sorted((r for r in everyone if _shown(r["status"], status)), key=lambda r: r["status"] != "coming")
    return _page(request, "senders.html", {
        **_sidebar(request, boxes, box, "senders"), "box": box, "rows": rows, "any": bool(everyone),
        "category": category, "status": status, "counts": counts, "days": queries.SENDER_DAYS,
        "label": lambda k: _label(box, k)})


@router.post("/ui/m/{box_id}/senders/action", dependencies=[Depends(require_login)])
async def sender_action(request: Request, box_id: str):
    form = await _form(request)
    _all_boxes, box = _box(request, box_id)
    address, action = str(form.get("address") or "").strip().lower(), str(form.get("action") or "")
    category, status = str(form.get("category") or ""), str(form.get("status") or "")
    back = _back(box.id, category if category in box.cfg.categories else "", status if status in STATUSES else "")
    store = Store(box.workspace / "data" / "state.db")
    try:
        sender = store.sender(address) if address else None
        if sender is None:
            _flash(request, _("Unknown sender."), "err")
        elif action == "unsubscribe":
            url = one_click_link(sender["unsubscribe"]) if sender["one_click"] else None
            if address in queries.suspicious_senders(store.db, DANGER_CATEGORIES):  # the page offers no button
                _flash(request, _("%(sender)s sent suspicious mail – Sortroom does not unsubscribe from it.",
                                  sender=address), "err")
            elif not url:
                _flash(request, _("%(sender)s offers no one-click unsubscribe.", sender=address), "err")
            else:
                try:
                    await run_in_threadpool(one_click, url)
                except UnsubscribeError as e:
                    _flash(request, _("Unsubscribing from %(sender)s failed: %(error)s", sender=address, error=e),
                           "err")
                else:
                    store.set_unsubscribed(address, "one-click")
                    _flash(request, _("Unsubscribed from %(sender)s. If mail still arrives, the list shows it.",
                                      sender=address))
        elif action == "mark":
            store.set_unsubscribed(address, "manual")
            _flash(request, _("%(sender)s marked as unsubscribed.", sender=address))
        elif action == "reset":
            store.set_unsubscribed(address, None)
            _flash(request, _("%(sender)s no longer marked as unsubscribed.", sender=address))
        else:
            _flash(request, _("Unknown action."), "err")
    finally:
        store.close()
    return RedirectResponse(back, status_code=303)
