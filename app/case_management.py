"""Suivi des demandes RGPD générales, distinct des campagnes d'effacement des courtiers."""
from __future__ import annotations

import calendar
from datetime import date, datetime
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import ForeignKey, String, Text, DateTime, Integer, select
from sqlalchemy.orm import Mapped, mapped_column, relationship, Session, selectinload

from . import __version__
from .db import Base, Profile, SessionLocal, utcnow
from .main_templates import templates

router = APIRouter()
CASE_TYPES = {
    "access": "Droit d’accès",
    "erasure": "Droit à l’effacement",
    "objection": "Droit d’opposition",
    "rectification": "Droit de rectification",
    "restriction": "Limitation du traitement",
    "portability": "Droit à la portabilité",
}
CASE_STATUSES = {
    "draft": "Brouillon",
    "sent": "Envoyée",
    "acknowledged": "Accusé de réception",
    "in_progress": "En cours",
    "completed": "Traitée — résultat confirmé",
    "refused": "Refusée",
    "closed": "Clôturée",
}
OPEN_CASE_STATUSES = {"draft", "sent", "acknowledged", "in_progress"}
FINAL_CASE_STATUSES = {"completed", "refused", "closed"}


class Organization(Base):
    __tablename__ = "privacy_organizations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(300), nullable=True)
    postal_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    cases: Mapped[list["PrivacyCase"]] = relationship(back_populates="organization")


class PrivacyCase(Base):
    __tablename__ = "privacy_cases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("privacy_organizations.id", ondelete="RESTRICT"), index=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True, index=True)
    request_type: Mapped[str] = mapped_column(String(30), default="erasure")
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    subject: Mapped[str] = mapped_column(String(300), default="")
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    extended_deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    extension_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    organization: Mapped[Organization] = relationship(back_populates="cases")
    profile: Mapped[Profile | None] = relationship()
    events: Mapped[list["CaseEvent"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="CaseEvent.at"
    )

    @property
    def status_label(self) -> str:
        return CASE_STATUSES.get(self.status, self.status)

    @property
    def type_label(self) -> str:
        return CASE_TYPES.get(self.request_type, self.request_type)

    @property
    def effective_deadline(self) -> datetime | None:
        return self.extended_deadline_at or self.deadline_at

    @property
    def overdue(self) -> bool:
        return bool(self.effective_deadline and self.status in OPEN_CASE_STATUSES
                    and self.effective_deadline.date() < utcnow().date())


class CaseEvent(Base):
    __tablename__ = "privacy_case_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("privacy_cases.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(30), default="note")
    summary: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    case: Mapped[PrivacyCase] = relationship(back_populates="events")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _get(db: Session, model, ident: int):
    obj = db.get(model, ident)
    if obj is None:
        raise HTTPException(404)
    return obj


def _event(db: Session, case: PrivacyCase, kind: str, summary: str, body: str | None = None):
    db.add(CaseEvent(case=case, kind=kind, summary=summary[:500], body=body))


def _render(request: Request, name: str, db: Session, **context):
    from sqlalchemy import func
    from .db import FAILED, NEEDS_ACTION, OVERDUE, REPLIED, Request as ExistingRequest
    todo = db.scalar(select(func.count(ExistingRequest.id)).where(
        ExistingRequest.status.in_([NEEDS_ACTION, REPLIED, OVERDUE, FAILED])
    )) or 0
    return templates.TemplateResponse(request, name, {
        **context, "request": request, "settings": __import__("app.config", fromlist=["settings"]).settings,
        "version": __version__, "nav_todo": todo, "flashes": request.session.pop("flash", []),
        "CASE_TYPES": CASE_TYPES, "CASE_STATUSES": CASE_STATUSES,
    })


@router.get("/privacy-cases", response_class=HTMLResponse)
def cases_list(request: Request, status: str = "", q: str = "", db: Session = Depends(get_db)):
    stmt = select(PrivacyCase).join(Organization).options(
        selectinload(PrivacyCase.organization), selectinload(PrivacyCase.profile)
    )
    if status in CASE_STATUSES:
        stmt = stmt.where(PrivacyCase.status == status)
    if q.strip():
        like = f"%{q.strip()[:120]}%"
        stmt = stmt.where((Organization.name.ilike(like)) | (PrivacyCase.subject.ilike(like))
                          | (PrivacyCase.reference.ilike(like)))
    cases = db.scalars(stmt.order_by(PrivacyCase.updated_at.desc(), PrivacyCase.id.desc()).limit(500)).all()
    organizations = db.scalars(select(Organization).order_by(Organization.name)).all()
    profiles = db.scalars(select(Profile).order_by(Profile.full_name)).all()
    return _render(request, "privacy_cases.html", db, cases=cases, organizations=organizations,
                   profiles=profiles, status=status, q=q)


