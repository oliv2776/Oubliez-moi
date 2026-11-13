"""Profils : identités à faire effacer, campagnes et aperçu des courriers."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request as HttpRequest
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import letters, phones, service
from ..config import settings
from ..db import (
    QUEUED,
    Broker,
    Profile,
    Request,
)

from ..web import _get, _split, back, flash, get_db, render

router = APIRouter()


@router.get("/profiles", response_class=HTMLResponse)
def profiles_list(request: HttpRequest, db: Session = Depends(get_db)):
    profiles = db.scalars(select(Profile).order_by(Profile.id)).all()
    return render(request, "profiles.html", db, profiles=[(p, service.stats(db, p.id)) for p in profiles])


@router.get("/profiles/new", response_class=HTMLResponse)
def profile_new(request: HttpRequest, db: Session = Depends(get_db)):
    return render(request, "profile_form.html", db, profile=None)


def _fill_profile(p: Profile, full_name, other_names, emails, phone_list, addresses, birth_date, language, notes) -> list[str]:
    """Remplit le profil et renvoie les numéros de téléphone non reconnus."""
    p.full_name = full_name.strip()
    p.other_names = _split(other_names)
    p.emails = [e.lower() for e in _split(emails)]
    p.phones, invalid = phones.normalize_list(_split(phone_list))
    # Adresses postales : séparées par une ligne vide
    p.addresses = [" ".join(_split(block)) for block in re.split(r"\n\s*\n", (addresses or "").replace("\r", "")) if block.strip()]
    p.birth_date = birth_date.strip() or None
    p.language = language if language in ("auto", "fr", "en") else "auto"
    p.notes = notes.strip() or None
    return invalid


def _warn_phones(request: HttpRequest, invalid: list[str]) -> None:
    if invalid:
        flash(request, "Numéro(s) non reconnu(s), conservé(s) tel(s) quel(s) : " + ", ".join(invalid)
              + ". Pour un numéro étranger, commencez par l'indicatif (+32, +41…).", "info")


@router.post("/profiles")
def profile_create(
    request: HttpRequest, db: Session = Depends(get_db),
    full_name: str = Form(...), other_names: str = Form(""), emails: str = Form(""), phones: str = Form(""),
    addresses: str = Form(""), birth_date: str = Form(""), language: str = Form("auto"), notes: str = Form(""),
):
    p = Profile()
    invalid = _fill_profile(p, full_name, other_names, emails, phones, addresses, birth_date, language, notes)
    if not p.emails:
        flash(request, "Au moins une adresse e-mail est nécessaire pour que les courtiers vous identifient.", "error")
        return back("/profiles/new")
    db.add(p)
    db.commit()
    _warn_phones(request, invalid)
    flash(request, f"Profil « {p.full_name} » créé. Vérifiez l'aperçu du courrier, puis lancez les demandes.")
    return back(f"/profiles/{p.id}")


@router.get("/profiles/{pid}", response_class=HTMLResponse)
def profile_detail(request: HttpRequest, pid: int, db: Session = Depends(get_db)):
    p = _get(db, Profile, pid)
    reqs = db.scalars(
        select(Request).options(selectinload(Request.broker)).where(Request.profile_id == pid).order_by(Request.status, Request.id)
    ).all()
    sample_broker = db.scalars(select(Broker).where(Broker.enabled.is_(True), Broker.email.is_not(None)).order_by(Broker.name)).first()
    pending_new = db.scalar(
        select(func.count(Broker.id)).where(Broker.enabled.is_(True), Broker.id.not_in(select(Request.broker_id).where(Request.profile_id == pid)))
    )
    # Organismes à risque pas encore contactés (ni demande, ni demande encore en file)
    contacted = select(Request.broker_id).where(Request.profile_id == pid, Request.status != QUEUED)
    risky = [b for b in db.scalars(select(Broker).where(Broker.enabled.is_(True), Broker.id.not_in(contacted)).order_by(Broker.name))
             if b.account_risk]
    cands = service.supplement_candidates(db, p)
    new_values = sorted({v for _, missing in cands for _, v in missing})
    supp_queued = db.scalar(select(func.count(Request.id)).where(Request.profile_id == pid, Request.supplement_pending.is_(True)))
    return render(request, "profile_detail.html", db, p=p, reqs=reqs, st=service.stats(db, pid), sample_broker=sample_broker,
                  pending_new=pending_new, supp_count=len(cands), supp_values=new_values, supp_queued=supp_queued, risky=risky)


@router.post("/profiles/{pid}/supplement")
def profile_supplement(request: HttpRequest, pid: int, db: Session = Depends(get_db)):
    p = _get(db, Profile, pid)
    n = service.queue_supplements(db, p)
    flash(request, f"{n} complément(s) mis en file : ils partiront au rythme normal d'envoi." if n else "Aucun complément à envoyer.")
    return back(f"/profiles/{pid}")


@router.get("/profiles/{pid}/edit", response_class=HTMLResponse)
def profile_edit(request: HttpRequest, pid: int, db: Session = Depends(get_db)):
    return render(request, "profile_form.html", db, profile=_get(db, Profile, pid))


@router.post("/profiles/{pid}")
def profile_update(
    request: HttpRequest, pid: int, db: Session = Depends(get_db),
    full_name: str = Form(...), other_names: str = Form(""), emails: str = Form(""), phones: str = Form(""),
    addresses: str = Form(""), birth_date: str = Form(""), language: str = Form("auto"), notes: str = Form(""),
):
    p = _get(db, Profile, pid)
    invalid = _fill_profile(p, full_name, other_names, emails, phones, addresses, birth_date, language, notes)
    db.commit()
    _warn_phones(request, invalid)
    n = len(service.supplement_candidates(db, p))
    if n and settings.auto_supplements:
        n = service.queue_supplements(db, p)
        flash(request, f"Profil mis à jour. {n} courtier(s) déjà contacté(s) recevront automatiquement un complément "
                       "avec les nouvelles informations.")
    elif n:
        flash(request, f"Profil mis à jour. {n} courtier(s) déjà contacté(s) n'ont pas reçu les nouvelles informations : "
                       "vous pouvez leur envoyer un complément ci-dessous.", "info")
    else:
        flash(request, "Profil mis à jour. Les prochains courriers utiliseront ces informations.")
    return back(f"/profiles/{pid}")


@router.post("/profiles/{pid}/delete")
def profile_delete(request: HttpRequest, pid: int, confirm: str = Form(""), db: Session = Depends(get_db)):
    p = _get(db, Profile, pid)
    if confirm.strip() != p.full_name:
        flash(request, "Tapez le nom exact du profil pour confirmer la suppression.", "error")
        return back(f"/profiles/{pid}/edit")
    db.delete(p)
    db.commit()
    flash(request, "Profil et historique supprimés.")
    return back("/profiles")


@router.post("/profiles/{pid}/campaign")
def profile_campaign(request: HttpRequest, pid: int, db: Session = Depends(get_db)):
    p = _get(db, Profile, pid)
    n = service.start_campaign(db, p)
    if n:
        mode = " (mode simulation : rien ne part réellement)" if settings.dry_run else ""
        flash(request, f"{n} demandes créées. Envoi progressif à raison de {settings.send_per_hour} par heure{mode}.")
    else:
        flash(request, "Aucun nouveau courtier : toutes les demandes existent déjà.", "info")
    return back(f"/profiles/{pid}")


@router.post("/profiles/{pid}/toggle")
def profile_toggle(request: HttpRequest, pid: int, db: Session = Depends(get_db)):
    p = _get(db, Profile, pid)
    p.active = not p.active
    db.commit()
    flash(request, "Envois repris." if p.active else "Envois en pause pour ce profil.")
    return back(f"/profiles/{pid}")


@router.get("/profiles/{pid}/preview", response_class=HTMLResponse)
def profile_preview(request: HttpRequest, pid: int, broker_id: int | None = None, lang: str | None = None, db: Session = Depends(get_db)):
    p = _get(db, Profile, pid)
    b = db.get(Broker, broker_id) if broker_id else db.scalars(select(Broker).where(Broker.email.is_not(None)).order_by(Broker.name)).first()
    if not b:
        raise HTTPException(404, "Aucun courtier")
    language = lang if lang in ("fr", "en") else letters.letter_language(p, b)
    subject, body = letters.erasure_letter(p, b, f"{settings.reference_prefix}-XXXXXX", language)
    return render(request, "preview.html", db, p=p, b=b, subject=subject, body=body, language=language)
