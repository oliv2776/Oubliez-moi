"""Briques communes à toutes les pages : gabarits, session de base, rendu, compteur « À traiter ».

Toutes les pages (main.py et les routeurs secondaires) passent par ce module : un seul
environnement Jinja (donc les mêmes filtres partout) et un seul calcul du compteur du menu.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request as HttpRequest
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import __version__, mailview
from .config import settings
from .db import STATUS_LABELS, TODO_STATUSES, Request, SessionLocal

log = logging.getLogger("oubliez-moi")
BASE = Path(__file__).parent
SEED = BASE / "seed" / "brokers.json"
TZ = ZoneInfo(settings.timezone)
templates = Jinja2Templates(directory=BASE / "templates")


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
        csp_nonce=getattr(request.state, "csp_nonce", ""),
    )
    return templates.TemplateResponse(request, name, ctx)


# ------------------------------------------------- Filtres et petits outils ---
def _local(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ) if dt else None


def fmt_dt(dt):
    return _local(dt).strftime("%d/%m/%Y %H:%M") if dt else "—"


def fmt_d(dt):
    return _local(dt).strftime("%d/%m/%Y") if dt else "—"


templates.env.filters["dt"] = fmt_dt
templates.env.filters["d"] = fmt_d
templates.env.filters["isodt"] = lambda v: fmt_dt(datetime.fromisoformat(v)) if v else "jamais"
templates.env.filters["url"] = lambda v: v if v and re.match(r"(?i)^https?://", v) else "#"
templates.env.filters["host"] = lambda v: (urlsplit(v).hostname or "") if v else ""
def _mailheaders(body):
    h = dict(mailview.split_headers(body or "")[0])
    sender = h.get("De", "").strip()
    if sender.startswith("<") and sender.endswith(">"):
        h["De"] = sender[1:-1]
    return h


templates.env.filters["mailheaders"] = _mailheaders
templates.env.filters["mailtext"] = lambda body: Markup(mailview.text_to_html(mailview.split_headers(body or "")[1]))
templates.env.filters["lines"] = lambda v: "\n".join(v or [])


def flash(request: HttpRequest, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])




def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _split(text: str) -> list[str]:
    return [line.strip() for line in (text or "").replace("\r", "").split("\n") if line.strip()]


def _get(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if not obj:
        raise HTTPException(404)
    return obj


def _safe_next(url: str | None) -> str | None:
    """N'accepte qu'une redirection interne (évite les redirections ouvertes)."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return None
