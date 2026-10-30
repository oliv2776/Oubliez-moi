"""Cartographie prudente des domaines observés et des courtiers connus."""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session, selectinload

from . import __version__
from .config import settings
from .db import (
    FAILED, NEEDS_ACTION, OVERDUE, REPLIED, Broker, ExposureLink, Sender, SessionLocal, utcnow,
)
from .mailer import registered_domain
from .main_templates import templates

router = APIRouter()
DOMAIN_RE = re.compile(r"^(?=.{1,200}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
RELATION_LABELS = {
    "contact_domain": "Domaine de contact identique",
    "website_domain": "Domaine du site identique",
    "manual": "Association manuelle",
}
LINK_STATUSES = {"proposed": "À vérifier", "confirmed": "Confirmé", "rejected": "Écarté"}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def normalize_domain(value: str) -> str:
    value = (value or "").strip().lower().rstrip(".")
    if "://" in value or "/" in value or "@" in value:
        raise ValueError("Saisissez un nom de domaine, sans URL ni adresse e-mail.")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Nom de domaine invalide.") from exc
    if not DOMAIN_RE.fullmatch(value) or ".." in value:
        raise ValueError("Nom de domaine invalide.")
    return value


def broker_domain_sources(broker: Broker) -> dict[str, str]:
    """Domaine enregistrable -> type de preuve ; aucune recherche DNS ou déduction floue."""
    found = {}
    if broker.email and "@" in broker.email:
        domain = registered_domain(broker.email.rsplit("@", 1)[1])
        if domain:
            found[domain] = "contact_domain"
    for field in (broker.web, broker.webform):
        if field:
            host = urlsplit(field if "://" in field else "//" + field).hostname or ""
            domain = registered_domain(host)
            if domain and domain not in found:
                found[domain] = "website_domain"
    return found


def discover_candidates(db: Session) -> int:
    """Ajoute des propositions sur correspondance exacte de domaine enregistrable."""
    senders = db.scalars(select(Sender.domain).distinct()).all()
    brokers = db.scalars(select(Broker).where(Broker.enabled.is_(True))).all()
    existing = {
        (link.sender_domain, link.broker_id)
        for link in db.scalars(select(ExposureLink)).all()
    }
    added = 0
    for sender_domain in senders:
        domain = registered_domain(sender_domain)
        if not domain:
            continue
        for broker in brokers:
            source = broker_domain_sources(broker).get(domain)
            if not source or (domain, broker.id) in existing:
                continue
            db.add(ExposureLink(
                sender_domain=domain, broker_id=broker.id, group_name=broker.name,
                relation=source, confidence=0.95 if source == "contact_domain" else 0.90,
                status="proposed",
                evidence=("Domaine enregistrable identique à celui de l’adresse de contact du catalogue."
                          if source == "contact_domain" else
                          "Domaine enregistrable identique au site ou formulaire du catalogue."),
                source="automatic",
            ))
            existing.add((domain, broker.id))
            added += 1
    if added:
        db.flush()
    return added


def _render(request: Request, name: str, db: Session, **context):
    todo = db.scalar(select(func.count()).select_from(__import__("app.db", fromlist=["Request"]).Request).where(
        __import__("app.db", fromlist=["Request"]).Request.status.in_([NEEDS_ACTION, REPLIED, OVERDUE, FAILED])
    )) or 0
    return templates.TemplateResponse(request, name, {
        **context, "request": request, "settings": settings, "version": __version__,
        "nav_todo": todo, "flashes": request.session.pop("flash", []),
        "LINK_STATUSES": LINK_STATUSES, "RELATION_LABELS": RELATION_LABELS,
    })


@router.get("/exposures", response_class=HTMLResponse)
def exposures_page(request: Request, q: str = "", status: str = "", db: Session = Depends(get_db)):
    suggested = discover_candidates(db)
    if suggested:
        db.commit()
    senders = db.scalars(select(Sender).options(selectinload(Sender.mailbox))).all()
    links = db.scalars(select(ExposureLink).options(selectinload(ExposureLink.broker))
                       .order_by(ExposureLink.sender_domain, ExposureLink.id)).all()
    grouped_senders: dict[str, list[Sender]] = {}
    for sender in senders:
        grouped_senders.setdefault(registered_domain(sender.domain) or sender.domain, []).append(sender)
    grouped_links: dict[str, list[ExposureLink]] = {}
    for link in links:
        grouped_links.setdefault(link.sender_domain, []).append(link)
    rows = []
    for domain, items in grouped_senders.items():
        matches = grouped_links.get(domain, [])
        if status == "review" and not any(link.status == "proposed" for link in matches):
            continue
        if status in LINK_STATUSES and not any(link.status == status for link in matches):
            continue
        if q and q.lower() not in domain.lower() and not any(
            q.lower() in s.address.lower() or q.lower() in s.name.lower() for s in items
        ) and not any(q.lower() in link.broker.name.lower() for link in matches):
            continue
        rows.append({
            "domain": domain, "senders": items, "matches": matches,
            "mail_count": sum(s.count or 0 for s in items),
            "reappeared": sum(s.seen_after_unsub or 0 for s in items),
            "last_seen": max((s.last_seen for s in items if s.last_seen), default=None),
            "new_count": sum(1 for s in items if s.status == "new"),
        })
    rows.sort(key=lambda row: (row["reappeared"] > 0, row["last_seen"] or datetime.min), reverse=True)
    brokers = db.scalars(select(Broker).order_by(Broker.name)).all()
    stats = {
        "domains": len(grouped_senders),
        "unmatched": sum(1 for domain in grouped_senders if not grouped_links.get(domain)),
        "proposed": sum(1 for link in links if link.status == "proposed"),
        "reappeared": sum(1 for sender in senders if sender.seen_after_unsub),
    }
    return _render(request, "exposures.html", db, rows=rows, brokers=brokers, q=q, status=status,
                   stats=stats, suggestions_added=suggested)


@router.post("/exposures/manual")
def exposure_manual(request: Request, sender_domain: str = Form(...), broker_id: int = Form(...),
                    group_name: str = Form(""), note: str = Form(""), db: Session = Depends(get_db)):
    try:
        domain = normalize_domain(sender_domain)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    broker = db.get(Broker, broker_id)
    if broker is None:
        raise HTTPException(404)
    link = db.scalar(select(ExposureLink).where(
        ExposureLink.sender_domain == domain, ExposureLink.broker_id == broker_id
    ))
    if link is None:
        link = ExposureLink(sender_domain=domain, broker_id=broker_id)
        db.add(link)
    link.group_name = group_name.strip()[:300] or broker.name
    link.relation, link.confidence, link.status, link.source = "manual", 1.0, "confirmed", "manual"
    link.evidence = note.strip()[:2000] or "Association effectuée manuellement par l’utilisateur."
    link.reviewed_at = utcnow()
    link.review_note = note.strip()[:2000] or None
    db.commit()
    request.session.setdefault("flash", []).append(["ok", f"Association de {domain} enregistrée comme confirmée."])
    return RedirectResponse("/exposures", status_code=303)


@router.post("/exposures/{link_id}/review")
def exposure_review(request: Request, link_id: int, decision: str = Form(...),
                    group_name: str = Form(""), note: str = Form(""), db: Session = Depends(get_db)):
    link = db.get(ExposureLink, link_id)
    if link is None:
        raise HTTPException(404)
    if decision not in {"confirmed", "rejected"}:
        raise HTTPException(400, "Décision invalide.")
    link.status = decision
    if group_name.strip():
        link.group_name = group_name.strip()[:300]
    link.reviewed_at = utcnow()
    link.review_note = note.strip()[:2000] or None
    db.commit()
    label = "confirmée" if decision == "confirmed" else "écartée"
    request.session.setdefault("flash", []).append(["ok", f"Correspondance {label}."])
    return RedirectResponse("/exposures", status_code=303)


@router.post("/exposures/{link_id}/group")
def exposure_group(request: Request, link_id: int, group_name: str = Form(...),
                   db: Session = Depends(get_db)):
    link = db.get(ExposureLink, link_id)
    if link is None:
        raise HTTPException(404)
    group_name = group_name.strip()[:300]
    if not group_name:
        raise HTTPException(400, "Le nom du groupe est obligatoire.")
    link.group_name = group_name
    link.review_note = "Groupe / marque modifié manuellement."
    link.reviewed_at = utcnow()
    db.commit()
    request.session.setdefault("flash", []).append(["ok", "Groupe commercial mis à jour."])
    return RedirectResponse("/exposures", status_code=303)
