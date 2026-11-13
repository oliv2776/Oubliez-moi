"""Demandes aux courtiers : liste, fiche, statut, relance, dossier imprimable, export CSV."""
from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request as HttpRequest
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from .. import letters, mailview, service
from .. import history
from ..db import (
    FAILED,
    NEEDS_ACTION,
    OVERDUE,
    QUEUED,
    STATUS_LABELS,
    Broker,
    Event,
    Profile,
    Request,
    add_event,
)

from ..web import TZ, _get, _safe_next, back, flash, fmt_d, fmt_dt, get_db, render, templates

router = APIRouter()


@router.get("/requests", response_class=HTMLResponse)
def requests_list(request: HttpRequest, status: str = "", profile: int | None = None, q: str = "", db: Session = Depends(get_db)):
    stmt = select(Request).join(Broker).options(selectinload(Request.broker), selectinload(Request.profile))
    if status:
        stmt = stmt.where(Request.status.in_(status.split(",")))
    if profile:
        stmt = stmt.where(Request.profile_id == profile)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Broker.name.ilike(like), Broker.email.ilike(like), Request.reference.ilike(like)))
    reqs = db.scalars(stmt.order_by(Request.id.desc()).limit(1000)).all()
    profiles = db.scalars(select(Profile)).all()
    return render(request, "requests.html", db, reqs=reqs, status=status, profile=profile, q=q, profiles=profiles)


@router.get("/requests/{rid}", response_class=HTMLResponse)
def request_detail(request: HttpRequest, rid: int, db: Session = Depends(get_db)):
    r = _get(db, Request, rid)
    complaint = letters.cnil_complaint_text(r) if r.status in (OVERDUE, "refused") else None
    return render(request, "request_detail.html", db, r=r, complaint=complaint, form_help=_form_help(r))


def _form_help(r: Request) -> dict | None:
    if not r.broker.webform or r.status in ("completed", "cancelled", "cnil"):
        return None
    return {
        "fields": letters.form_fields(r),
        "msg_fr": letters.form_message(r, "fr"),
        "msg_en": letters.form_message(r, "en"),
        "open": r.status == NEEDS_ACTION or (r.channel == "webform" and not r.sent_at),
    }


@router.get("/requests/{rid}/dossier", response_class=HTMLResponse)
def request_dossier(request: HttpRequest, rid: int, db: Session = Depends(get_db)):
    """Dossier de réclamation imprimable (Enregistrer en PDF depuis le navigateur)."""
    r = _get(db, Request, rid)
    letters_sent = [e for e in r.events if e.kind in ("sent", "reminder", "supplement", "auto_reply", "formal_notice")]
    replies = [e for e in r.events if e.kind == "reply"]
    return templates.TemplateResponse(request, "dossier.html", {
        "request": request, "r": r, "letters_sent": letters_sent, "replies": replies,
        "complaint": letters.cnil_complaint_text(r), "now": datetime.now(TZ), "split": mailview.split_headers,
        "STATUS": STATUS_LABELS,
    })


@router.get("/events/{eid}/view", response_class=HTMLResponse)
def event_view(eid: int, db: Session = Depends(get_db)):
    """Contenu d'un message, servi isolé (cadre sandboxé + CSP sans aucun chargement externe)."""
    ev = _get(db, Event, eid)
    return HTMLResponse(
        mailview.render_event(ev.body, ev.html),
        # SAMEORIGIN : cette page est faite pour être affichée dans le cadre de l'application (DENY partout ailleurs).
        headers={"Content-Security-Policy": mailview.CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                 "X-Frame-Options": "SAMEORIGIN"},
    )


@router.post("/requests/{rid}/status")
def request_status(request: HttpRequest, rid: int, status: str = Form(...), note: str = Form(""), next: str = Form(""),
                   db: Session = Depends(get_db)):
    r = _get(db, Request, rid)
    if status not in STATUS_LABELS:
        raise HTTPException(400)
    service.set_status(db, r, status, f"Statut changé manuellement : {STATUS_LABELS[status]}" + (f" — {note}" if note.strip() else ""),
                       actor=history.MANUAL, category=status)
    db.commit()
    flash(request, f"{r.broker.name} : {STATUS_LABELS[status]}.")
    return back(_safe_next(next) or request.headers.get("referer") or f"/requests/{rid}")


@router.post("/requests/{rid}/note")
def request_note(request: HttpRequest, rid: int, note: str = Form(...), db: Session = Depends(get_db)):
    r = _get(db, Request, rid)
    if note.strip():
        add_event(db, r, "note", note.strip()[:500], note.strip() if len(note) > 500 else None)
        db.commit()
    return back(f"/requests/{rid}")


@router.post("/requests/{rid}/send-reminder")
def request_reminder(request: HttpRequest, rid: int, next: str = Form(""), db: Session = Depends(get_db)):
    r = _get(db, Request, rid)
    if not r.broker.email or not r.sent_at:
        flash(request, "Impossible de relancer : la demande n'a pas été envoyée par e-mail.", "error")
    elif service.send_reminder(db, r):
        if r.status != "sent":
            service.set_status(db, r, "sent", "Relance manuelle envoyée")
            db.commit()
        flash(request, "Relance envoyée.")
    else:
        flash(request, f"Échec de la relance : {r.last_error}", "error")
    return back(_safe_next(next) or f"/requests/{rid}")


@router.post("/requests/requeue-failed")
def requeue_failed(request: HttpRequest, db: Session = Depends(get_db)):
    n = 0
    for r in db.scalars(select(Request).join(Broker).where(Request.status == FAILED, Broker.email.is_not(None))):
        service.set_status(db, r, QUEUED, "Remise en file d'attente")
        n += 1
    db.commit()
    flash(request, f"{n} demande(s) remise(s) en file d'attente.")
    return back("/requests?status=failed")


def _csv_safe(value) -> str:
    """Neutralise les cellules susceptibles d'être interprétées comme une formule par un tableur."""
    text = "" if value is None else str(value)
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


@router.get("/export.csv")
def export_csv(db: Session = Depends(get_db)):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["reference", "profil", "courtier", "contact", "cycle", "statut", "envoyee_le", "echeance", "relances", "derniere_reponse", "close_le"])
    for r in db.scalars(select(Request).options(selectinload(Request.broker), selectinload(Request.profile)).order_by(Request.id)):
        w.writerow([_csv_safe(value) for value in (
            r.reference, r.profile.full_name, r.broker.name, r.broker.email or r.broker.webform, r.cycle, r.status_label,
            fmt_dt(r.sent_at), fmt_d(r.due_at), r.reminder_count, fmt_dt(r.last_reply_at), fmt_dt(r.closed_at),
        )])
    data = "﻿" + buf.getvalue()  # BOM pour Excel
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="oubliez-moi-{datetime.now():%Y%m%d}.csv"'})
