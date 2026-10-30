"""Application web Oubliez-moi."""
from __future__ import annotations

import csv
import hmac
import io
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, brokers as broker_catalog, letters, llm, mailer, mailview, notify, phones, scheduler, service, settings_store
from .config import settings
from .db import (
    FAILED,
    NEEDS_ACTION,
    OVERDUE,
    QUEUED,
    REPLIED,
    STATUS_LABELS,
    Broker,
    Event,
    Profile,
    Request,
    SessionLocal,
    add_event,
    init_db,
    kv_get,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("uvicorn.access").addFilter(lambda r: "/healthz" not in r.getMessage())
log = logging.getLogger("oubliez-moi")
BASE = Path(__file__).parent
SEED = BASE / "seed" / "brokers.json"
TZ = ZoneInfo(settings.timezone)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    settings_store.apply_overrides()
    with SessionLocal() as db:
        if not db.scalar(select(func.count(Broker.id))):
            res = service.sync_brokers(db, broker_catalog.load_seed(SEED))
            log.info("Catalogue initial chargé : %s courtiers", res["added"])
    if not settings.admin_password:
        log.warning("ADMIN_PASSWORD n'est pas défini : la connexion est impossible tant qu'il ne l'est pas.")
    if settings.dry_run:
        log.warning("Mode SIMULATION (DRY_RUN=true) : aucun e-mail ne sera réellement envoyé.")
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Oubliez-moi", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

PUBLIC_PATHS = ("/login", "/static", "/healthz")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not request.url.path.startswith(PUBLIC_PATHS) and not request.session.get("user"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax", max_age=7 * 24 * 3600, https_only=settings.cookie_secure)


# ---------------------------------------------------------------- Vues ----

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


def render(request: HttpRequest, name: str, db: Session | None = None, **ctx):
    todo = 0
    if db is not None:
        todo = db.scalar(select(func.count(Request.id)).where(Request.status.in_([NEEDS_ACTION, REPLIED, OVERDUE, FAILED]))) or 0
    ctx.update(
        request=request,
        settings=settings,
        STATUS=STATUS_LABELS,
        flashes=request.session.pop("flash", []),
        nav_todo=todo,
        version=__version__,
    )
    return templates.TemplateResponse(request, name, ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _split(text: str) -> list[str]:
    return [line.strip() for line in (text or "").replace("\r", "").split("\n") if line.strip()]


def _get(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if not obj:
        raise HTTPException(404)
    return obj


# ----------------------------------------------------------- Connexion ----

@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.get("/login", response_class=HTMLResponse)
def login_form(request: HttpRequest):
    return render(request, "login.html")


@app.post("/login")
def login(request: HttpRequest, username: str = Form(""), password: str = Form("")):
    ok = bool(settings.admin_password) and hmac.compare_digest(username.encode(), settings.admin_user.encode()) & hmac.compare_digest(
        password.encode(), settings.admin_password.encode()
    )
    if not ok:
        time.sleep(1.5)
        msg = "Identifiants incorrects." if settings.admin_password else "ADMIN_PASSWORD n'est pas défini dans le fichier .env."
        flash(request, msg, "error")
        return back("/login")
    request.session["user"] = username
    return back("/")


@app.post("/logout")
def logout(request: HttpRequest):
    request.session.clear()
    return back("/login")


# ------------------------------------------------------ Tableau de bord ----

@app.get("/", response_class=HTMLResponse)
def dashboard(request: HttpRequest, db: Session = Depends(get_db)):
    profiles = db.scalars(select(Profile).order_by(Profile.id)).all()
    per_profile = [(p, service.stats(db, p.id)) for p in profiles]
    events = db.scalars(
        select(Event).options(selectinload(Event.request).selectinload(Request.broker))
        .where(Event.kind.in_(["sent", "reminder", "reply", "status", "error"]))
        .order_by(Event.at.desc()).limit(25)
    ).all()
    upcoming = db.scalars(
        select(Request).where(Request.status == "sent", Request.due_at.is_not(None)).order_by(Request.due_at).limit(8)
    ).all()
    queued = db.scalar(select(func.count(Request.id)).where(Request.status == QUEUED)) or 0
    eta_hours = round(queued / settings.send_per_hour, 1) if queued else 0
    return render(
        request, "dashboard.html", db,
        per_profile=per_profile, total=service.stats(db), events=events, upcoming=upcoming,
        queued=queued, eta_hours=eta_hours,
        brokers_enabled=db.scalar(select(func.count(Broker.id)).where(Broker.enabled.is_(True))),
        imap_polled_at=kv_get(db, "imap_polled_at"),
    )


# -------------------------------------------------------------- Profils ----

@app.get("/profiles", response_class=HTMLResponse)
def profiles_list(request: HttpRequest, db: Session = Depends(get_db)):
    profiles = db.scalars(select(Profile).order_by(Profile.id)).all()
    return render(request, "profiles.html", db, profiles=[(p, service.stats(db, p.id)) for p in profiles])


@app.get("/profiles/new", response_class=HTMLResponse)
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


@app.post("/profiles")
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


@app.get("/profiles/{pid}", response_class=HTMLResponse)
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


@app.post("/profiles/{pid}/supplement")
def profile_supplement(request: HttpRequest, pid: int, db: Session = Depends(get_db)):
    p = _get(db, Profile, pid)
    n = service.queue_supplements(db, p)
    flash(request, f"{n} complément(s) mis en file : ils partiront au rythme normal d'envoi." if n else "Aucun complément à envoyer.")
    return back(f"/profiles/{pid}")


@app.get("/profiles/{pid}/edit", response_class=HTMLResponse)
def profile_edit(request: HttpRequest, pid: int, db: Session = Depends(get_db)):
    return render(request, "profile_form.html", db, profile=_get(db, Profile, pid))


@app.post("/profiles/{pid}")
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


@app.post("/profiles/{pid}/delete")
def profile_delete(request: HttpRequest, pid: int, confirm: str = Form(""), db: Session = Depends(get_db)):
    p = _get(db, Profile, pid)
    if confirm.strip() != p.full_name:
        flash(request, "Tapez le nom exact du profil pour confirmer la suppression.", "error")
        return back(f"/profiles/{pid}/edit")
    db.delete(p)
    db.commit()
    flash(request, "Profil et historique supprimés.")
    return back("/profiles")


@app.post("/profiles/{pid}/campaign")
def profile_campaign(request: HttpRequest, pid: int, db: Session = Depends(get_db)):
    p = _get(db, Profile, pid)
    n = service.start_campaign(db, p)
    if n:
        mode = " (mode simulation : rien ne part réellement)" if settings.dry_run else ""
        flash(request, f"{n} demandes créées. Envoi progressif à raison de {settings.send_per_hour} par heure{mode}.")
    else:
        flash(request, "Aucun nouveau courtier : toutes les demandes existent déjà.", "info")
    return back(f"/profiles/{pid}")


@app.post("/profiles/{pid}/toggle")
def profile_toggle(request: HttpRequest, pid: int, db: Session = Depends(get_db)):
    p = _get(db, Profile, pid)
    p.active = not p.active
    db.commit()
    flash(request, "Envois repris." if p.active else "Envois en pause pour ce profil.")
    return back(f"/profiles/{pid}")


@app.get("/profiles/{pid}/preview", response_class=HTMLResponse)
def profile_preview(request: HttpRequest, pid: int, broker_id: int | None = None, lang: str | None = None, db: Session = Depends(get_db)):
    p = _get(db, Profile, pid)
    b = db.get(Broker, broker_id) if broker_id else db.scalars(select(Broker).where(Broker.email.is_not(None)).order_by(Broker.name)).first()
    if not b:
        raise HTTPException(404, "Aucun courtier")
    language = lang if lang in ("fr", "en") else letters.letter_language(p, b)
    subject, body = letters.erasure_letter(p, b, f"{settings.reference_prefix}-XXXXXX", language)
    return render(request, "preview.html", db, p=p, b=b, subject=subject, body=body, language=language)


# ------------------------------------------------------------ Demandes ----

@app.get("/requests", response_class=HTMLResponse)
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


@app.get("/requests/{rid}", response_class=HTMLResponse)
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


def _safe_next(url: str | None) -> str | None:
    """N'accepte qu'une redirection interne (évite les redirections ouvertes)."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return None


@app.get("/requests/{rid}/dossier", response_class=HTMLResponse)
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


@app.get("/events/{eid}/view", response_class=HTMLResponse)
def event_view(eid: int, db: Session = Depends(get_db)):
    """Contenu d'un message, servi isolé (cadre sandboxé + CSP sans aucun chargement externe)."""
    ev = _get(db, Event, eid)
    return HTMLResponse(
        mailview.render_event(ev.body, ev.html),
        headers={"Content-Security-Policy": mailview.CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"},
    )


@app.post("/requests/{rid}/status")
def request_status(request: HttpRequest, rid: int, status: str = Form(...), note: str = Form(""), next: str = Form(""),
                   db: Session = Depends(get_db)):
    r = _get(db, Request, rid)
    if status not in STATUS_LABELS:
        raise HTTPException(400)
    service.set_status(db, r, status, f"Statut changé manuellement : {STATUS_LABELS[status]}" + (f" — {note}" if note.strip() else ""))
    db.commit()
    flash(request, f"{r.broker.name} : {STATUS_LABELS[status]}.")
    return back(_safe_next(next) or request.headers.get("referer") or f"/requests/{rid}")


@app.post("/requests/{rid}/note")
def request_note(request: HttpRequest, rid: int, note: str = Form(...), db: Session = Depends(get_db)):
    r = _get(db, Request, rid)
    if note.strip():
        add_event(db, r, "note", note.strip()[:500], note.strip() if len(note) > 500 else None)
        db.commit()
    return back(f"/requests/{rid}")


@app.post("/requests/{rid}/send-reminder")
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


@app.post("/requests/requeue-failed")
def requeue_failed(request: HttpRequest, db: Session = Depends(get_db)):
    n = 0
    for r in db.scalars(select(Request).join(Broker).where(Request.status == FAILED, Broker.email.is_not(None))):
        service.set_status(db, r, QUEUED, "Remise en file d'attente")
        n += 1
    db.commit()
    flash(request, f"{n} demande(s) remise(s) en file d'attente.")
    return back("/requests?status=failed")


TRIAGE = [
    ("replied", "Réponses à examiner", [REPLIED]),
    ("needs", "Actions requises", [NEEDS_ACTION]),
    ("overdue", "Sans réponse", [OVERDUE]),
    ("failed", "Échecs d'envoi", [FAILED]),
]


@app.get("/tasks", response_class=HTMLResponse)
def tasks(request: HttpRequest, cat: str = "", id: int | None = None, db: Session = Depends(get_db)):
    cats = {k: st for k, _, st in TRIAGE}
    statuses = cats.get(cat) or [x for st in cats.values() for x in st]
    if cat not in cats:
        cat = ""
    activity = func.coalesce(Request.last_reply_at, Request.sent_at, Request.created_at)
    items = db.scalars(
        select(Request).options(selectinload(Request.broker), selectinload(Request.profile), selectinload(Request.events))
        .where(Request.status.in_(statuses)).order_by(activity.desc(), Request.id.desc())
    ).all()
    counts = dict(db.execute(select(Request.status, func.count()).where(Request.status.in_([x for st in cats.values() for x in st]))
                             .group_by(Request.status)).all())
    chips = [("", "Tout", sum(counts.values()))] + [(k, label, sum(counts.get(x, 0) for x in st)) for k, label, st in TRIAGE]

    r = next((x for x in items if x.id == id), None) or (items[0] if items else None)
    ctx = {"items": items, "cat": cat, "chips": chips, "r": r, "next_url": None, "multi_profiles": len({x.profile_id for x in items}) > 1}
    if r:
        i = items.index(r)
        after = items[i + 1] if i + 1 < len(items) else (items[i - 1] if i > 0 else None)
        prefix = f"/tasks?cat={cat}&" if cat else "/tasks?"
        ctx["next_url"] = f"{prefix}id={after.id}" if after else (f"/tasks?cat={cat}" if cat else "/tasks")
        ctx["prev_id"] = items[i - 1].id if i > 0 else None
        ctx["next_id"] = items[i + 1].id if i + 1 < len(items) else None
        ctx["position"] = i + 1
        replies = [e for e in r.events if e.kind == "reply"]
        sends = [e for e in r.events if e.kind in ("sent", "reminder", "supplement")]
        ctx["latest_reply"] = replies[-1] if replies else None
        ctx["latest_sent"] = sends[-1] if sends else None
        ctx["form_help"] = _form_help(r)
        infos = [e for e in r.events if e.kind in ("info", "confirm")]
        ctx["ai_note"] = next((e.summary for e in reversed(infos) if e.summary.startswith("Analyse IA")), None)
        ctx["confirm_link"] = next((e for e in reversed(infos) if e.body and e.body.startswith("http")), None)
        ctx["complaint"] = letters.cnil_complaint_text(r) if r.status in (OVERDUE, "refused") else None
    return render(request, "tasks.html", db, **ctx)


def _snippet(r: Request) -> str:
    if r.status == FAILED:
        return r.last_error or "Échec d'envoi"
    if r.status == OVERDUE:
        return f"Aucune réponse, échéance dépassée le {fmt_d(r.due_at)}"
    if r.channel == "webform" and not r.sent_at:
        return "Formulaire en ligne à remplir"
    replies = [e for e in r.events if e.kind == "reply"]
    if replies:
        return replies[-1].summary.split(" : ", 1)[-1]
    return STATUS_LABELS.get(r.status, r.status)


templates.env.globals["snippet"] = _snippet


@app.get("/export.csv")
def export_csv(db: Session = Depends(get_db)):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["reference", "profil", "courtier", "contact", "cycle", "statut", "envoyee_le", "echeance", "relances", "derniere_reponse", "close_le"])
    for r in db.scalars(select(Request).options(selectinload(Request.broker), selectinload(Request.profile)).order_by(Request.id)):
        w.writerow([r.reference, r.profile.full_name, r.broker.name, r.broker.email or r.broker.webform, r.cycle, r.status_label,
                    fmt_dt(r.sent_at), fmt_d(r.due_at), r.reminder_count, fmt_dt(r.last_reply_at), fmt_dt(r.closed_at)])
    data = "﻿" + buf.getvalue()  # BOM pour Excel
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="oubliez-moi-{datetime.now():%Y%m%d}.csv"'})


# ----------------------------------------------------------- Courtiers ----

@app.get("/brokers", response_class=HTMLResponse)
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


@app.post("/brokers/{bid}/toggle")
def broker_toggle(request: HttpRequest, bid: int, db: Session = Depends(get_db)):
    b = _get(db, Broker, bid)
    b.enabled = not b.enabled
    b.enabled_overridden = True
    db.commit()
    return back(request.headers.get("referer") or "/brokers")


@app.post("/brokers/bulk")
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


@app.post("/brokers/reset-defaults")
def broker_reset(request: HttpRequest, db: Session = Depends(get_db)):
    for b in db.scalars(select(Broker).where(Broker.source != "custom")):
        b.enabled_overridden = False
    n = service.apply_country_defaults(db)
    flash(request, f"Sélection par défaut (pays : {', '.join(settings.broker_countries)}) rétablie — {n} changement(s).")
    return back("/brokers")


@app.get("/brokers/new", response_class=HTMLResponse)
def broker_new(request: HttpRequest, db: Session = Depends(get_db)):
    return render(request, "broker_form.html", db)


@app.post("/brokers/new")
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


@app.post("/brokers/{bid}/delete")
def broker_delete(request: HttpRequest, bid: int, db: Session = Depends(get_db)):
    b = _get(db, Broker, bid)
    if b.source != "custom":
        flash(request, "Seuls les courtiers ajoutés à la main peuvent être supprimés (désactivez les autres).", "error")
    else:
        db.delete(b)
        db.commit()
        flash(request, "Courtier supprimé.")
    return back("/brokers")


@app.post("/brokers/update")
def brokers_update(request: HttpRequest):
    try:
        res = service.update_brokers_from_remote()
        flash(request, f"Catalogue mis à jour : {res['added']} nouveau(x), {res['updated']} actualisé(s).")
    except Exception as e:  # noqa: BLE001
        log.exception("Mise à jour du catalogue")
        flash(request, f"Mise à jour impossible : {e}", "error")
    return back("/brokers")


# ------------------------------------------------------------- Réglages ----

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: HttpRequest, db: Session = Depends(get_db)):
    return render(
        request, "settings.html", db,
        imap_polled_at=kv_get(db, "imap_polled_at"), synced_at=kv_get(db, "brokers_synced_at"),
        overridden=settings_store.overridden_keys(), file_status=(kv_get(db, "imap_file_status") or "").split("|", 2),
        llm_models=llm.MODELS, llm_installed=llm.installed_models() if settings.llm_url else None,
        llm_in_catalog=bool(llm.catalog_kind(settings.llm_model)),
    )


TABS = ("general", "compte", "releve", "automatismes", "notifications", "auto")


def _tab_url(form) -> str:
    tab = form.get("tab", "")
    return f"/settings#{tab}" if tab in TABS else "/settings"


def _save_settings(request: HttpRequest, form: dict) -> bool:
    """Enregistre les réglages si la requête vient du formulaire Réglages. Renvoie False en cas d'erreur."""
    if "_settings_form" not in form:
        return True  # bouton utilisé ailleurs (ex. tableau de bord) : rien à enregistrer
    was_dry = settings.dry_run
    errors = settings_store.save_from_form(form)
    for e in errors:
        flash(request, e, "error")
    if errors:
        return False
    scheduler.reschedule()
    if was_dry and not settings.dry_run:
        n = service.requeue_simulated()
        msg = "Envoi réel activé : les demandes en file vont maintenant partir"
        flash(request, msg + (f" (dont {n} demande(s) simulée(s) remise(s) en file)." if n else "."))
    return True


@app.post("/settings")
async def settings_save(request: HttpRequest):
    form = dict(await request.form())
    if _save_settings(request, form):
        flash(request, "Réglages enregistrés.")
    return back(_tab_url(form))


@app.post("/settings/reset")
def settings_reset(request: HttpRequest):
    settings_store.reset_to_env()
    scheduler.reschedule()
    flash(request, "Réglages de l'interface effacés : les valeurs du fichier .env s'appliquent de nouveau.")
    return back("/settings")


@app.post("/settings/test-smtp")
async def test_smtp(request: HttpRequest):
    form = dict(await request.form())
    if not _save_settings(request, form):
        return back(_tab_url(form))
    if not settings.smtp_configured:
        flash(request, "Renseignez le serveur d'envoi et l'adresse e-mail.", "error")
        return back(_tab_url(form))
    msg = mailer.build_message(settings.smtp_from, "Oubliez-moi – e-mail de test", "Si vous lisez ceci, l'envoi SMTP fonctionne.", "Oubliez-moi")
    try:
        mailer.send_message(msg, force=True)  # le test part réellement, même en mode simulation
        flash(request, f"Réglages enregistrés. E-mail de test envoyé à {settings.smtp_from} : vérifiez votre boîte.")
    except mailer.SendError as e:
        flash(request, str(e), "error")
    return back(_tab_url(form))


@app.post("/settings/test-imap")
async def test_imap(request: HttpRequest):
    form = dict(await request.form())
    if not _save_settings(request, form):
        return back(_tab_url(form))
    if not settings.imap_configured:
        flash(request, "Renseignez le serveur de relève (et les identifiants s'ils diffèrent de ceux de l'envoi).", "error")
    else:
        try:
            flash(request, "Réglages enregistrés. " + mailer.test_imap())
        except Exception as e:  # noqa: BLE001
            flash(request, f"Erreur IMAP : {e}", "error")
    return back(_tab_url(form))


@app.post("/settings/test-llm")
async def test_llm(request: HttpRequest):
    form = dict(await request.form())
    if not _save_settings(request, form):
        return back(_tab_url(form))
    if not settings.llm_enabled:
        flash(request, "Indiquez l'adresse d'Ollama et le nom du modèle.", "error")
    else:
        try:
            flash(request, "Réglages enregistrés. " + llm.test())
        except Exception as e:  # noqa: BLE001
            flash(request, f"IA locale : {e}", "error")
    return back(_tab_url(form))


@app.post("/settings/llm-pull")
async def llm_pull(request: HttpRequest):
    """Lance le téléchargement d'un modèle dans Ollama (en arrière-plan)."""
    form = dict(await request.form())
    if form.get("_settings_form"):
        settings_store.save_from_form(form)
    model = form.get("model") or settings.llm_model
    try:
        state = llm.start_pull(model)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"model": model, **state})


@app.get("/settings/llm-status")
def llm_status(model: str = ""):
    model = model or settings.llm_model
    installed = llm.installed_models(max_age=0 if llm.pull_status(model).get("done") else 20)
    return JSONResponse({
        "model": model,
        "reachable": installed is not None,
        "installed": llm.normalize(model) in {llm.normalize(n) for n in (installed or [])},
        "pull": llm.pull_status(model),
    })


@app.post("/settings/test-notify")
async def test_notify(request: HttpRequest):
    form = dict(await request.form())
    if not _save_settings(request, form):
        return back(_tab_url(form))
    if not settings.notify_enabled:
        flash(request, "Configurez ntfy ou Gotify.", "error")
    else:
        errors = notify.push("Oubliez-moi : test", "Les notifications fonctionnent. Vous serez prévenu quand une décision vous attend.")
        flash(request, "Réglages enregistrés. " + ("Notification de test envoyée." if not errors else " ; ".join(errors)), "error" if errors else "ok")
    return back(_tab_url(form))


@app.post("/settings/poll")
async def poll_now(request: HttpRequest):
    form = dict(await request.form())
    if not _save_settings(request, form):
        return back(_tab_url(form))
    try:
        n = service.poll_imap()
        flash(request, f"Relève terminée : {n} réponse(s) rattachée(s).")
    except Exception as e:  # noqa: BLE001
        flash(request, f"Erreur IMAP : {e}", "error")
    return back(_tab_url(form) if "_settings_form" in form else request.headers.get("referer") or "/settings")


@app.post("/settings/send-now")
def send_now(request: HttpRequest):
    res = service.send_tick()
    flash(request, f"Envoi : {res}" if res else "Rien à envoyer pour le moment.", "info")
    return back(request.headers.get("referer") or "/")
