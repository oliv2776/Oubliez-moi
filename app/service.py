"""Logique métier : campagnes, envoi, relances, réponses, mise à jour des courtiers."""
from __future__ import annotations

import logging
import re
import threading
from datetime import timedelta

from sqlalchemy import String, cast, func, or_, select, true
from sqlalchemy.orm import Session, selectinload

from . import brokers as broker_catalog
from . import case_management, confirm, letters, llm, mailer, mailview, notify, phones
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
    TODO_STATUSES,
    Broker,
    Event,
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
            if rec.get("email") and b.email and rec["email"].lower() != b.email.lower():
                # Nouvelle adresse : on renvoie les demandes qui avaient échoué vers l'ancienne.
                for r in db.scalars(select(Request).where(Request.broker_id == b.id, Request.status == FAILED)):
                    set_status(db, r, QUEUED, f"Nouvelle adresse dans le catalogue ({rec['email']}) : demande renvoyée")
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
    if settings.resend_interval_days <= 0 or settings.max_automatic_resend_cycles <= 1:
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
            .where(Request.status == COMPLETED, Request.closed_at.is_not(None), Request.closed_at < cutoff,
                   Request.cycle < settings.max_automatic_resend_cycles)
        ).all()
        for old in rows:
            if not (old.profile.active and old.broker.enabled and old.broker.email):
                continue
            next_cycle = old.cycle + 1
            # Idempotence : si un cycle suivant existe déjà (reprise après incident,
            # exécution manuelle ou chevauchement de planificateurs), ne pas le dupliquer.
            already_created = db.scalar(
                select(Request.id).where(
                    Request.profile_id == old.profile_id,
                    Request.broker_id == old.broker_id,
                    Request.cycle >= next_cycle,
                ).limit(1)
            )
            if already_created:
                continue
            _create_request(db, old.profile, old.broker, cycle=next_cycle)
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
        .where(Request.status == QUEUED, Profile.active.is_(True), Broker.email.is_not(None), Broker.enabled.is_(True))
        .order_by(Request.attempts, Request.id)
        .limit(1)
    ).first()


def _max_letters() -> int:
    """Nombre de courriers de suivi avant « sans réponse » : relance, puis mise en demeure."""
    if not settings.auto_reminder:
        return 0
    return 2 if settings.auto_formal_notice else 1


def send_formal_notice(db: Session, req: Request) -> bool:
    subject, body = letters.formal_notice_letter(req)
    if not _deliver(db, req, subject, body):
        return False
    now = utcnow()
    req.reminder_count += 1
    req.due_at = now + timedelta(days=settings.formal_notice_wait_days)
    prefix = "[SIMULATION] " if settings.dry_run else ""
    add_event(db, req, "formal_notice", f"{prefix}Mise en demeure envoyée à {req.broker.email}", f"Objet : {subject}\n\n{body}")
    db.commit()
    return True


