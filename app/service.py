"""Logique métier : campagnes, envoi, relances, réponses, mise à jour des courtiers."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from . import brokers as broker_catalog
from . import letters, mailer
from .config import settings
from .db import (
    COMPLETED,
    FAILED,
    FINAL_STATUSES,
    NEEDS_ACTION,
    OVERDUE,
    QUEUED,
    REFUSED,
    REPLIED,
    SENT,
    STATUS_LABELS,
    Broker,
    Profile,
    Request,
    SessionLocal,
    add_event,
    kv_get,
    kv_set,
    new_reference,
    utcnow,
)

log = logging.getLogger(__name__)
_send_lock = threading.Lock()
_imap_lock = threading.Lock()
MAX_ATTEMPTS = 3


# ------------------------------------------------------------ Courtiers ----

def sync_brokers(db: Session, records: list[dict]) -> dict:
    """Insère / met à jour les courtiers d'une source, sans écraser les choix de l'utilisateur."""
    countries = set(settings.broker_countries)
    existing = {b.slug: b for b in db.scalars(select(Broker))}
    added = updated = 0
    for rec in records:
        b = existing.get(rec["slug"])
        if b is None:
            b = Broker(slug=rec["slug"], enabled=broker_catalog.is_default_enabled(rec, countries))
            db.add(b)
            added += 1
        elif b.source == "custom":
            continue
        else:
            updated += 1
            if not b.enabled_overridden:
                b.enabled = broker_catalog.is_default_enabled(rec, countries)
        for key in ("name", "email", "webform", "web", "postal_address", "countries", "categories", "needs_id", "quality", "source"):
            setattr(b, key, rec.get(key))
    kv_set(db, "brokers_synced_at", utcnow().isoformat())
    db.commit()
    return {"added": added, "updated": updated, "total": len(records)}


def update_brokers_from_remote() -> dict:
    records = broker_catalog.fetch_remote()
    if len(records) < 50:  # garde-fou si la source change de format
        raise RuntimeError(f"Seulement {len(records)} courtiers reçus, mise à jour ignorée")
    with SessionLocal() as db:
        result = sync_brokers(db, records)
    if settings.auto_enroll_new_brokers:
        enroll_new_brokers()
    return result


def apply_country_defaults(db: Session) -> int:
    """Réapplique BROKER_COUNTRIES aux courtiers non modifiés à la main."""
    countries = set(settings.broker_countries)
    n = 0
    for b in db.scalars(select(Broker).where(Broker.enabled_overridden.is_(False), Broker.source != "custom")):
        want = bool(set(b.countries or ["all"]) & countries)
        if b.enabled != want:
            b.enabled = want
            n += 1
    db.commit()
    return n


# ------------------------------------------------------------ Campagnes ----

def _create_request(db: Session, profile: Profile, broker: Broker, cycle: int) -> Request:
    lang = letters.letter_language(profile, broker)
    req = Request(
        profile=profile,
        broker=broker,
        reference=new_reference(db),
        cycle=cycle,
        channel=broker.channel,
        language=lang,
        status=QUEUED if broker.email else NEEDS_ACTION,
    )
    db.add(req)
    if broker.email:
        add_event(db, req, "info", f"Demande créée (cycle {cycle}) et placée en file d'attente")
    else:
        add_event(db, req, "info", f"Ce courtier n'accepte que son formulaire en ligne : {broker.webform}")
    return req


def start_campaign(db: Session, profile: Profile) -> int:
    """Crée une demande pour chaque courtier activé qui n'en a pas encore pour ce profil."""
    already = set(db.scalars(select(Request.broker_id).where(Request.profile_id == profile.id)))
    n = 0
    for broker in db.scalars(select(Broker).where(Broker.enabled.is_(True)).order_by(Broker.name)):
        if broker.id not in already:
            _create_request(db, profile, broker, cycle=1)
            n += 1
    if profile.campaign_started_at is None:
        profile.campaign_started_at = utcnow()
    db.commit()
    log.info("Campagne %s : %d demandes créées", profile.full_name, n)
    return n


