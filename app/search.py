"""Recherche transversale dans les données déjà présentes dans Oubliez-moi."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import __version__, case_management
from .config import settings
from .db import (
    FAILED, NEEDS_ACTION, OVERDUE, REPLIED, Broker, Profile, Request as PrivacyRequest,
    Sender, SessionLocal,
)
from .main_templates import templates

router = APIRouter()
router.include_router(case_management.router)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/search", response_class=HTMLResponse)
def global_search(request: Request, q: str = "", db: Session = Depends(get_db)):
    query = q.strip()[:120]
    profiles, brokers, requests, senders, cases = [], [], [], [], []
    if len(query) >= 2:
        like = f"%{query}%"
        profiles = db.scalars(
            select(Profile).where(or_(Profile.full_name.ilike(like))).order_by(Profile.full_name).limit(8)
        ).all()
        brokers = db.scalars(
            select(Broker).where(or_(Broker.name.ilike(like), Broker.email.ilike(like), Broker.slug.ilike(like)))
            .order_by(Broker.name).limit(8)
        ).all()
        requests = db.scalars(
            select(PrivacyRequest).join(Broker).join(Profile)
            .where(or_(PrivacyRequest.reference.ilike(like), Broker.name.ilike(like), Broker.email.ilike(like), Profile.full_name.ilike(like)))
            .order_by(PrivacyRequest.id.desc()).limit(8)
        ).all()
        cases = db.scalars(
            select(case_management.PrivacyCase).join(case_management.Organization)
            .where(or_(case_management.Organization.name.ilike(like), case_management.PrivacyCase.subject.ilike(like),
                       case_management.PrivacyCase.reference.ilike(like)))
            .order_by(case_management.PrivacyCase.id.desc()).limit(8)
        ).all()
        senders = db.scalars(
            select(Sender).where(or_(Sender.name.ilike(like), Sender.address.ilike(like), Sender.domain.ilike(like)))
            .order_by(Sender.count.desc()).limit(8)
        ).all()
    todo = db.scalar(select(func.count(PrivacyRequest.id)).where(
        PrivacyRequest.status.in_([NEEDS_ACTION, REPLIED, OVERDUE, FAILED])
    )) or 0
    return templates.TemplateResponse(request, "search.html", {
        "q": query, "profiles": profiles, "brokers": brokers, "requests": requests, "senders": senders, "cases": cases,
        "request": request, "settings": settings, "version": __version__, "nav_todo": todo,
        "flashes": request.session.pop("flash", []),
    })
