"""Application web Oubliez-moi."""
from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import re
import time
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import backup, brokers as broker_catalog, crypto, letters, llm, mailboxes, oauth_ms, mailer, mailview, notify, phones, scheduler, service, settings_store, web
from . import actions, case_assistant, case_management, device_ids, energy, exposures, insight, migration_routes, reanalysis, search as search_routes, task_export
from .config import settings
from .login_security import login_attempt_limiter
from .db import (
    FAILED,
    NEEDS_ACTION,
    OVERDUE,
    QUEUED,
    REPLIED,
    SENT,
    STATUS_LABELS,
    Broker,
    SENDER_STATUSES,
    Event,
    Mailbox,
    Profile,
    Sender,
    Request,
    SessionLocal,
    add_event,
    init_db,
    kv_get,
    kv_set,
    utcnow,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("uvicorn.access").addFilter(lambda r: "/healthz" not in r.getMessage())
log = logging.getLogger("oubliez-moi")
BASE = Path(__file__).parent
SEED = BASE / "seed" / "brokers.json"
TZ = ZoneInfo(settings.timezone)




@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    crypto.fernet()  # crée la clé de chiffrement dès le premier démarrage
    migrated = settings_store.apply_overrides()
    if migrated:
        log.info("%d mot(s) de passe stocké(s) en clair ont été chiffrés", migrated)
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
templates = web.templates  # environnement Jinja partagé : les filtres ci-dessous servent à toutes les pages
for _module in (search_routes, case_management, case_assistant, exposures, migration_routes, device_ids):
    app.include_router(_module.router)

PUBLIC_PATHS = ("/login", "/healthz")
PUBLIC_PREFIXES = ("/static",)


def _origin_of(url: str) -> tuple[str, str, int] | None:
    """(schéma, hôte, port) d'une URL http(s), ou None si elle n'est pas exploitable."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


def allowed_origins(request) -> set[tuple[str, str, int]]:
    """Origines légitimes : celle de l'URL utilisée (en-tête Host) et, si définie, APP_URL.

    Accepter les deux évite de se retrouver bloqué hors de l'interface quand APP_URL
    pointe vers un nom de domaine mais que l'on passe par l'IP du réseau local.
    Ce n'est pas un affaiblissement : une page malveillante ne peut pas choisir
    l'en-tête Origin que le navigateur de la victime envoie.
    """
    out = set()
    for url in (f"{request.url.scheme}://{request.headers.get('host', '')}", settings.app_url):
        if url and (origin := _origin_of(url)):
            out.add(origin)
    return out


class CSRFMiddleware(BaseHTTPMiddleware):
    """Bloque les requêtes mutantes cross-origin, y compris le formulaire de connexion.

    1. Origin (ou à défaut Referer) doit correspondre à une origine autorisée ;
    2. si le navigateur n'envoie ni l'une ni l'autre (ou « Origin: null »),
       l'en-tête Sec-Fetch-Site, que les scripts ne peuvent pas falsifier,
       doit valoir « same-origin ».
    """
    async def dispatch(self, request, call_next):
        if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            return await call_next(request)
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        origin = request.headers.get("origin", "")
        source = origin if origin and origin != "null" else request.headers.get("referer", "")
        if fetch_site == "cross-site":
            ok = False
        elif source:
            ok = _origin_of(source) in allowed_origins(request)
        else:
            ok = fetch_site == "same-origin"
        if not ok:
            return JSONResponse({"detail": "Origine de requête invalide (protection CSRF)."}, status_code=403)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Applique des en-têtes défensifs et interdit la mise en cache des pages privées."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        # « same-origin » et surtout pas « no-referrer » : avec no-referrer, les navigateurs
        # envoient « Origin: null » sur chaque POST et la protection CSRF bloque tout.
        # Aucun Referer n'est transmis aux sites externes.
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        is_public = path in PUBLIC_PATHS or any(
            path == prefix or path.startswith(prefix + "/") for prefix in PUBLIC_PREFIXES
        )
        if not is_public and not request.session.get("user"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


app.add_middleware(AuthMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
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


render = web.render
get_db = web.get_db


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
    # Le pair TCP est utilisé, jamais un X-Forwarded-For fourni librement par le client.
    client = request.client.host if request.client else "unknown"
    retry_after = login_attempt_limiter.consume(client)
    if retry_after:
        return PlainTextResponse(
            "Trop de tentatives de connexion. Réessayez plus tard.",
            status_code=429,
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )
    ok = bool(settings.admin_password) and hmac.compare_digest(username.encode(), settings.admin_user.encode()) & hmac.compare_digest(
        password.encode(), settings.admin_password.encode()
    )
    if not ok:
        time.sleep(1.5)
        msg = "Identifiants incorrects." if settings.admin_password else "ADMIN_PASSWORD n'est pas défini dans le fichier .env."
        flash(request, msg, "error")
        return back("/login")
    login_attempt_limiter.reset(client)
    request.session["user"] = username
    return back("/")


@app.post("/logout")
def logout(request: HttpRequest):
    request.session.clear()
    return back("/login")


# ------------------------------------------------------ Tableau de bord ----

@app.get("/", response_class=HTMLResponse)
def dashboard(request: HttpRequest, db: Session = Depends(get_db)):
    mailboxes.sync_settings_mailbox(db)
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
    late_cases, soon_cases = case_management.cases_due(db, within_days=30)
    queued = db.scalar(select(func.count(Request.id)).where(Request.status == QUEUED)) or 0
    eta_hours = round(queued / settings.send_per_hour, 1) if queued else 0
    return render(
        request, "dashboard.html", db,
        per_profile=per_profile, total=service.stats(db), events=events, upcoming=upcoming,
        late_cases=late_cases, soon_cases=soon_cases[:5],
        queued=queued, eta_hours=eta_hours,
        brokers_enabled=db.scalar(select(func.count(Broker.id)).where(Broker.enabled.is_(True))),
        mailboxes_total=db.scalar(select(func.count(Mailbox.id))) or 0,
        senders_total=db.scalar(select(func.count(Sender.id))) or 0,
        senders_new=db.scalar(select(func.count(Sender.id)).where(Sender.status == "new")) or 0,
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
        # SAMEORIGIN : cette page est faite pour être affichée dans le cadre de l'application (DENY partout ailleurs).
        headers={"Content-Security-Policy": mailview.CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                 "X-Frame-Options": "SAMEORIGIN"},
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
    sub = cat.split(":", 1)[1] if cat.startswith("needs:") else ""
    if sub and sub not in actions.LABELS:
        cat, sub = "needs", ""
    statuses = cats.get("needs" if sub else cat) or [x for st in cats.values() for x in st]
    if cat not in cats and cat != "ai" and not sub:
        cat = ""
    activity = func.coalesce(Request.last_reply_at, Request.sent_at, Request.created_at)
    items = db.scalars(
        select(Request).options(selectinload(Request.broker), selectinload(Request.profile), selectinload(Request.events))
        .where(Request.status.in_(statuses)).order_by(activity.desc(), Request.id.desc())
    ).all()
    insights = {x.id: insight.for_request(x) for x in items}
    # Classement actuel de la dernière réponse : précise « action demandée » et les classements d'anciennes versions.
    kinds = {x.id: actions.current_kind(x) for x in items}
    for x in items:
        insight.refine(insights[x.id], kinds[x.id])
    action_of = {x.id: actions.action_type(x, kinds[x.id]) for x in items}
    n_ai = sum(1 for v in insights.values() if v and v.by_ai)
    if cat == "ai":  # réponses classées par l'IA
        items = [x for x in items if insights[x.id] and insights[x.id].by_ai]
    counts = dict(db.execute(select(Request.status, func.count()).where(Request.status.in_([x for st in cats.values() for x in st]))
                             .group_by(Request.status)).all())
    chips = [("", "Tout", sum(counts.values()))] + [(k, label, sum(counts.get(x, 0) for x in st)) for k, label, st in TRIAGE]
    chips.append(("ai", "Classées par l'IA", n_ai))
    subchips = []
    if cat == "needs" or sub:
        n = {}
        for x in items:
            if action_of[x.id]:
                n[action_of[x.id]] = n.get(action_of[x.id], 0) + 1
        subchips = [(f"needs:{k}", label, n[k]) for k, label, _, _ in actions.ACTION_TYPES if n.get(k)]
        if sub:
            items = [x for x in items if action_of[x.id] == sub]

    r = next((x for x in items if x.id == id), None) or (items[0] if items else None)
    late_cases, soon_cases = case_management.cases_due(db)
    ctx = {"items": items, "cat": cat, "chips": chips, "subchips": subchips, "action_of": action_of,
           "action_short": actions.SHORT, "r": r, "next_url": None, "multi_profiles": len({x.profile_id for x in items}) > 1,
           "late_cases": late_cases, "soon_cases": soon_cases, "insights": insights,
           "can_summarize": settings.llm_enabled and settings.llm_mode == "chat"}
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
        ctx["why"] = next((e.summary for e in reversed(r.events) if e.kind == "status"), None)
        ctx["confirm_link"] = next((e for e in reversed(infos) if e.body and e.body.startswith("http")), None)
        ctx["earlier_link"] = service.earlier_confirm_link(r) if r.status == NEEDS_ACTION and not ctx["confirm_link"] else None
        ctx["complaint"] = letters.cnil_complaint_text(r) if r.status in (OVERDUE, "refused") else None
    ctx["device_count"] = len(device_ids.candidates(db))
    ctx["confirm_from"] = service.confirm_from_plan_for(db, r) if r and r.status in (NEEDS_ACTION, REPLIED) else None
    ctx["proposed"] = service.proposed_reply(r) if r and r.status in (NEEDS_ACTION, REPLIED) else None
    ctx["reanalysis_count"] = len(reanalysis.load(db))
    ctx["reanalysis_suggest"] = reanalysis.model_changed(db) and bool(items)
    ctx["reanalysis_running"] = reanalysis.state["running"]
    ctx["rp"] = reanalysis.progress(with_models=False)
    return render(request, "tasks.html", db, **ctx)


@app.get("/tasks/export")
def tasks_export(request: HttpRequest, db: Session = Depends(get_db)):
    """Fichier JSON des réponses (à traiter, terminées…) pour chercher de nouvelles règles de classement."""
    parts = {p for p in request.query_params.getlist("parts") if p in (*task_export.SECTIONS, "examples")}
    data = task_export.build(db, anonymize=request.query_params.get("brut") != "1", sections=parts or None)
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    name = f"oubliez-moi-analyse-{datetime.now():%Y%m%d-%H%M}.json"
    return StreamingResponse(iter([payload]), media_type="application/json; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.post("/requests/{rid}/send-proposed")
def request_send_proposed(request: HttpRequest, rid: int, next: str = Form(""), db: Session = Depends(get_db)):
    """Envoie la réponse type proposée (pièce d'identité, pays, intérêt légitime, question simple)."""
    r = _get(db, Request, rid)
    p = service.proposed_reply(r)
    if p is None or not p["to"]:
        flash(request, "Aucune réponse type à envoyer pour ce message (ou elle est déjà partie).", "error")
    elif service._send_auto_reply(db, r, p["kind"], p["mail"]):
        service.set_status(db, r, SENT, f"Réponse type envoyée ({p['label']})")
        flash(request, f"Réponse envoyée à {p['to']}.")
    else:
        flash(request, f"Échec de l'envoi : {r.events[-1].summary if r.events else ''}", "error")
    db.commit()
    return back(_safe_next(next) or f"/requests/{rid}")


@app.post("/requests/{rid}/confirm-from")
def request_confirm_from(request: HttpRequest, rid: int, next: str = Form(""), db: Session = Depends(get_db)):
    """Envoie, depuis l'autre adresse du profil, la confirmation demandée par le courtier."""
    r = _get(db, Request, rid)
    plan = service.confirm_from_plan_for(db, r)
    if plan is None:
        flash(request, "Cette réponse ne demande pas de confirmation depuis une autre adresse.", "error")
    else:
        ok, err = service.send_confirm_from(db, r, plan)
        if ok:
            service.set_status(db, r, SENT, f"Confirmation envoyée depuis {plan['address']}")
            flash(request, f"Confirmation envoyée depuis {plan['address']} à {plan['to']}.")
        else:
            flash(request, f"Envoi impossible : {err}", "error")
        db.commit()
    return back(_safe_next(next) or f"/requests/{rid}")


@app.post("/requests/{rid}/summarize")
def request_summarize(request: HttpRequest, rid: int, next: str = Form(""), db: Session = Depends(get_db)):
    """Résumé et conseil de l'IA pour la dernière réponse, sans changer le statut de la demande."""
    r = _get(db, Request, rid)
    replies = [e for e in r.events if e.kind == "reply"]  # (« next » est ici le paramètre du formulaire)
    reply = replies[-1] if replies else None
    if reply is None or not (settings.llm_enabled and settings.llm_mode == "chat"):
        flash(request, "Il faut une réponse et un modèle conversationnel (Qwen…) dans Réglages › Automatismes.", "error")
        return back(_safe_next(next) or f"/requests/{rid}")
    headers, text = mailview.split_headers(reply.body or "")
    res = llm.classify(headers.get("Objet", ""), mailview.strip_quoted(text))
    if not res or not res.get("summary"):
        flash(request, f"Le modèle {settings.llm_model} n'a pas répondu : vérifiez Ollama dans Réglages › Automatismes.", "error")
    else:
        note = f"Analyse IA : {res['summary']}" + (f" (dossier n° {res['ticket']})" if res.get("ticket") else "")
        add_event(db, r, "info", note[:500])
        if res.get("action"):
            add_event(db, r, "info", f"Conseil IA : {res['action']}"[:500])
        db.commit()
    return back(_safe_next(next) or f"/requests/{rid}")


# ------------------------------------------------------------- Réanalyse ----

@app.get("/tasks/reanalysis", response_class=HTMLResponse)
def reanalysis_page(request: HttpRequest, db: Session = Depends(get_db)):
    return render(request, "reanalysis.html", db, proposals=reanalysis.load(db), rp=reanalysis.progress(),
                  model=reanalysis.current_model(), status_label=reanalysis.status_label)


@app.get("/tasks/reanalysis/status")
def reanalysis_status():
    return JSONResponse(reanalysis.progress())


@app.post("/tasks/reanalysis/start")
def reanalysis_start(request: HttpRequest, rid: int | None = Form(None)):
    if reanalysis.start([rid] if rid else None):
        flash(request, "Réanalyse lancée : les propositions s'affichent dès qu'elle est terminée. Rien n'est modifié sans votre accord.")
    else:
        flash(request, "Une réanalyse est déjà en cours.", "info")
    return back("/tasks/reanalysis")


@app.post("/tasks/reanalysis/apply")
async def reanalysis_apply(request: HttpRequest, db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids") if str(v).isdigit()]
    if not ids:
        flash(request, "Cochez au moins une proposition.", "error")
        return back("/tasks/reanalysis")
    applied, skipped = reanalysis.apply(db, ids)
    msg = f"{applied} correction(s) appliquée(s)."
    if skipped:
        msg += f" {skipped} ignorée(s) : la demande a changé depuis l'analyse."
    flash(request, msg)
    return back("/tasks/reanalysis")


@app.post("/tasks/reanalysis/dismiss")
def reanalysis_dismiss(request: HttpRequest, db: Session = Depends(get_db)):
    reanalysis.dismiss(db)
    flash(request, "Propositions écartées : rien n'a été modifié.")
    return back("/tasks")


def _snippet(r: Request) -> str:
    if r.status == FAILED:
        return r.last_error or "Échec d'envoi"
    if r.status == OVERDUE:
        return f"Aucune réponse, échéance dépassée le {fmt_d(r.due_at)}"
    if r.channel == "webform" and not r.sent_at:
        return "Formulaire en ligne à remplir"
    replies = [e for e in r.events if e.kind == "reply"]
    if replies:
        return _first_words(replies[-1]) or replies[-1].summary.split(" : ", 1)[-1]
    return STATUS_LABELS.get(r.status, r.status)


_GREETING = re.compile(r"^\s*(hi|hello|hey|dear|bonjour|bonsoir|madame|monsieur|hallo|guten tag|sehr geehrte)\b[^\n]{0,60}$", re.I)


_QUOTE_INTRO = re.compile(r"^\s*(on|le|am)\b.{0,120}\b(wrote|a écrit|schrieb)\s*:\s*$", re.I)


def _first_words(ev: Event, limit: int = 140) -> str:
    """Début utile d'une réponse (sans en-têtes, citation ni formule d'appel) pour la liste « À traiter »."""
    text = mailview.strip_quoted(mailview.split_headers(ev.body or "")[1])
    lines = []
    for ln in text.splitlines():
        if ln.startswith(">") or _QUOTE_INTRO.match(ln):
            break  # début du message cité
        if ln.strip() and not _GREETING.match(ln):
            lines.append(ln.strip())
    out = " ".join(lines)
    return out[:limit].rstrip() + ("…" if len(out) > limit else "")


templates.env.globals["snippet"] = _snippet


# ------------------------------------------------------------ Boîtes mail ----
# Analyse des expéditeurs en masse, indépendante des demandes RGPD aux courtiers.

@app.get("/mailboxes", response_class=HTMLResponse)
def mailboxes_page(request: HttpRequest, status: str = "new", mailbox: int | None = None, q: str = "",
                   db: Session = Depends(get_db)):
    for j in mailboxes.jobs.values():  # résultat des travaux terminés
        if j["finished"] and not j.get("seen"):
            j["seen"] = True
            if j["action"] == "unsubscribe":
                msg = f"Désinscription terminée : {j['ok']} réussie(s)"
                if j["manual"]:
                    msg += f", {j['manual']} à finir vous-même (lien ou e-mail, onglet « Désinscription à finir »)"
                if j["failed"]:
                    msg += f", {j['failed']} échec(s)"
                flash(request, msg + ".", "ok" if not j["failed"] else "info")
            else:
                flash(request, f"Rangement terminé : {j['ok']} e-mail(s) déplacé(s)." if not j["error"] else f"Rangement impossible : {j['error']}",
                      "ok" if not j["error"] else "error")
    mailboxes.sync_settings_mailbox(db)
    boxes = db.scalars(select(Mailbox).order_by(Mailbox.linked_settings.desc(), Mailbox.id)).all()
    settings_box = mailboxes.settings_address()
    conds = [Sender.mailbox_id == mailbox] if mailbox else []
    counts = dict(db.execute(select(Sender.status, func.count()).where(*conds).group_by(Sender.status)).all())
    stmt = select(Sender).where(*conds).options(selectinload(Sender.mailbox))
    if status in SENDER_STATUSES:
        stmt = stmt.where(Sender.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Sender.address.ilike(like), Sender.name.ilike(like), Sender.domain.ilike(like)))
    senders = db.scalars(stmt.order_by(Sender.count.desc(), Sender.last_seen.desc()).limit(400)).all()
    per_box = dict(db.execute(select(Sender.mailbox_id, func.count()).where(Sender.status == "new").group_by(Sender.mailbox_id)).all())
    return render(request, "mailboxes.html", db, boxes=boxes, senders=senders, status=status, mailbox=mailbox, q=q,
                  counts=counts, total=sum(counts.values()), per_box=per_box, SSTATUS=SENDER_STATUSES,
                  brokers_by_domain=mailboxes.catalog_domains(db), jobs=mailboxes.jobs, settings_box=settings_box,
                  settings_box_hidden=bool(settings_box) and all(b.address.lower() != settings_box for b in boxes))


@app.get("/mailboxes/new", response_class=HTMLResponse)
def mailbox_new(request: HttpRequest, db: Session = Depends(get_db)):
    return render(request, "mailbox_form.html", db, mb=None, PROVIDERS=mailboxes.PROVIDERS,
                  ms_default=bool(oauth_ms.default_client_id()))


@app.get("/mailboxes/{mid}/edit", response_class=HTMLResponse)
def mailbox_edit(request: HttpRequest, mid: int, db: Session = Depends(get_db)):
    return render(request, "mailbox_form.html", db, mb=_get(db, Mailbox, mid), PROVIDERS=mailboxes.PROVIDERS,
                  ms_default=bool(oauth_ms.default_client_id()))


def _fill_analysis_options(mb: Mailbox, form: dict, default_folders: list[str]) -> None:
    mb.folders = [f.strip() for f in (form.get("folders") or "").split(",") if f.strip()] or list(default_folders)
    mb.file_folder = (form.get("file_folder") or "Newsletters").strip() or "Newsletters"
    months = (form.get("months") or "12").strip()
    mb.months = min(120, max(1, int(months))) if months.isdigit() else 12
    mb.auto_scan = bool(form.get("auto_scan"))


def _fill_mailbox(mb: Mailbox, form: dict) -> list[str]:
    if mb.linked_settings:  # la connexion se règle dans Réglages : seules les options d'analyse sont modifiables
        mb.label = (form.get("label") or "").strip() or "Boîte des demandes"
        _fill_analysis_options(mb, form, mb.folders or ["INBOX"])
        return []
    errors = []
    provider = form.get("provider", "imap")
    preset = mailboxes.PROVIDERS.get(provider, mailboxes.PROVIDERS["imap"])
    mb.provider = provider if provider in mailboxes.PROVIDERS else "imap"
    mb.label = (form.get("label") or "").strip() or preset["label"]
    mb.address = (form.get("address") or "").strip().lower()
    mb.host = (form.get("host") or preset["host"]).strip()
    port = (form.get("port") or str(preset["port"])).strip()
    mb.port = int(port) if port.isdigit() else preset["port"]
    mb.security = form.get("security") if form.get("security") in ("ssl", "starttls", "none") else preset["security"]
    mb.verify_tls = bool(form.get("verify_tls"))
    mb.auth = preset["auth"]
    mb.username = (form.get("username") or "").strip()
    if form.get("password"):
        mb.secret_enc = crypto.encrypt(form["password"])
    mb.oauth_client_id = (form.get("oauth_client_id") or "").strip() or None
    mb.oauth_tenant = (form.get("oauth_tenant") or "consumers").strip() or "consumers"
    _fill_analysis_options(mb, form, preset["folders"])
    if "@" not in mb.address:
        errors.append("Indiquez l'adresse e-mail de la boîte")
    if not mb.host:
        errors.append("Indiquez le serveur IMAP")
    if mb.auth == "oauth_ms" and not oauth_ms.client_id_for(mb.oauth_client_id):
        errors.append("Indiquez l'identifiant d'application Microsoft (client ID)")
    return errors


@app.post("/mailboxes")
async def mailbox_create(request: HttpRequest, db: Session = Depends(get_db)):
    form = dict(await request.form())
    mb = Mailbox(state={})
    errors = _fill_mailbox(mb, form)
    if errors:
        for e in errors:
            flash(request, e, "error")
        return back("/mailboxes/new")
    db.add(mb)
    db.commit()
    if mb.auth == "oauth_ms":
        flash(request, "Boîte ajoutée. Il reste à la connecter à votre compte Microsoft (bouton ci-dessous).")
        return back(f"/mailboxes/{mb.id}/edit#oauth")
    flash(request, f"Boîte « {mb.label} » ajoutée : première analyse lancée.")
    mailboxes.scan_in_background(mb.id)
    return back(f"/mailboxes?mailbox={mb.id}")


@app.post("/mailboxes/{mid}")
async def mailbox_update(request: HttpRequest, mid: int, db: Session = Depends(get_db)):
    form = dict(await request.form())
    mb = _get(db, Mailbox, mid)
    old = (mb.host, mb.address, mb.username)
    errors = _fill_mailbox(mb, form)
    if errors:
        db.rollback()
        for e in errors:
            flash(request, e, "error")
        return back(f"/mailboxes/{mid}/edit")
    if old != (mb.host, mb.address, mb.username):
        mb.state = {}  # autre compte : on repart de zéro
        oauth_ms.forget(mb.id)
    db.commit()
    flash(request, "Boîte enregistrée.")
    return back(f"/mailboxes/{mid}/edit")


@app.post("/mailboxes/{mid}/delete")
def mailbox_delete(request: HttpRequest, mid: int, db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    oauth_ms.forget(mb.id)
    if mb.linked_settings:
        mailboxes.dismiss_settings_mailbox(db, mb)  # sinon elle réapparaîtrait au prochain affichage
    db.delete(mb)
    db.commit()
    if mb.linked_settings:
        flash(request, "Boîte des demandes retirée de cette page. Elle reste utilisée pour relever les réponses des courtiers.")
    else:
        flash(request, "Boîte retirée d'Oubliez-moi (rien n'a été modifié dans la boîte elle-même).")
    return back("/mailboxes")


@app.post("/mailboxes/settings-account/restore")
def mailbox_settings_restore(request: HttpRequest, db: Session = Depends(get_db)):
    mb = mailboxes.restore_settings_mailbox(db)
    if mb:
        flash(request, f"« {mb.address} » est de nouveau dans la liste. Lancez l'analyse quand vous voulez.")
    return back(f"/mailboxes?mailbox={mb.id}" if mb else "/mailboxes")


@app.post("/mailboxes/{mid}/test")
def mailbox_test(request: HttpRequest, mid: int, db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    try:
        flash(request, mailboxes.test_connection(mb, db))
    except mailboxes.MailboxError as e:
        flash(request, str(e), "error")
    return back(f"/mailboxes/{mid}/edit")


@app.post("/mailboxes/{mid}/scan")
def mailbox_scan(request: HttpRequest, mid: int, db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    if mb.scanning:
        flash(request, "Une analyse est déjà en cours.", "info")
    else:
        if mb.linked_settings and mb.last_scan_at is None:
            mb.auto_scan = True  # comme une boîte ajoutée à la main : première analyse, puis chaque nuit
            db.commit()
        mailboxes.scan_in_background(mid)
        flash(request, f"Analyse de « {mb.label} » lancée. Les expéditeurs apparaissent au fur et à mesure.")
    return back(f"/mailboxes?mailbox={mid}")


@app.get("/mailboxes/status")
def mailboxes_status(db: Session = Depends(get_db)):
    boxes = db.scalars(select(Mailbox)).all()
    return JSONResponse({
        "scanning": [{"id": b.id, "label": b.label, "progress": b.progress} for b in boxes if b.scanning],
        "jobs": {k: v for k, v in mailboxes.jobs.items() if not v.get("seen")},
    })


@app.post("/mailboxes/{mid}/oauth/start")
def mailbox_oauth_start(mid: int, db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    try:
        flow = oauth_ms.start(mb.id, oauth_ms.client_id_for(mb.oauth_client_id), mb.oauth_tenant)
    except oauth_ms.OAuthError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"user_code": flow["user_code"], "uri": flow["uri"], "interval": flow["interval"]})


@app.post("/mailboxes/{mid}/oauth/poll")
def mailbox_oauth_poll(request: HttpRequest, mid: int, db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    try:
        state, refresh = oauth_ms.poll(mb.id)
    except oauth_ms.OAuthError as e:
        return JSONResponse({"state": f"error:{e}"})
    if state == "ok" and refresh:
        mb.secret_enc = crypto.encrypt(refresh)
        mb.last_error = None
        db.commit()
        flash(request, "Compte Microsoft connecté : première analyse lancée.")
        mailboxes.scan_in_background(mb.id)
    return JSONResponse({"state": state})


@app.post("/senders/action")
async def senders_action(request: HttpRequest, db: Session = Depends(get_db)):
    form = await request.form()
    action = form.get("action", "")
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    nxt = _safe_next(form.get("next")) or "/mailboxes"
    senders = db.scalars(select(Sender).where(Sender.id.in_(ids))).all() if ids else []
    if not senders:
        flash(request, "Sélectionnez au moins un expéditeur.", "info")
        return back(nxt)
    if action in ("keep", "reset", "done"):
        new = {"keep": "kept", "reset": "new", "done": "unsubscribed"}[action]
        for s in senders:
            s.status, s.status_at = new, utcnow()
            if action == "done":
                s.detail = "désinscription faite à la main"
        db.commit()
        flash(request, f"{len(senders)} expéditeur(s) : {SENDER_STATUSES[new].lower()}.")
    elif action in ("unsubscribe", "file"):
        groups: dict[int, list[int]] = {}
        for s in senders:
            groups.setdefault(s.mailbox_id, []).append(s.id)
        name = f"{action}-{int(utcnow().timestamp())}"
        mailboxes.run_job(name, groups, action)
        verb = "Désinscription" if action == "unsubscribe" else "Rangement des e-mails"
        flash(request, f"{verb} en cours pour {len(senders)} expéditeur(s)…", "info")
    return back(nxt)


def _csv_safe(value) -> str:
    """Neutralise les cellules susceptibles d'être interprétées comme une formule par un tableur."""
    text = "" if value is None else str(value)
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


@app.get("/export.csv")
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
        # Ne jamais supprimer physiquement un courtier : la relation ORM utilise
        # delete-orphan et effacerait aussi les demandes, réponses et preuves associées.
        b.enabled = False
        b.enabled_overridden = True
        db.commit()
        flash(request, "Courtier archivé : il ne sera plus inclus dans les campagnes, et son historique est conservé.")
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

@app.get("/settings/energy", response_class=HTMLResponse)
def energy_page(request: HttpRequest, db: Session = Depends(get_db)):
    checks = energy.checks(db)
    return render(request, "energy.html", db, checks=checks,
                  n_warn=sum(1 for c in checks if c.status == "warn"), n_ok=sum(1 for c in checks if c.status == "ok"))


@app.post("/settings/energy/test-llm")
def energy_test_llm(request: HttpRequest):
    err = energy.warm_up_model()
    flash(request, f"Test impossible : {err}" if err else "Modèle chargé : voici où il tourne.", "error" if err else "ok")
    return back("/settings/energy")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: HttpRequest, db: Session = Depends(get_db)):
    return render(
        request, "settings.html", db,
        imap_polled_at=kv_get(db, "imap_polled_at"), synced_at=kv_get(db, "brokers_synced_at"),
        overridden=settings_store.overridden_keys(), file_status=(kv_get(db, "imap_file_status") or "").split("|", 2),
        llm_models=llm.MODELS, llm_installed=llm.installed_models() if settings.llm_url else None,
        llm_hint=llm.unreachable_hint(settings.llm_url) if settings.llm_url else "",
        llm_details=llm.installed_details() if settings.llm_url else None,
        backups=backup.list_backups(), backup_error=_backup_error(db),
        key_source=crypto.key_source, crypto_errors=crypto.decrypt_errors,
        llm_in_catalog=bool(llm.catalog_kind(settings.llm_model)),
    )


TABS = ("general", "compte", "releve", "automatismes", "notifications", "securite", "auto")


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
    model_before = reanalysis.current_model()
    if _save_settings(request, form):
        flash(request, "Réglages enregistrés.")
        if reanalysis.current_model() and reanalysis.current_model() != model_before:
            flash(request, f"Nouveau modèle d'IA ({settings.llm_model}) : vous pouvez réanalyser les réponses en attente "
                           "depuis « À traiter ».", "info")
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


@app.post("/settings/detect-authserv")
async def detect_authserv(request: HttpRequest):
    """Repère l'identifiant (authserv-id) du serveur de réception et le propose/enregistre."""
    form = dict(await request.form())
    if not _save_settings(request, form):
        return back(_tab_url(form))
    if not settings.imap_configured:
        flash(request, "Renseignez d'abord le serveur de relève.", "error")
        return back(_tab_url(form))
    try:
        res = mailer.detect_authserv_ids()
    except Exception as e:  # noqa: BLE001
        flash(request, f"Erreur IMAP : {e}", "error")
        return back(_tab_url(form))
    flash(request, *_authserv_feedback(res))
    return back(_tab_url(form))


def _authserv_feedback(res: dict) -> tuple[str, str]:
    """Applique le résultat de la détection et renvoie (message, type de bandeau)."""
    scanned, best, current = res["scanned"], res["suggestion"], settings.imap_authserv_id
    if not scanned:
        return "La boîte de réception est vide : impossible de détecter l'identifiant pour l'instant.", "warn"
    if best:
        count = dict(res["ids"])[best]
        share = f"présent dans {count} des {scanned} derniers messages"
        if not current:
            settings_store.set_value("imap_authserv_id", best)
            return (f"Identifiant détecté et enregistré : {best} ({share}). Les réponses authentifiées des courtiers "
                    "peuvent désormais clôturer leurs demandes."), "ok"
        if current == best:
            return f"Le réglage « {current} » correspond bien à votre serveur ({share}).", "ok"
        return (f"Votre serveur semble être « {best} » ({share}), mais le réglage vaut « {current} ». "
                "Videz le champ et relancez la détection pour le remplacer."), "error"
    if res["without_id"] >= 0.6 * scanned:
        return ("Ce fournisseur (Outlook, Hotmail…) n'indique pas d'identifiant dans ses résultats d'authentification : "
                "la clôture automatique n'est pas possible avec cette boîte. Les réponses restent relevées et classées, "
                "et les décisions sensibles vous sont laissées."), "warn"
    seen = ", ".join(f"{i} ({n})" for i, n in res["ids"]) or "aucun"
    return f"Aucun identifiant ne domine parmi les {scanned} derniers messages. Vus : {seen}.", "warn"


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


def _backup_error(db) -> str | None:
    status = (kv_get(db, "backup_status") or "").split("|", 2)
    if len(status) == 3 and status[1] == "error":
        return f"Dernière sauvegarde automatique en échec ({fmt_dt(datetime.fromisoformat(status[0]))}) : {status[2]}"
    if settings.backup_schedule != "off" and not settings.backup_dir.is_dir():
        return f"Le dossier {settings.backup_dir} n'existe pas dans le conteneur : vérifiez BACKUP_PATH dans .env et docker-compose.yml."
    return None


@app.post("/settings/backup-now")
async def backup_now(request: HttpRequest, db: Session = Depends(get_db)):
    form = dict(await request.form())
    if not _save_settings(request, form):
        return back(_tab_url(form))
    try:
        path = backup.create(manual=True)
        kv_set(db, "backup_status", f"{utcnow().isoformat()}|ok|{path.name}")
        db.commit()
        flash(request, f"Réglages enregistrés. Sauvegarde créée : {path.name}")
    except (backup.BackupError, OSError) as e:
        flash(request, f"Sauvegarde impossible : {e}", "error")
    return back(_tab_url(form))


@app.get("/settings/encryption-key")
def encryption_key():
    return JSONResponse({"key": crypto.export_key()}, headers={"Cache-Control": "no-store"})


@app.post("/settings/llm-delete")
async def llm_delete(request: HttpRequest):
    """Supprime un modèle d'Ollama pour libérer de la place."""
    form = dict(await request.form())
    model = form.get("model", "")
    try:
        llm.delete_model(model)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    flash(request, f"Modèle « {model} » supprimé." + (
        " C'était le modèle sélectionné : choisissez-en un autre, ou téléchargez-le à nouveau." if llm.normalize(model) == llm.normalize(settings.llm_model) else ""))
    return JSONResponse({"ok": True})


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