def enroll_new_brokers() -> int:
    with SessionLocal() as db:
        total = 0
        for p in db.scalars(select(Profile).where(Profile.active.is_(True), Profile.campaign_started_at.is_not(None))):
            total += start_campaign(db, p)
        return total


def schedule_resends() -> int:
    """Renvoie périodiquement les demandes terminées : les courtiers recollectent les données."""
    if settings.resend_interval_days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=settings.resend_interval_days)
    n = 0
    with SessionLocal() as db:
        latest = (
            select(Request.profile_id, Request.broker_id, func.max(Request.cycle).label("cycle"))
            .group_by(Request.profile_id, Request.broker_id)
            .subquery()
        )
        rows = db.scalars(
            select(Request)
            .join(latest, (Request.profile_id == latest.c.profile_id) & (Request.broker_id == latest.c.broker_id) & (Request.cycle == latest.c.cycle))
            .where(Request.status == COMPLETED, Request.closed_at < cutoff)
        ).all()
        for old in rows:
            if old.profile.active and old.broker.enabled and old.broker.email:
                _create_request(db, old.profile, old.broker, cycle=old.cycle + 1)
                n += 1
        db.commit()
    if n:
        log.info("%d demandes périodiques recréées", n)
    return n


# ---------------------------------------------------------------- Envoi ----

def _original_body(req: Request) -> str:
    _, body = letters.erasure_letter(req.profile, req.broker, req.reference, req.language)
    return body


def _deliver(db: Session, req: Request, subject: str, body: str) -> bool:
    msg = mailer.build_message(req.broker.email, subject, body, from_name=req.profile.full_name)
    try:
        mailer.send_message(msg)
    except mailer.SendError as e:
        req.attempts += 1
        req.last_error = str(e)
        add_event(db, req, "error", f"Échec d'envoi (tentative {req.attempts}) : {e}")
        if e.permanent or req.attempts >= MAX_ATTEMPTS:
            req.status = FAILED
        db.commit()
        return False
    req.message_ids = [*(req.message_ids or []), msg["Message-ID"]]
    req.last_error = None
    return True


def send_initial(db: Session, req: Request) -> bool:
    subject, body = letters.erasure_letter(req.profile, req.broker, req.reference, req.language)
    if not _deliver(db, req, subject, body):
        return False
    now = utcnow()
    req.status, req.sent_at = SENT, now
    req.due_at = now + timedelta(days=settings.response_days)
    prefix = "[SIMULATION] " if settings.dry_run else ""
    add_event(db, req, "sent", f"{prefix}Demande envoyée à {req.broker.email}", f"Objet : {subject}\n\n{body}")
    db.commit()
    return True


def send_reminder(db: Session, req: Request) -> bool:
    subject, body = letters.reminder_letter(req, _original_body(req))
    if not _deliver(db, req, subject, body):
        return False
    now = utcnow()
    req.reminder_count += 1
    req.last_reminder_at = now
    req.due_at = now + timedelta(days=settings.reminder_wait_days)
    prefix = "[SIMULATION] " if settings.dry_run else ""
    add_event(db, req, "reminder", f"{prefix}Relance envoyée à {req.broker.email}", f"Objet : {subject}\n\n{body}")
    db.commit()
    return True


def _next_queued(db: Session) -> Request | None:
    return db.scalars(
        select(Request)
        .join(Profile).join(Broker)
        .where(Request.status == QUEUED, Profile.active.is_(True), Broker.email.is_not(None))
        .order_by(Request.attempts, Request.id)
        .limit(1)
    ).first()


def _next_reminder(db: Session) -> Request | None:
    if not settings.auto_reminder:
        return None
    return db.scalars(
        select(Request)
        .join(Profile)
        .where(Request.status == SENT, Request.reminder_count == 0, Request.due_at < utcnow(), Profile.active.is_(True))
        .order_by(Request.due_at)
        .limit(1)
    ).first()


