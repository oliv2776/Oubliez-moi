"""Recherche transversale dans les données déjà présentes dans Oubliez-moi."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import case_management
from .db import Broker, Profile, Request as PrivacyRequest, Sender
from .web import get_db, render

router = APIRouter()




@router.get("/search", response_class=HTMLResponse)
def global_search(request: Request, q: str = "", db: Session = Depends(get_db)):
    query = q.strip()[:120]
    profiles, brokers, requests, senders, cases = [], [], [], [], []
    if len(query) >= 2:
        # % et _ sont des jokers SQL : on les échappe pour chercher le texte tel quel.
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        profiles = db.scalars(
            select(Profile).where(or_(Profile.full_name.ilike(like, escape="\\"))).order_by(Profile.full_name).limit(8)
        ).all()
        brokers = db.scalars(
            select(Broker).where(or_(Broker.name.ilike(like, escape="\\"), Broker.email.ilike(like, escape="\\"), Broker.slug.ilike(like, escape="\\")))
            .order_by(Broker.name).limit(8)
        ).all()
        requests = db.scalars(
            select(PrivacyRequest).join(Broker).join(Profile)
            .where(or_(PrivacyRequest.reference.ilike(like, escape="\\"), Broker.name.ilike(like, escape="\\"), Broker.email.ilike(like, escape="\\"), Profile.full_name.ilike(like, escape="\\")))
            .order_by(PrivacyRequest.id.desc()).limit(8)
        ).all()
        cases = db.scalars(
            select(case_management.PrivacyCase).join(case_management.Organization)
            .where(or_(case_management.Organization.name.ilike(like, escape="\\"), case_management.PrivacyCase.subject.ilike(like, escape="\\"),
                       case_management.PrivacyCase.reference.ilike(like, escape="\\")))
            .order_by(case_management.PrivacyCase.id.desc()).limit(8)
        ).all()
        senders = db.scalars(
            select(Sender).where(or_(Sender.name.ilike(like, escape="\\"), Sender.address.ilike(like, escape="\\"), Sender.domain.ilike(like, escape="\\")))
            .order_by(Sender.count.desc()).limit(8)
        ).all()
    return render(request, "search.html", db, q=query, profiles=profiles, brokers=brokers, requests=requests,
                  senders=senders, cases=cases)