@router.post("/organizations")
def organization_create(request: Request, name: str = Form(...), website: str = Form(""),
                        contact_email: str = Form(""), postal_address: str = Form(""),
                        notes: str = Form(""), db: Session = Depends(get_db)):
    name = name.strip()[:300]
    if not name:
        raise HTTPException(400, "Le nom de l’organisme est obligatoire.")
    org = db.scalar(select(Organization).where(Organization.name.ilike(name)))
    if org is None:
        org = Organization(name=name)
        db.add(org)
    org.website = website.strip()[:500] or None
    org.contact_email = contact_email.strip()[:300] or None
    org.postal_address = postal_address.strip() or None
    org.notes = notes.strip() or None
    db.commit()
    request.session.setdefault("flash", []).append(["ok", f"Organisme « {name} » enregistré."])
    return RedirectResponse("/privacy-cases", status_code=303)


@router.post("/privacy-cases")
def case_create(request: Request, organization_id: int = Form(...), request_type: str = Form(...),
                subject: str = Form(""), profile_id: int = Form(0), reference: str = Form(""),
                note: str = Form(""), db: Session = Depends(get_db)):
    if request_type not in CASE_TYPES:
        raise HTTPException(400, "Type de demande invalide.")
    org = _get(db, Organization, organization_id)
    profile = _get(db, Profile, profile_id) if profile_id else None
    case = PrivacyCase(organization=org, profile=profile, request_type=request_type,
                       subject=subject.strip()[:300], reference=reference.strip()[:150] or None)
    db.add(case)
    db.flush()
    _event(db, case, "created", "Dossier créé", note.strip() or None)
    db.commit()
    request.session.setdefault("flash", []).append(["ok", "Dossier RGPD créé. Aucun courrier n’a été envoyé."])
    return RedirectResponse(f"/privacy-cases/{case.id}", status_code=303)


@router.get("/privacy-cases/{case_id}", response_class=HTMLResponse)
def case_detail(request: Request, case_id: int, db: Session = Depends(get_db)):
    case = db.scalar(select(PrivacyCase).where(PrivacyCase.id == case_id).options(
        selectinload(PrivacyCase.organization), selectinload(PrivacyCase.profile),
        selectinload(PrivacyCase.events),
    ))
    if case is None:
        raise HTTPException(404)
    return _render(request, "privacy_case_detail.html", db, case=case)


@router.post("/privacy-cases/{case_id}/status")
def case_status(request: Request, case_id: int, status: str = Form(...), note: str = Form(""),
                db: Session = Depends(get_db)):
    case = _get(db, PrivacyCase, case_id)
    if status not in CASE_STATUSES:
        raise HTTPException(400, "Statut invalide.")
    previous = case.status
    case.status = status
    if status == "sent" and case.sent_at is None:
        case.sent_at = utcnow()
        case.deadline_at = add_months(case.sent_at, 1)
        _event(db, case, "sent", "Demande marquée comme envoyée",
               "Date d’envoi enregistrée manuellement. Aucune transmission automatique n’a été effectuée.")
    if status in FINAL_CASE_STATUSES:
        case.closed_at = utcnow()
    elif previous in FINAL_CASE_STATUSES:
        case.closed_at = None
    _event(db, case, "status", f"Statut : {CASE_STATUSES[status]}" + (f" — {note.strip()}" if note.strip() else ""),
           note.strip() or None)
    db.commit()
    request.session.setdefault("flash", []).append(["ok", f"Statut du dossier mis à jour : {CASE_STATUSES[status]}."])
    return RedirectResponse(f"/privacy-cases/{case_id}", status_code=303)


@router.post("/privacy-cases/{case_id}/note")
def case_note(request: Request, case_id: int, summary: str = Form(...), body: str = Form(""),
              db: Session = Depends(get_db)):
    case = _get(db, PrivacyCase, case_id)
    summary = summary.strip()[:500]
    if not summary:
        raise HTTPException(400, "Le titre de l’événement est obligatoire.")
    _event(db, case, "note", summary, body.strip() or None)
    db.commit()
    request.session.setdefault("flash", []).append(["ok", "Événement ajouté à l’historique."])
    return RedirectResponse(f"/privacy-cases/{case_id}", status_code=303)


@router.post("/privacy-cases/{case_id}/extend")
def case_extend(request: Request, case_id: int, note: str = Form(""), db: Session = Depends(get_db)):
    case = _get(db, PrivacyCase, case_id)
    if not case.sent_at or not case.deadline_at:
        raise HTTPException(400, "La demande doit d’abord être marquée comme envoyée.")
    if case.extended_deadline_at:
        raise HTTPException(409, "Une prolongation a déjà été enregistrée.")
    if utcnow() > add_months(case.sent_at, 1):
        raise HTTPException(400, "La prolongation doit être signalée au demandeur dans le premier mois.")
    case.extended_deadline_at = add_months(case.deadline_at, 2)
    case.extension_notified_at = utcnow()
    _event(db, case, "extension", "Prolongation de deux mois enregistrée",
           "L’utilisateur a confirmé avoir informé la personne concernée de la prolongation."
           + (f" Note : {note.strip()}" if note.strip() else ""))
    db.commit()
    request.session.setdefault("flash", []).append(["ok", "Prolongation enregistrée. Pensez à conserver la preuve de l’information envoyée."])
    return RedirectResponse(f"/privacy-cases/{case_id}", status_code=303)