def send_tick() -> str | None:
    """Envoie au plus un e-mail (appelé à intervalle régulier pour lisser le débit)."""
    if not settings.dry_run and not settings.smtp_configured:
        return None  # rien à faire tant que la messagerie n'est pas configurée
    if not _send_lock.acquire(blocking=False):
        return None
    try:
        with SessionLocal() as db:
            req = _next_queued(db)
            if req:
                ok = send_initial(db, req)
                return f"{'OK' if ok else 'ERREUR'} {req.reference} {req.broker.name}"
            req = _next_reminder(db)
            if req:
                ok = send_reminder(db, req)
                return f"{'OK' if ok else 'ERREUR'} relance {req.reference} {req.broker.name}"
        return None
    finally:
        _send_lock.release()


def requeue_simulated() -> int:
    """Au passage en envoi réel, remet en file les demandes « envoyées » en simulation."""
    n = 0
    with SessionLocal() as db:
        for req in db.scalars(select(Request).where(Request.status.in_([SENT, OVERDUE]))):
            sends = [e for e in req.events if e.kind in ("sent", "reminder")]
            if sends and all(e.summary.startswith("[SIMULATION]") for e in sends):
                req.status = QUEUED
                req.sent_at = req.due_at = req.last_reminder_at = None
                req.reminder_count = req.attempts = 0
                req.message_ids = []
                add_event(db, req, "info", "Fin de la simulation : demande remise en file pour un envoi réel")
                n += 1
        db.commit()
    return n


def check_deadlines() -> int:
    """Passe en « sans réponse » les demandes relancées restées muettes."""
    n = 0
    with SessionLocal() as db:
        cond = Request.reminder_count > 0 if settings.auto_reminder else Request.reminder_count >= 0
        for req in db.scalars(select(Request).where(Request.status == SENT, Request.due_at < utcnow(), cond)):
            req.status = OVERDUE
            add_event(db, req, "status", "Délai dépassé sans réponse : vous pouvez saisir la CNIL")
            n += 1
        db.commit()
    return n


# ------------------------------------------------------------- Réponses ----

def _match_request(db: Session, m: mailer.ParsedMail) -> Request | None:
    refs = mailer.find_references(m)
    if refs:
        req = db.scalars(select(Request).where(Request.reference.in_(refs)).order_by(Request.id.desc())).first()
        if req:
            return req
    for mid in m.in_reply_to:
        req = db.scalars(select(Request).where(cast(Request.message_ids, String).contains(mid, autoescape=True))).first()
        if req:
            return req
    if m.is_bounce:
        return None
    dom = mailer.registered_domain(m.from_addr.split("@")[-1])
    if not dom or dom in mailer.FREEMAIL_DOMAINS:
        return None
    candidates = db.scalars(
        select(Request).join(Broker)
        .where(Request.sent_at.is_not(None), or_(Broker.email.ilike(f"%@%{dom}"), Broker.web.ilike(f"%{dom}%")))
        .order_by(Request.sent_at.desc())
    ).all()
    candidates = [
        r for r in candidates
        if mailer.registered_domain((r.broker.email or "").split("@")[-1]) == dom or mailer.registered_domain(r.broker.web or "") == dom
    ]
    open_ones = [r for r in candidates if r.is_open]
    return (open_ones or candidates or [None])[0]


