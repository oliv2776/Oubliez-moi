"""Courtiers qui ne connaissent que des identifiants publicitaires (cookie, téléphone).

Ils ne peuvent pas relier l'e-mail de la demande à leur profil et réclament l'identifiant. Plutôt
que de le leur envoyer (ce qui leur donnerait justement ce lien), on rend ces identifiants inutiles
une fois pour toutes (téléphone, navigateur), on fait leur opposition si on le souhaite, puis on
clôture toutes ces demandes d'un coup.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request as HttpRequest
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import confirm, insight, mailer, mailview
from .db import COMPLETED, NEEDS_ACTION, REPLIED, STATUS_LABELS, Request
from .web import get_db, render

router = APIRouter()
NOTE = "identifiants publicitaires réinitialisés (téléphone, navigateur), opposition faite si proposée"
# Liens utiles dans leur message : opposition, préférences, centre de confidentialité…
_USEFUL = re.compile(r"opt.?out|privacy|choices|cookie|preference|interest|rights|consent|datenschutz|widerspruch|"
                     r"confidentialit|opposition|do.?not.?sell|youronlinechoices|networkadvertising|aboutads", re.I)
_USELESS = re.compile(r"linkedin\.|twitter\.|facebook\.|instagram\.|youtube\.|support\.google\.|support\.mozilla\.|"
                      r"support\.apple\.|microsoft\.com|\.(png|jpe?g|gif|svg)(\?|$)|unsubscribe|\[jeton\]", re.I)


def _last_reply(req: Request):
    replies = [e for e in req.events if e.kind == "reply"]
    return replies[-1] if replies else None


def asks_for_device_id(req: Request) -> bool:
    """Classée « identifiant publicitaire demandé » à la réception, à la réanalyse, ou par les règles actuelles."""
    ins = insight.for_request(req)
    if ins and ins.kind == "device_id":
        return True
    ev = _last_reply(req)
    if ev is None:
        return False
    headers, text = mailview.split_headers(ev.body or "")
    m = mailer.ParsedMail(uid="", from_addr="", from_name="", subject=headers.get("Objet", ""), message_id="", text=text)
    return mailer.classify(m).kind == "device_id"


def candidates(db: Session) -> list[Request]:
    rows = db.scalars(select(Request).options(selectinload(Request.broker), selectinload(Request.events))
                      .where(Request.status.in_([NEEDS_ACTION, REPLIED])).order_by(Request.id)).all()
    return [r for r in rows if asks_for_device_id(r)]


def useful_links(req: Request, limit: int = 4) -> list[dict]:
    """Liens d'opposition ou de confidentialité cités dans la dernière réponse, avec leur domaine."""
    ev = _last_reply(req)
    if ev is None:
        return []
    text = mailview.split_headers(ev.body or "")[1]
    own = confirm.broker_domains(req.broker.email, req.broker.web)
    out, seen = [], set()
    for url, label in mailer.extract_links(ev.html or "", mailer.latest_reply_text(text)):
        host = (urlsplit(url).hostname or "").lower()
        key = (host, urlsplit(url).path.rstrip("/"))
        if not host or key in seen or _USELESS.search(url) or not (_USEFUL.search(url) or _USEFUL.search(label or "")):
            continue
        seen.add(key)
        path = urlsplit(url).path.rstrip("/")
        short = host + (path if len(path) <= 40 else path[:39] + "…")
        label = (label or "").strip()
        out.append({"url": url, "label": label[:80] if label and label.lower() not in url.lower() else "", "short": short, "host": host,
                    "own": mailer.registered_domain(host) in own})
        if len(out) >= limit:
            break
    return out


@router.get("/tasks/device-ids", response_class=HTMLResponse)
def device_ids_page(request: HttpRequest, db: Session = Depends(get_db)):
    items = [(r, useful_links(r)) for r in candidates(db)]
    return render(request, "device_ids.html", db, items=items)


@router.post("/tasks/device-ids/close")
def device_ids_close(request: HttpRequest, ids: list[int] = Form(default=[]), db: Session = Depends(get_db)):
    from .service import set_status
    allowed = {r.id: r for r in candidates(db)}
    done = 0
    for rid in ids:
        r = allowed.get(rid)
        if r is None:
            continue  # demande déjà traitée ou qui ne relève plus de ce cas
        set_status(db, r, COMPLETED, f"Statut changé manuellement : {STATUS_LABELS[COMPLETED]} — {NOTE}")
        done += 1
    db.commit()
    request.session.setdefault("flash", []).append(
        ["ok", f"{done} demande(s) clôturée(s)." if done else "Aucune demande sélectionnée."])
    return RedirectResponse("/tasks/device-ids" if candidates(db) else "/tasks", status_code=303)
