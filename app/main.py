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
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, brokers as broker_catalog, letters, mailer, scheduler, service, settings_store
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


def _fill_profile(p: Profile, full_name, other_names, emails, phones, addresses, birth_date, language, notes):
    p.full_name = full_name.strip()
    p.other_names = _split(other_names)
    p.emails = [e.lower() for e in _split(emails)]
    p.phones = _split(phones)
    # Adresses postales : séparées par une ligne vide
    p.addresses = [" ".join(_split(block)) for block in re.split(r"\n\s*\n", (addresses or "").replace("\r", "")) if block.strip()]
    p.birth_date = birth_date.strip() or None
    p.language = language if language in ("auto", "fr", "en") else "auto"
    p.notes = notes.strip() or None


@app.post("/profiles")
def profile_create(
    request: HttpRequest, db: Session = Depends(get_db),
    full_name: str = Form(...), other_names: str = Form(""), emails: str = Form(""), phones: str = Form(""),
    addresses: str = Form(""), birth_date: str = Form(""), language: str = Form("auto"), notes: str = Form(""),
):
    p = Profile()
    _fill_profile(p, full_name, other_names, emails, phones, addresses, birth_date, language, notes)
    if not p.emails:
        flash(request, "Au moins une adresse e-mail est nécessaire pour que les courtiers vous identifient.", "error")
        return back("/profiles/new")
    db.add(p)
    db.commit()
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
    return render(request, "profile_detail.html", db, p=p, reqs=reqs, st=service.stats(db, pid), sample_broker=sample_broker, pending_new=pending_new)


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
    _fill_profile(p, full_name, other_names, emails, phones, addresses, birth_date, language, notes)
    db.commit()
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
    return render(request, "request_detail.html", db, r=r, complaint=complaint)


@app.post("/requests/{rid}/status")
def request_status(request: HttpRequest, rid: int, status: str = Form(...), note: str = Form(""), db: Session = Depends(get_db)):
    r = _get(db, Request, rid)
    if status not in STATUS_LABELS:
        raise HTTPException(400)
    service.set_status(db, r, status, f"Statut changé manuellement : {STATUS_LABELS[status]}" + (f" — {note}" if note.strip() else ""))
    db.commit()
    flash(request, f"Statut mis à jour : {STATUS_LABELS[status]}.")
    return back(request.headers.get("referer") or f"/requests/{rid}")


@app.post("/requests/{rid}/note")
def request_note(request: HttpRequest, rid: int, note: str = Form(...), db: Session = Depends(get_db)):
    r = _get(db, Request, rid)
    if note.strip():
        add_event(db, r, "note", note.strip()[:500], note.strip() if len(note) > 500 else None)
        db.commit()
    return back(f"/requests/{rid}")


@app.post("/requests/{rid}/send-reminder")
def request_reminder(request: HttpRequest, rid: int, db: Session = Depends(get_db)):
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
    return back(f"/requests/{rid}")


@app.post("/requests/requeue-failed")
def requeue_failed(request: HttpRequest, db: Session = Depends(get_db)):
    n = 0
    for r in db.scalars(select(Request).join(Broker).where(Request.status == FAILED, Broker.email.is_not(None))):
        service.set_status(db, r, QUEUED, "Remise en file d'attente")
        n += 1
    db.commit()
    flash(request, f"{n} demande(s) remise(s) en file d'attente.")
    return back("/requests?status=failed")


@app.get("/tasks", response_class=HTMLResponse)
def tasks(request: HttpRequest, db: Session = Depends(get_db)):
    def q(*statuses):
        return db.scalars(
            select(Request).options(selectinload(Request.broker), selectinload(Request.profile))
            .where(Request.status.in_(statuses)).order_by(Request.id)
        ).all()
    return render(request, "tasks.html", db, replied=q(REPLIED), needs=q(NEEDS_ACTION), overdue=q(OVERDUE, "refused"), failed=q(FAILED))


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
        overridden=settings_store.overridden_keys(),
    )


@app.post("/settings")
async def settings_save(request: HttpRequest):
    form = dict(await request.form())
    was_dry = settings.dry_run
    errors = settings_store.save_from_form(form)
    if errors:
        for e in errors:
            flash(request, e, "error")
        return back("/settings")
    scheduler.reschedule()
    msg = "Réglages enregistrés."
    if was_dry and not settings.dry_run:
        n = service.requeue_simulated()
        msg += " Envoi réel activé : les demandes en file vont maintenant partir"
        msg += f" (dont {n} demande(s) simulée(s) remise(s) en file)." if n else "."
    flash(request, msg)
    return back("/settings")


@app.post("/settings/clear-secret")
def settings_clear_secret(request: HttpRequest, key: str = Form(...)):
    settings_store.clear_secret(key)
    flash(request, "Mot de passe effacé.")
    return back("/settings")


@app.post("/settings/reset")
def settings_reset(request: HttpRequest):
    settings_store.reset_to_env()
    scheduler.reschedule()
    flash(request, "Réglages de l'interface effacés : les valeurs du fichier .env s'appliquent de nouveau.")
    return back("/settings")


@app.post("/settings/test-smtp")
def test_smtp(request: HttpRequest):
    if not settings.smtp_configured:
        flash(request, "Renseignez d'abord le serveur SMTP et l'adresse d'expédition, puis enregistrez.", "error")
        return back("/settings")
    msg = mailer.build_message(settings.smtp_from, "Oubliez-moi – e-mail de test", "Si vous lisez ceci, l'envoi SMTP fonctionne.", "Oubliez-moi")
    try:
        mailer.send_message(msg, force=True)  # le test part réellement, même en mode simulation
        flash(request, f"E-mail de test envoyé à {settings.smtp_from} : vérifiez votre boîte.")
    except mailer.SendError as e:
        flash(request, str(e), "error")
    return back("/settings")


@app.post("/settings/test-imap")
def test_imap(request: HttpRequest):
    if not settings.imap_configured:
        flash(request, "IMAP non configuré.", "error")
    else:
        try:
            flash(request, mailer.test_imap())
        except Exception as e:  # noqa: BLE001
            flash(request, f"Erreur IMAP : {e}", "error")
    return back("/settings")


@app.post("/settings/poll")
def poll_now(request: HttpRequest):
    try:
        n = service.poll_imap()
        flash(request, f"Relève terminée : {n} réponse(s) rattachée(s).")
    except Exception as e:  # noqa: BLE001
        flash(request, f"Erreur IMAP : {e}", "error")
    return back(request.headers.get("referer") or "/settings")


@app.post("/settings/send-now")
def send_now(request: HttpRequest):
    res = service.send_tick()
    flash(request, f"Envoi : {res}" if res else "Rien à envoyer pour le moment.", "info")
    return back(request.headers.get("referer") or "/")