def process_incoming(db: Session, m: mailer.ParsedMail) -> Request | None:
    own = {settings.smtp_from.lower()} | {e.lower() for p in db.scalars(select(Profile)) for e in (p.emails or [])}
    if m.from_addr in own:
        return None
    req = _match_request(db, m)
    if req is None:
        return None
    if m.message_id and m.message_id in (req.message_ids or []):
        return None
    c = mailer.classify(m) if settings.auto_classify else mailer.Classification("reply")
    req.last_reply_at = utcnow()
    body = f"De : {m.from_name} <{m.from_addr}>\nDate : {m.date}\nObjet : {m.subject}\n\n{m.text[:15000]}"
    add_event(db, req, "reply", f"Réponse reçue de {m.from_addr} : {m.subject[:200]}", body)

    new_status = None
    if c.kind == "bounce":
        new_status = FAILED
        req.last_error = "Adresse e-mail du courtier invalide (message de non-distribution)"
    elif req.status in FINAL_STATUSES:
        new_status = None  # une réponse tardive ne rouvre pas une demande close
    elif c.kind == "needs_action":
        new_status = NEEDS_ACTION
    elif c.kind in ("completed_no_data", "completed_deleted"):
        new_status = COMPLETED
    elif c.kind == "ack":
        add_event(db, req, "info", f"Accusé de réception détecté (« {c.reason} »)")
    else:
        new_status = REPLIED
    if new_status and new_status != req.status:
        set_status(db, req, new_status, f"Classement automatique : {STATUS_LABELS[new_status]}" + (f" (« {c.reason} »)" if c.reason else ""))
    db.commit()
    return req


def poll_imap() -> int:
    if not settings.imap_configured or not _imap_lock.acquire(blocking=False):
        return 0
    matched = 0
    try:
        with SessionLocal() as db:
            first_sent = db.scalar(select(func.min(Request.sent_at)))
            if first_sent is None:
                return 0
            since = (first_sent - timedelta(days=1)).strftime("%d-%b-%Y")
            for folder in settings.imap_folders:
                key = f"imap:{folder}"
                state = (kv_get(db, key) or ":0").split(":")
                validity, last_uid = state[0] or None, int(state[1] or 0)
                try:
                    new_validity, msgs = mailer.fetch_new_messages(folder, validity, last_uid, since)
                except Exception as e:  # noqa: BLE001 - un dossier introuvable ne bloque pas les autres
                    log.warning("Relève IMAP du dossier %s impossible : %s", folder, e)
                    if len(settings.imap_folders) == 1:
                        raise
                    continue
                max_uid = last_uid if validity == new_validity else 0
                for uid, raw in msgs:
                    try:
                        if process_incoming(db, mailer.parse_mail(raw, str(uid))):
                            matched += 1
                    except Exception:  # noqa: BLE001 - un message mal formé ne doit pas bloquer la relève
                        log.exception("Message IMAP %s ignoré", uid)
                        db.rollback()
                    max_uid = max(max_uid, uid)
                kv_set(db, key, f"{new_validity}:{max_uid}")
                db.commit()
            kv_set(db, "imap_polled_at", utcnow().isoformat())
            db.commit()
    finally:
        _imap_lock.release()
    if matched:
        log.info("IMAP : %d réponse(s) rattachée(s)", matched)
    return matched


# --------------------------------------------------------------- Divers ----

def set_status(db: Session, req: Request, status: str, note: str | None = None) -> None:
    old = req.status
    req.status = status
    now = utcnow()
    if status in FINAL_STATUSES:
        req.closed_at = now
    else:
        req.closed_at = None
    if status == SENT and old in (NEEDS_ACTION, QUEUED, FAILED) and req.sent_at is None:
        # Formulaire rempli à la main : on démarre le délai légal.
        req.sent_at = now
        req.due_at = now + timedelta(days=settings.response_days)
    if status == QUEUED:
        req.attempts = 0
        req.last_error = None
    add_event(db, req, "status", note or f"Statut : {STATUS_LABELS.get(old, old)} → {STATUS_LABELS.get(status, status)}")


def stats(db: Session, profile_id: int | None = None) -> dict:
    q = select(Request.status, func.count()).group_by(Request.status)
    if profile_id:
        q = q.where(Request.profile_id == profile_id)
    counts = {s: 0 for s in STATUS_LABELS}
    counts.update(dict(db.execute(q).all()))
    total = sum(counts.values())
    done = counts[COMPLETED]
    counts["total"] = total
    counts["progress"] = round(100 * done / total) if total else 0
    counts["todo"] = counts[NEEDS_ACTION] + counts[REPLIED] + counts[OVERDUE] + counts[FAILED]
    counts["refused_or_overdue"] = counts[REFUSED] + counts[OVERDUE]
    return counts