def _next_reminder(db: Session) -> Request | None:
    if not settings.auto_reminder:
        return None
    return db.scalars(
        select(Request)
        .join(Profile)
        .join(Broker)
        .where(Request.status == SENT, Request.reminder_count < _max_letters(), Request.due_at < utcnow(), Profile.active.is_(True),
               Broker.email.is_not(None))
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
            req = _next_supplement(db)
            if req:
                ok = send_supplement(db, req)
                return f"{'OK' if ok else 'ERREUR'} complément {req.reference} {req.broker.name}"
            req = _next_reminder(db)
            if req:
                if req.reminder_count == 0:
                    ok, what = send_reminder(db, req), "relance"
                else:
                    ok, what = send_formal_notice(db, req), "mise en demeure"
                return f"{'OK' if ok else 'ERREUR'} {what} {req.reference} {req.broker.name}"
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


# ------------------------------------------------------------ Compléments ----
# Quand on ajoute une adresse e-mail, un téléphone, une adresse postale ou un
# autre nom au profil après l'envoi, les courtiers déjà contactés ne les ont pas
# reçus. On compare donc le profil actuel aux courriers réellement envoyés.

IDENTITY_FIELDS = (("emails", "Adresse e-mail", "Email address"), ("phones", "Téléphone", "Phone number"),
                   ("addresses", "Adresse postale", "Postal address"), ("other_names", "Autre nom", "Other name"))
_SUPPLEMENT_STATUSES = {SENT, REPLIED, NEEDS_ACTION, OVERDUE, COMPLETED}


def _compact(text: str) -> str:
    return re.sub(r"[\s.\-()]", "", text).lower()


def _sent_text(req: Request) -> str:
    return "\n".join(e.body or "" for e in req.events if e.kind in ("sent", "supplement"))


def _covered(field: str, value: str, text: str, compact: str) -> bool:
    if field == "phones":
        p = phones.parse(value)
        if p.valid:
            return _compact(p.e164) in compact or _compact(p.national) in compact
        return _compact(value) in compact
    norm = " ".join(value.split()).lower()
    return norm in " ".join(text.split()).lower()


def missing_identifiers(req: Request) -> list[tuple[str, str]]:
    """Identifiants du profil absents des courriers déjà envoyés à ce courtier."""
    text = _sent_text(req)
    if not text:
        return []
    compact = _compact(text)
    out = []
    for field, *_ in IDENTITY_FIELDS:
        for value in getattr(req.profile, field) or []:
            if value and not _covered(field, value, text, compact):
                out.append((field, value))
    return out


def supplement_candidates(db: Session, profile: Profile) -> list[tuple[Request, list[tuple[str, str]]]]:
    """Dernière demande par courtier, déjà envoyée par e-mail, à qui il manque des identifiants."""
    latest: dict[int, Request] = {}
    q = (select(Request).options(selectinload(Request.events), selectinload(Request.broker))
         .where(Request.profile_id == profile.id).order_by(Request.cycle))
    for r in db.scalars(q):
        latest[r.broker_id] = r
    out = []
    for r in latest.values():
        if r.broker.email and r.sent_at and r.status in _SUPPLEMENT_STATUSES and not r.supplement_pending:
            missing = missing_identifiers(r)
            if missing:
                out.append((r, missing))
    return out


def queue_supplements(db: Session, profile: Profile) -> int:
    cands = supplement_candidates(db, profile)
    for r, missing in cands:
        r.supplement_pending = True
        add_event(db, r, "info", "Complément mis en file : " + ", ".join(v for _, v in missing))
    db.commit()
    return len(cands)


def send_supplement(db: Session, req: Request) -> bool:
    missing = missing_identifiers(req)
    if not missing:
        req.supplement_pending = False
        db.commit()
        return True
    subject, body = letters.supplement_letter(req, missing)
    if not _deliver(db, req, subject, body):
        return False
    now = utcnow()
    req.supplement_pending = False
    if req.status in (SENT, COMPLETED, OVERDUE):
        # Le délai d'un mois court à nouveau pour les données nouvellement signalées.
        req.status, req.closed_at = SENT, None
        req.due_at = now + timedelta(days=settings.response_days)
        req.reminder_count = 0
    prefix = "[SIMULATION] " if settings.dry_run else ""
    add_event(db, req, "supplement", f"{prefix}Complément envoyé à {req.broker.email} : " + ", ".join(v for _, v in missing),
              f"Objet : {subject}\n\n{body}")
    db.commit()
    return True


def _next_supplement(db: Session) -> Request | None:
    return db.scalars(
        select(Request).join(Profile)
        .where(Request.supplement_pending.is_(True), Profile.active.is_(True))
        .order_by(Request.id).limit(1)
    ).first()


def check_deadlines() -> int:
    """Passe en « sans réponse » les demandes relancées restées muettes et signale les dossiers RGPD en retard."""
    n = 0
    with SessionLocal() as db:
        todo_before = _todo_ids(db)
        # Sans relance possible (formulaire en ligne, relance désactivée), le délai expiré suffit.
        cond = or_(Request.reminder_count >= _max_letters(), Broker.email.is_(None)) if settings.auto_reminder else true()
        q = select(Request).join(Broker).where(Request.status == SENT, Request.due_at < utcnow(), cond)
        for req in db.scalars(q):
            req.status = OVERDUE
            add_event(db, req, "status", "Délai dépassé sans réponse : vous pouvez saisir la CNIL (dossier prêt dans la demande)")
            n += 1
        db.commit()
        if n:
            _notify_new_todo(db, todo_before, "Délai dépassé, plainte CNIL possible")
        cases = case_management.flag_overdue_cases(db)
        db.commit()
        if cases and settings.notify_enabled:
            names = ", ".join(sorted({c.organization.name for c in cases})[:4]) + ("…" if len(cases) > 4 else "")
            notify.push(f"Oubliez-moi : {len(cases)} dossier(s) RGPD en retard",
                        f"Échéance dépassée sans résultat confirmé : {names}", path="/tasks")
    return n


# ------------------------------------------------------------- Réponses ----

def _sender_matches_broker(req: Request, from_addr: str, dmarc_pass: bool = False) -> bool:
    """Authentifie l'expéditeur via DMARC validé par le serveur IMAP, puis vérifie son domaine.

    Le DMARC doit d'abord être validé par le serveur IMAP explicitement approuvé.
    Les plateformes de gestion RGPD sont admises en plus du domaine du courtier.
    """
    if not dmarc_pass or "@" not in (from_addr or ""):
        return False
    sender_domain = mailer.registered_domain(from_addr.rsplit("@", 1)[-1])
    if not sender_domain:
        return False
    expected = {
        d for d in (
            mailer.registered_domain((req.broker.email or "").rsplit("@", 1)[-1]),
            mailer.registered_domain(req.broker.web or ""),
        ) if d
    }
    return sender_domain in expected or sender_domain in confirm.PRIVACY_PLATFORMS


def _untrusted_reason(m: mailer.ParsedMail) -> str:
    """Explique pourquoi une réponse n'a pas été considérée comme authentifiée."""
    if not settings.imap_authserv_id:
        return "identifiant du serveur de messagerie non réglé — Réglages › Relève des réponses"
    if not m.dmarc_pass:
        return f"authentification DMARC absente ou en échec selon {settings.imap_authserv_id}"
    return "le domaine de l'expéditeur n'est pas celui du courtier"


def _match_request(db: Session, m: mailer.ParsedMail) -> Request | None:
    refs = mailer.find_references(m)
    if refs:
        req = db.scalars(select(Request).where(Request.reference.in_(refs)).order_by(Request.id.desc())).first()
        if req:
            # Les plateformes de tickets peuvent répondre depuis un domaine tiers.
            # Le rattachement par référence est permis, mais les décisions sensibles
            # restent conditionnées à la cohérence du domaine dans _handle_reply().
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


MAX_AUTO_REPLIES = 3
BOUNCE_ERROR = "Adresse e-mail du courtier invalide (message de non-distribution)"
LLM_MIN_CONFIDENCE = 0.7
_NOREPLY_RE = re.compile(r"^(no-?reply|do-?not-?reply|ne-?pas-?repondre|mailer-daemon|postmaster|bounce)", re.I)


def _own_addresses(db: Session) -> set[str]:
    return {settings.smtp_from.lower()} | {e.lower() for p in db.scalars(select(Profile)) for e in (p.emails or [])}


def process_incoming(db: Session, m: mailer.ParsedMail) -> Request | None:
    """Rattache une réponse à sa demande, la classe et réagit. Renvoie la demande concernée, ou None."""
    if m.from_addr in _own_addresses(db):
        return None
    # Anti-doublon : le même message peut être relevé deux fois (autre dossier, boîte réindexée…)
    if m.message_id:
        dup = db.scalars(select(Event).where(Event.message_id == m.message_id)).first()
        if dup:
            return dup.request
    req = _match_request(db, m)
    if req is None:
        return None
    if m.message_id and m.message_id in (req.message_ids or []):
        return None
    req.last_reply_at = utcnow()
    sender = f"{m.from_name} <{m.from_addr}>" if m.from_name else m.from_addr
    body = f"De : {sender}\nDate : {m.date}\nObjet : {m.subject}\n\n{m.text[:15000]}"
    ev = add_event(db, req, "reply", f"Réponse reçue de {m.from_addr} : {m.subject[:200]}", body)
    ev.message_id = (m.message_id or None)
    if m.html:
        ev.html = m.html[:500_000]
    db.flush()
    _handle_reply(db, req, m)
    db.commit()
    return req


def classify_reply(m: mailer.ParsedMail) -> tuple[mailer.Classification, str | None, str | None]:
    """Classement d'une réponse (mots-clés, puis IA locale si ambiguë), sans aucun effet.

    Renvoie (classement, note IA, conseil IA). Le conseil n'existe qu'avec un modèle conversationnel.
    """
    if not settings.auto_classify:
        return mailer.Classification("reply"), None, None
    c = mailer.classify(m)
    note = advice = None
    if c.kind == "reply" and settings.llm_enabled:
        res = llm.classify(m.subject, mailview.strip_quoted(m.text))
        if res:
            label = mailer.KIND_LABELS.get(res["category"], res["category"])
            if res["summary"]:
                note = f"Analyse IA : {res['summary']}"
            else:  # modèle de décision : pas de texte, on affiche la catégorie et sa probabilité
                note = f"Analyse IA : {label} (probabilité {round(res['confidence'] * 100)} %)"
            ticket = res["ticket"] or mailer.extract_ticket(f"{m.subject}\n{mailview.strip_quoted(m.text)}")
            if ticket:
                note += f" (dossier n° {ticket})"
            note = note[:500]
            advice = res.get("action")
            if res["confidence"] >= LLM_MIN_CONFIDENCE:
                return mailer.Classification(res["category"], f"IA, probabilité {round(res['confidence'] * 100)} %"), note, advice
    return c, note, advice


def _classify(db: Session, req: Request, m: mailer.ParsedMail) -> mailer.Classification:
    c, note, advice = classify_reply(m)
    if note:
        add_event(db, req, "info", note)
    if advice:
        add_event(db, req, "info", f"Conseil IA : {advice}"[:500])
    return c


def _list_member_bounce(req: Request, m: mailer.ParsedMail) -> bool:
    """Échec limité à des membres d'une liste de diffusion du courtier : son adresse a bien reçu le message."""
    failed = mailer.bounced_recipients(m.text)
    broker = (req.broker.email or "").lower()
    if not failed or not broker or broker in failed:
        return False
    domains = confirm.broker_domains(req.broker.email, req.broker.web)
    return all(mailer.registered_domain(a.rsplit("@", 1)[-1]) in domains for a in failed)


def _handle_reply(db: Session, req: Request, m: mailer.ParsedMail) -> None:
    c = _classify(db, req, m)
    if c.kind == "bounce" and _list_member_bounce(req, m):
        c = mailer.Classification("delivery_issue", "échec pour un membre de la liste de diffusion du courtier")
    sender_trusted = _sender_matches_broker(req, m.from_addr, m.dmarc_pass)
    if not sender_trusted:
        # Une référence permet d'identifier le ticket, pas d'authentifier son expéditeur.
        # Ne jamais clôturer, confirmer automatiquement ou prolonger sur cette seule base.
        if c.kind in ("completed_no_data", "completed_deleted", "confirm_link", "extension", "bounce"):
            add_event(db, req, "warning", f"Réponse non vérifiée de {m.from_addr} : décision sensible non appliquée "
                                          f"({_untrusted_reason(m)})")
            c = mailer.Classification("reply", "expéditeur non vérifié")
    label = mailer.KIND_LABELS.get(c.kind, c.kind)
    why = f"Classement automatique : {label}" + (f" (« {c.reason} »)" if c.reason else "")

    if c.kind == "bounce":
        req.last_error = BOUNCE_ERROR
        set_status(db, req, FAILED, why)
        return
    if c.kind == "delivery_issue":
        add_event(db, req, "info", f"Incident de distribution sans conséquence (« {c.reason} ») : rien à faire")
        return
    if req.status in FINAL_STATUSES:
        return  # une réponse tardive ne rouvre pas une demande close
    if req.status == FAILED and (req.last_error or "") == BOUNCE_ERROR:
        # Le courtier répond : le message de non-distribution ne concernait pas son adresse principale.
        req.last_error = None
        add_event(db, req, "info", "Le courtier a répondu après un message de non-distribution : son adresse fonctionne")
        if c.kind == "ack":
            set_status(db, req, SENT, why)
    if c.kind in ("completed_no_data", "completed_deleted"):
        set_status(db, req, COMPLETED, why)
    elif c.kind == "ack":
        add_event(db, req, "info", f"Accusé de réception détecté (« {c.reason} »)")
    elif c.kind == "extension":
        base = req.sent_at or utcnow()
        new_due = max(req.due_at or utcnow(), base + timedelta(days=settings.response_days + 60))
        req.due_at = new_due
        if req.status != SENT:
            set_status(db, req, SENT, why)
        add_event(db, req, "info", f"Prolongation annoncée (RGPD art. 12.3 : deux mois de plus au maximum) : nouvelle échéance le {new_due:%d/%m/%Y}")
    elif c.kind in ("id_request", "legit_refusal", "residence_question"):
        if settings.auto_replies and _can_auto_reply(req, c.kind) and _send_auto_reply(db, req, c.kind, m):
            if c.kind == "legit_refusal":
                req.due_at = utcnow() + timedelta(days=settings.response_days)
            if req.status != SENT:
                set_status(db, req, SENT, why + " — réponse automatique envoyée")
        else:
            set_status(db, req, NEEDS_ACTION if c.kind == "id_request" else REPLIED, why)
    elif c.kind == "confirm_link":
        links = confirm.candidate_links(m.links, req.broker.email, req.broker.web)
        if settings.auto_confirm_links and links:
            res = confirm.click(links[0], confirm.broker_domains(req.broker.email, req.broker.web))
            add_event(db, req, "confirm", f"Lien de confirmation ouvert automatiquement : {res.detail}", res.url)
            if res.ok:
                now = utcnow()
                req.due_at = now + timedelta(days=settings.response_days)
                req.reminder_count = 0
                set_status(db, req, SENT, why + " — confirmé automatiquement")
                return
        elif links:
            add_event(db, req, "info", "Lien de confirmation à ouvrir", links[0])
        set_status(db, req, NEEDS_ACTION, why)
    elif c.kind == "confirm_from_address":
        plan = confirm_from_plan(db, req, m.subject, m.text, m.message_id)
        if (plan and plan["mailbox"] is not None and settings.auto_replies and _can_auto_reply(req, c.kind)
                and send_confirm_from(db, req, plan)[0]):
            set_status(db, req, SENT, why + f" — confirmation envoyée depuis {plan['address']}")
        else:
            set_status(db, req, NEEDS_ACTION, why)
    elif c.kind in ("needs_action", "webform", "device_id"):
        set_status(db, req, NEEDS_ACTION, why)
    else:  # refus probable, réponse non reconnue
        set_status(db, req, REPLIED, why)


def confirm_from_plan(db: Session, req: Request, subject: str, text: str, message_id: str | None = None) -> dict | None:
    """Le courtier demande une confirmation écrite depuis une autre adresse du profil : de quelle adresse, vers qui, quoi.

    Renvoie None si la réponse ne demande pas cela, ou si l'adresse n'est pas une adresse du profil.
    """
    from . import mailboxes
    m = mailer.ParsedMail(uid="", from_addr="", from_name="", subject=subject, message_id=message_id or "", text=text)
    c = mailer.classify(m)
    if c.kind != "confirm_from_address":
        return None
    current = mailer.latest_reply_text(text)
    mine = [e.lower() for e in (req.profile.emails or []) if e]
    main = (settings.smtp_from or "").lower()
    # L'adresse citée dans la phrase reconnue d'abord, sinon une adresse du profil citée dans la réponse.
    cited = [a for a in mailer.emails_in(c.reason) + mailer.emails_in(current) if a in mine and a != main]
    if not cited:
        return None
    address = cited[0]
    allowed = {d for d in (mailer.registered_domain((req.broker.email or "").split("@")[-1]),
                           mailer.registered_domain(req.broker.web or "")) if d} | confirm.PRIVACY_PLATFORMS
    targets = [a for a in mailer.emails_in(current)
               if a not in mine and not _NOREPLY_RE.match(a.split("@")[0]) and mailer.registered_domain(a.split("@")[-1]) in allowed]
    to = targets[0] if targets else req.broker.email
    if not to:
        return None
    letter_subject, body = letters.confirm_from_letter(req, address, subject)
    mb = mailboxes.for_address(db, address)
    return {"address": address, "to": to, "mailbox": mb, "subject": letter_subject, "body": body,
            "in_reply_to": message_id or None}


def confirm_from_plan_for(db: Session, req: Request) -> dict | None:
    """Même chose à partir de la dernière réponse enregistrée (page À traiter, réanalyse)."""
    replies = [e for e in req.events if e.kind == "reply"]
    if not replies:
        return None
    headers, text = mailview.split_headers(replies[-1].body or "")
    return confirm_from_plan(db, req, headers.get("Objet", ""), text, replies[-1].message_id)


def send_confirm_from(db: Session, req: Request, plan: dict) -> tuple[bool, str]:
    """Envoie la confirmation depuis la boîte de l'autre adresse. Les réponses reviennent à l'adresse habituelle."""
    from . import mailboxes
    mb = plan.get("mailbox")
    if mb is None:
        return False, f"{plan['address']} n'est pas connectée dans Boîtes mail : envoyez le message vous-même."
    msg = mailer.build_message(plan["to"], plan["subject"], plan["body"], from_name=req.profile.full_name,
                               reply_to=settings.smtp_from or None, in_reply_to=plan.get("in_reply_to"),
                               from_addr=plan["address"])
    try:
        mailboxes.send_from(mb, msg, db)
    except mailboxes.MailboxError as e:
        add_event(db, req, "error", f"Confirmation non envoyée depuis {plan['address']} : {e}")
        return False, str(e)
    req.message_ids = [*(req.message_ids or []), msg["Message-ID"]]
    prefix = "[SIMULATION] " if settings.dry_run else ""
    add_event(db, req, "auto_reply",
              f"{prefix}Confirmation envoyée depuis {plan['address']} à {plan['to']} "
              f"({mailer.KIND_LABELS['confirm_from_address']})", f"Objet : {plan['subject']}\n\n{plan['body']}")
    return True, ""


def _can_auto_reply(req: Request, kind: str) -> bool:
    """Garde-fou anti-boucle : une seule réponse automatique par motif, trois au maximum par demande."""
    autos = [e for e in req.events if e.kind == "auto_reply"]
    label = mailer.KIND_LABELS[kind]
    return len(autos) < MAX_AUTO_REPLIES and not any(label in e.summary for e in autos)


def _send_auto_reply(db: Session, req: Request, kind: str, m: mailer.ParsedMail) -> bool:
    to = m.reply_to or m.from_addr
    # On ne répond qu'au domaine du courtier (ou d'une plateforme RGPD connue) : un message falsifié
    # ne doit pas pouvoir détourner une réponse, qui contient vos coordonnées, vers une autre adresse.
    allowed = {d for d in (mailer.registered_domain((req.broker.email or "").split("@")[-1]),
                           mailer.registered_domain(req.broker.web or "")) if d} | confirm.PRIVACY_PLATFORMS
    if not to or _NOREPLY_RE.match(to.split("@")[0]) or mailer.registered_domain(to.split("@")[-1]) not in allowed:
        to = req.broker.email
    if not to:
        return False
    subject, body = letters.auto_reply_letter(req, kind, m.subject)
    msg = mailer.build_message(to, subject, body, from_name=req.profile.full_name, in_reply_to=m.message_id or None)
    try:
        mailer.send_message(msg)
    except mailer.SendError as e:
        add_event(db, req, "error", f"Réponse automatique non envoyée : {e}")
        return False
    req.message_ids = [*(req.message_ids or []), msg["Message-ID"]]
    prefix = "[SIMULATION] " if settings.dry_run else ""
    add_event(db, req, "auto_reply", f"{prefix}Réponse automatique envoyée à {to} ({mailer.KIND_LABELS[kind]})",
              f"Objet : {subject}\n\n{body}")
    return True


def _todo_ids(db: Session) -> set[int]:
    return set(db.scalars(select(Request.id).where(Request.status.in_(TODO_STATUSES))))


def _notify_new_todo(db: Session, before: set[int], context: str) -> None:
    if not settings.notify_enabled:
        return
    new_ids = _todo_ids(db) - before
    if not new_ids:
        return
    reqs = db.scalars(select(Request).where(Request.id.in_(new_ids))).all()
    names = ", ".join(sorted({r.broker.name for r in reqs})[:4]) + ("…" if len(reqs) > 4 else "")
    notify.push(f"Oubliez-moi : {len(reqs)} élément(s) à traiter", f"{context} : {names}")


def send_digest() -> str | None:
    """Récapitulatif quotidien par e-mail de ce qui attend une décision."""
    if not settings.notify_email_digest or not settings.smtp_configured:
        return None
    with SessionLocal() as db:
        today = utcnow().date().isoformat()
        if kv_get(db, "digest_sent_on") == today:
            return None
        reqs = db.scalars(select(Request).where(Request.status.in_(TODO_STATUSES)).order_by(Request.status)).all()
        late_cases, _ = case_management.cases_due(db)
        if not reqs and not late_cases:
            return None
        base = settings.app_url.rstrip("/") if settings.app_url else ""
        lines = [f"- {r.broker.name} ({r.profile.full_name}) : {STATUS_LABELS[r.status]}" + (f"\n  {base}/tasks?id={r.id}" if base else "")
                 for r in reqs[:50]]
        case_lines = [f"- {c.organization.name} ({c.type_label}) : échéance du {c.effective_deadline.strftime('%d/%m/%Y')} dépassée"
                      + (f"\n  {base}/privacy-cases/{c.id}" if base else "") for c in late_cases[:50]]
        st = stats(db)
        total = len(reqs) + len(late_cases)
        body = "Bonjour,\n\n"
        if reqs:
            body += f"{len(reqs)} demande(s) attendent une décision de votre part :\n\n" + "\n".join(lines) + "\n\n"
        if late_cases:
            body += f"{len(late_cases)} dossier(s) RGPD ont dépassé leur échéance :\n\n" + "\n".join(case_lines) + "\n\n"
        body += (f"Au total : {st['completed']} suppression(s) confirmée(s), {st['sent']} demande(s) en attente de réponse.\n"
                 + (f"\nOuvrir Oubliez-moi : {base}/tasks\n" if base else ""))
        msg = mailer.build_message(settings.smtp_from, f"Oubliez-moi : {total} élément(s) à traiter", body, "Oubliez-moi")
        mailer.send_message(msg, force=True)
        kv_set(db, "digest_sent_on", today)
        db.commit()
        return f"récapitulatif envoyé ({total} éléments)"


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
            own = _own_addresses(db)
            todo_before = _todo_ids(db)
            filing = settings.imap_file_mode != "off" and settings.imap_file_folder
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
                to_file = []
                for uid, raw in msgs:
                    try:
                        m = mailer.parse_mail(raw, str(uid))
                        req = process_incoming(db, m)
                        if req:
                            matched += 1
                        own_copy = settings.imap_file_own_copies and m.from_addr in own and mailer.find_references(m)
                        if req or own_copy:
                            to_file.append(uid)
                    except Exception:  # noqa: BLE001 - un message mal formé ne doit pas bloquer la relève
                        log.exception("Message IMAP %s ignoré", uid)
                        db.rollback()
                    max_uid = max(max_uid, uid)
                kv_set(db, key, f"{new_validity}:{max_uid}")
                db.commit()
                if filing and to_file and folder != settings.imap_file_folder:
                    try:
                        msg = mailer.file_messages(folder, to_file, settings.imap_file_mode, settings.imap_file_folder, settings.imap_mark_read)
                        log.info("IMAP : %s", msg)
                        kv_set(db, "imap_file_status", f"{utcnow().isoformat()}|ok|{msg}")
                    except Exception as e:  # noqa: BLE001
                        log.warning("Rangement IMAP impossible : %s", e)
                        kv_set(db, "imap_file_status", f"{utcnow().isoformat()}|error|{e}")
                    db.commit()
            kv_set(db, "imap_polled_at", utcnow().isoformat())
            db.commit()
            _notify_new_todo(db, todo_before, "Nouvelles réponses de courtiers")
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
    elif status == SENT and old in (NEEDS_ACTION, REPLIED) and req.sent_at:
        # L'action demandée par le courtier vient d'être faite : un nouveau délai d'un mois commence.
        req.due_at = now + timedelta(days=settings.response_days)
        req.reminder_count = 0
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
