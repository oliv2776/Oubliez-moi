"""Application web Oubliez-moi."""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from urllib.parse import urlsplit
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import brokers as broker_catalog, crypto, mailboxes, scheduler, service, settings_store
from . import case_assistant, case_management, device_ids, model_compare, exposures, migration_routes, search as search_routes
from .config import settings
from .routes import brokers as _r_brokers, mailboxes as _r_mailboxes, profiles as _r_profiles, requests as _r_requests, settings as _r_settings, tasks as _r_tasks
from .web import BASE, SEED, back, flash, get_db, log, render
from .login_security import login_attempt_limiter
from .db import (
    QUEUED,
    Broker,
    Event,
    Mailbox,
    Profile,
    Sender,
    Request,
    SessionLocal,
    init_db,
    kv_get,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("uvicorn.access").addFilter(lambda r: "/healthz" not in r.getMessage())




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
for _module in (search_routes, case_management, case_assistant, exposures, migration_routes, device_ids, model_compare,
                _r_profiles, _r_requests, _r_tasks, _r_mailboxes, _r_brokers, _r_settings):
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


CSP = ("default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "frame-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Applique des en-têtes défensifs et interdit la mise en cache des pages privées."""
    async def dispatch(self, request, call_next):
        # Scripts : uniquement ceux de l'appli, marqués d'un jeton renouvelé à chaque page. Un script injecté
        # (dans un nom de courtier, un objet d'e-mail…) ne s'exécute pas, même si un échappement manquait.
        request.state.csp_nonce = secrets.token_urlsafe(16)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP.format(nonce=request.state.csp_nonce))
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
