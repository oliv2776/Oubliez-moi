"""Briques communes à toutes les pages : gabarits, session de base, rendu, compteur « À traiter ».

Toutes les pages (main.py et les routeurs secondaires) passent par ce module : un seul
environnement Jinja (donc les mêmes filtres partout) et un seul calcul du compteur du menu.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import __version__
from .config import settings
from .db import STATUS_LABELS, TODO_STATUSES, Request, SessionLocal

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def todo_count(db: Session) -> int:
    """Demandes aux courtiers à traiter + dossiers RGPD dont l'échéance est dépassée."""
    from .case_management import overdue_cases_filter, PrivacyCase  # import tardif : évite un cycle

    requests = db.scalar(select(func.count(Request.id)).where(Request.status.in_(TODO_STATUSES))) or 0
    cases = db.scalar(select(func.count(PrivacyCase.id)).where(overdue_cases_filter())) or 0
    return requests + cases


def render(request, name: str, db: Session | None = None, **ctx):
    ctx.update(
        request=request,
        settings=settings,
        STATUS=STATUS_LABELS,
        flashes=request.session.pop("flash", []),
        nav_todo=todo_count(db) if db is not None else 0,
        version=__version__,
    )
    return templates.TemplateResponse(request, name, ctx)
