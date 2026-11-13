"""Catalogue des courtiers."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request as HttpRequest
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import service
from ..config import settings
from ..db import (
    Broker,
    kv_get,
)

from ..web import _get, back, flash, get_db, log, render

router = APIRouter()


@router.get("/brokers", response_class=HTMLResponse)
def brokers_list(request: HttpRequest, q: str = "", country: str = "", state: str = "", db: Session = Depends(get_db)):
    stmt = select(Broker)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Broker.name.ilike(like), Broker.email.ilike(like), Broker.web.ilike(like), Broker.slug.ilike(like)))
    if state == "on":
        stmt = stmt.where(Broker.enabled.is_(True))
    elif state == "off":
        stmt = stmt.where(Broker.enabled.is_(False))
    elif state == "webform":
        stmt = stmt.where(Broker.email.is_(None))
    elif state == "custom":
        stmt = stmt.where(Broker.source == "custom")
    rows = db.scalars(stmt.order_by(Broker.name)).all()
    if state == "account":
        rows = [b for b in rows if b.account_risk]
    if country:
        rows = [b for b in rows if country.lower() in (b.countries or [])]
    all_countries = sorted({c for b in db.scalars(select(Broker)) for c in (b.countries or [])})
    return render(
        request, "brokers.html", db, brokers=rows, q=q, country=country, state=state, countries=all_countries,
        enabled_count=db.scalar(select(func.count(Broker.id)).where(Broker.enabled.is_(True))),
        total=db.scalar(select(func.count(Broker.id))), synced_at=kv_get(db, "brokers_synced_at"),
    )


@router.post("/brokers/{bid}/toggle")
def broker_toggle(request: HttpRequest, bid: int, db: Session = Depends(get_db)):
    b = _get(db, Broker, bid)
    b.enabled = not b.enabled
    b.enabled_overridden = True
    db.commit()
    return back(request.headers.get("referer") or "/brokers")


@router.post("/brokers/bulk")
def broker_bulk(request: HttpRequest, country: str = Form(...), action: str = Form(...), db: Session = Depends(get_db)):
    n = 0
    for b in db.scalars(select(Broker)):
        if country.lower() in (b.countries or []):
            b.enabled = action == "enable"
            b.enabled_overridden = True
            n += 1
    db.commit()
    flash(request, f"{n} courtier(s) {'activé' if action == 'enable' else 'désactivé'}(s) pour le pays « {country} ».")
    return back(f"/brokers?country={country}")


@router.post("/brokers/reset-defaults")
def broker_reset(request: HttpRequest, db: Session = Depends(get_db)):
    for b in db.scalars(select(Broker).where(Broker.source != "custom")):
        b.enabled_overridden = False
    n = service.apply_country_defaults(db)
    flash(request, f"Sélection par défaut (pays : {', '.join(settings.broker_countries)}) rétablie — {n} changement(s).")
    return back("/brokers")


@router.get("/brokers/new", response_class=HTMLResponse)
def broker_new(request: HttpRequest, db: Session = Depends(get_db)):
    return render(request, "broker_form.html", db)


@router.post("/brokers/new")
def broker_create(
    request: HttpRequest, db: Session = Depends(get_db), name: str = Form(...), email: str = Form(""),
    webform: str = Form(""), web: str = Form(""), postal_address: str = Form(""), countries: str = Form("fr"), notes: str = Form(""),
):
    if not email.strip() and not webform.strip():
        flash(request, "Indiquez au moins une adresse e-mail ou un formulaire en ligne.", "error")
        return back("/brokers/new")
    slug = "custom-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "courtier")
    base, i = slug, 2
    while db.scalar(select(Broker.id).where(Broker.slug == slug)):
        slug, i = f"{base}-{i}", i + 1
    b = Broker(
        slug=slug, name=name.strip(), email=email.strip() or None, webform=webform.strip() or None, web=web.strip() or None,
        postal_address=postal_address.strip() or None, countries=[c.strip().lower() for c in countries.split(",") if c.strip()] or ["all"],
        categories=["custom"], source="custom", enabled=True, enabled_overridden=True, notes=notes.strip() or None,
    )
    db.add(b)
    db.commit()
    flash(request, f"Courtier « {b.name} » ajouté." + (" Il sera ajouté automatiquement aux campagnes en cours cette nuit." if settings.auto_enroll_new_brokers else ""))
    return back("/brokers?state=custom")


@router.post("/brokers/{bid}/delete")
def broker_delete(request: HttpRequest, bid: int, db: Session = Depends(get_db)):
    b = _get(db, Broker, bid)
    if b.source != "custom":
        flash(request, "Seuls les courtiers ajoutés à la main peuvent être supprimés (désactivez les autres).", "error")
    else:
        # Ne jamais supprimer physiquement un courtier : la relation ORM utilise
        # delete-orphan et effacerait aussi les demandes, réponses et preuves associées.
        b.enabled = False
        b.enabled_overridden = True
        db.commit()
        flash(request, "Courtier archivé : il ne sera plus inclus dans les campagnes, et son historique est conservé.")
    return back("/brokers")


@router.post("/brokers/update")
def brokers_update(request: HttpRequest):
    try:
        res = service.update_brokers_from_remote()
        flash(request, f"Catalogue mis à jour : {res['added']} nouveau(x), {res['updated']} actualisé(s).")
    except Exception as e:  # noqa: BLE001
        log.exception("Mise à jour du catalogue")
        flash(request, f"Mise à jour impossible : {e}", "error")
    return back("/brokers")
