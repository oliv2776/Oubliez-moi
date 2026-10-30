"""Réanalyse des réponses déjà reçues avec le classement actuel (règles et modèle d'IA du moment).

Garde-fous :
- rien n'est appliqué sans validation : la réanalyse produit des propositions ;
- aucun effet après coup : ni réponse automatique, ni clic sur un lien de
  confirmation, ni relance ; seul le statut change, avec une trace ;
- le message est relu dans la boîte (par son Message-ID) pour retrouver ses
  en-têtes d'authentification : sans eux, aucune clôture n'est proposée ;
- une proposition n'est appliquée que si la demande n'a pas changé entre-temps.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from . import mailer
from .config import settings
from .db import (
    COMPLETED, FAILED, NEEDS_ACTION, OVERDUE, REPLIED, SENT, STATUS_LABELS, Event, Request, SessionLocal,
    add_event, kv_get, kv_set, utcnow,
)

log = logging.getLogger(__name__)
PROPOSALS_KEY = "reanalysis:proposals"
MODEL_KEY = "reanalysis:model"        # modèle avec lequel la dernière réanalyse a tourné
ELIGIBLE = (REPLIED, NEEDS_ACTION, SENT, OVERDUE)
# Clôtures et échecs décidés par l'automatisme (jamais ceux décidés à la main) : à revérifier aussi.
AUTO_DECIDED = (COMPLETED, FAILED)
_AUTO_PREFIXES = ("Classement automatique", "Réanalyse")
SENSITIVE = ("completed_no_data", "completed_deleted", "confirm_link", "extension", "bounce")

_lock = threading.Lock()
state: dict = {"running": False, "done": 0, "total": 0, "current": "", "phase": "", "started": None,
                "finished": None, "found": 0, "error": ""}


def _target(kind: str, current: str) -> str | None:
    """Statut auquel mènerait ce classement, sans effet de bord (None : pas de changement)."""
    return {
        "bounce": FAILED,
        "completed_no_data": COMPLETED, "completed_deleted": COMPLETED,
        "ack": SENT if current in (REPLIED, NEEDS_ACTION, COMPLETED, FAILED) else None,
        "delivery_issue": SENT if current in (FAILED, REPLIED, NEEDS_ACTION) else None,
        "webform": NEEDS_ACTION, "device_id": NEEDS_ACTION,
        "extension": SENT,
        "confirm_link": NEEDS_ACTION, "needs_action": NEEDS_ACTION, "id_request": NEEDS_ACTION,
        "legit_refusal": REPLIED, "residence_question": NEEDS_ACTION,
        "refusal": REPLIED, "reply": REPLIED,
    }.get(kind)


def decided_automatically(req: Request) -> bool:
    """Le dernier changement de statut vient du classement automatique ou d'une réanalyse, pas de l'utilisateur."""
    last = next((e for e in reversed(req.events) if e.kind == "status"), None)
    return bool(last and (last.summary or "").startswith(_AUTO_PREFIXES))


_BOUNCE_SENDER = re.compile(r"^(mailer-daemon|postmaster|non-?delivery|mail-?daemon)@", re.I)
_BOUNCE_SUBJECT = re.compile(r"^\s*(undeliverable|undelivered mail|delivery status notification|mail delivery (failed|failure)|"
                             r"returned mail|non remis|unzustellbar|nicht zustellbar|failure notice)", re.I)


def _from_event(ev: Event) -> mailer.ParsedMail:
    """Reconstitue la réponse depuis l'historique quand elle n'est plus dans la boîte (non authentifiée)."""
    head, _, text = (ev.body or "").partition("\n\n")
    meta = dict(re.findall(r"^(De|Date|Objet) : (.*)$", head, re.M))
    sender = re.search(r"<([^<>@\s]+@[^<>\s]+)>|(\S+@\S+)", meta.get("De", ""))
    addr = (sender.group(1) or sender.group(2)).lower() if sender else ""
    subject = meta.get("Objet", "")
    # Le type MIME du rapport de non-distribution n'est pas conservé : on le reconnaît à l'expéditeur et à l'objet.
    bounce = bool(_BOUNCE_SENDER.match(addr) or _BOUNCE_SUBJECT.match(subject))
    return mailer.ParsedMail(uid="", from_addr=addr, from_name="", subject=subject, is_bounce=bounce,
                             message_id=ev.message_id or "", text=text, date=meta.get("Date", ""), html=ev.html or "")


# Libellés d'un classement automatique qui exigeait un expéditeur authentifié (anciens libellés de statut inclus).
_SENSITIVE_LABELS = tuple(mailer.KIND_LABELS[k] for k in SENSITIVE) + (STATUS_LABELS[COMPLETED], STATUS_LABELS[FAILED])


def verified_at_reception(req: Request, ev: Event) -> bool:
    """L'expéditeur de cette réponse a été authentifié à sa réception.

    Une clôture ou un échec automatique n'est décidé qu'avec un expéditeur vérifié : si cette réponse
    en a provoqué un, on peut s'y fier même quand le message n'est plus dans la boîte.
    """
    events = list(req.events)
    try:
        i = next(k for k, e in enumerate(events) if e.id == ev.id)
    except StopIteration:
        return False
    for e in events[i + 1:]:
        if e.kind == "reply":
            break
        if e.kind == "warning" and (e.summary or "").startswith("Réponse non vérifiée"):
            return False
        summary = e.summary or ""
        if e.kind == "status" and summary.startswith("Classement automatique : ") and "non vérifié" not in summary:
            label = summary[len("Classement automatique : "):]
            if label.startswith(_SENSITIVE_LABELS):
                return True
    return False


def analyse(request_ids: list[int] | None = None) -> list[dict]:
    """Réanalyse la dernière réponse de chaque demande et renvoie les changements proposés."""
    from .service import _list_member_bounce, _sender_matches_broker, classify_reply
    with SessionLocal() as db:
        stmt = select(Request).options(selectinload(Request.events), selectinload(Request.broker))
        scope = (*ELIGIBLE, *AUTO_DECIDED)
        stmt = stmt.where(Request.id.in_(request_ids)) if request_ids else stmt.where(Request.status.in_(scope))
        items = []
        for req in db.scalars(stmt):
            replies = [e for e in req.events if e.kind == "reply"]
            if replies and (req.status in ELIGIBLE or (req.status in AUTO_DECIDED and decided_automatically(req))):
                items.append((req.id, replies[-1].id, replies[-1].message_id))
    raw_by_id: dict[str, bytes] = {}
    ids = [mid for _, _, mid in items if mid]
    state.update(phase="mailbox", total=len(items))
    if ids and settings.imap_configured:
        try:
            raw_by_id = mailer.fetch_by_message_id(ids)
        except Exception as e:  # noqa: BLE001 - boîte injoignable : on se rabat sur l'historique
            log.warning("Réanalyse : boîte injoignable (%s), analyse depuis l'historique", e)
    state.update(total=len(items), done=0, phase="analyse")
    proposals = []
    for req_id, ev_id, mid in items:
        with SessionLocal() as db:
            req, ev = db.get(Request, req_id), db.get(Event, ev_id)
            if req is None or ev is None:
                continue
            state["current"] = req.broker.name
            raw = raw_by_id.get(mid or "")
            m = mailer.parse_mail(raw) if raw else _from_event(ev)
            c, note, advice = classify_reply(m)
            if c.kind == "bounce" and _list_member_bounce(req, m):
                c = mailer.Classification("delivery_issue", "échec pour un membre de la liste de diffusion du courtier")
            trusted = (bool(raw) and _sender_matches_broker(req, m.from_addr, m.dmarc_pass)) or verified_at_reception(req, ev)
            kind = c.kind
            if kind in SENSITIVE and not trusted:
                if _target(kind, req.status) == req.status or req.status in AUTO_DECIDED:
                    state["done"] += 1
                    continue  # faute de pouvoir vérifier l'expéditeur, on ne défait pas une décision qui reste cohérente
                kind = "reply"  # même garde-fou qu'à la réception : pas de clôture sans expéditeur authentifié
            target = _target(kind, req.status)
            state["done"] += 1
            if target and target != req.status:
                state["found"] += 1
                proposals.append({
                    "request_id": req.id, "event_id": ev.id, "broker": req.broker.name, "reference": req.reference,
                    "from_status": req.status, "to_status": target, "kind": kind,
                    "label": mailer.KIND_LABELS.get(kind, kind), "reason": c.reason or "", "note": note or "",
                    "advice": advice or "",
                    "trusted": trusted, "from_mailbox": bool(raw), "downgraded": kind != c.kind,
                })
    return proposals


def _run(request_ids: list[int] | None) -> None:
    try:
        proposals = analyse(request_ids)
        with SessionLocal() as db:
            if request_ids:  # réanalyse ciblée : on complète les propositions existantes
                kept = [p for p in load(db) if p["request_id"] not in request_ids]
                proposals = kept + proposals
            kv_set(db, PROPOSALS_KEY, json.dumps(proposals, ensure_ascii=False))
            if not request_ids:
                kv_set(db, MODEL_KEY, current_model())
            db.commit()
        state["phase"] = "done"
    except Exception as e:  # noqa: BLE001
        log.exception("Réanalyse des réponses")
        state.update(phase="error", error=str(e)[:300])
    finally:
        state.update(running=False, current="", finished=time.time())
        _lock.release()


def start(request_ids: list[int] | None = None) -> bool:
    """Lance la réanalyse en arrière-plan. Renvoie False si une réanalyse tourne déjà."""
    if not _lock.acquire(blocking=False):
        return False
    state.update(running=True, done=0, total=0, current="", phase="start", started=time.time(), finished=None,
                 found=0, error="")
    threading.Thread(target=_run, args=(request_ids,), daemon=True).start()
    return True


def progress(with_models: bool = True) -> dict:
    """État lisible pour l'interface : réanalyse et modèle d'IA (with_models : interroge Ollama, jusqu'à 3 s)."""
    from . import llm
    busy = llm.activity.get("since")
    return {
        **state,
        "llm_model": current_model(),
        "llm_busy_for": round(time.time() - busy) if busy else None,
        "llm_busy_model": llm.activity.get("model"),
        "llm_loaded": llm.loaded_models() if (with_models and current_model()) else None,
        "finished_ago": round(time.time() - state["finished"]) if state.get("finished") else None,
    }


def current_model() -> str:
    return settings.llm_model if settings.llm_enabled else ""


def model_changed(db) -> bool:
    """Le modèle d'IA a changé depuis la dernière réanalyse complète (ou n'a jamais servi à en faire une)."""
    return bool(current_model()) and kv_get(db, MODEL_KEY) != current_model()


def load(db) -> list[dict]:
    try:
        return json.loads(kv_get(db, PROPOSALS_KEY) or "[]")
    except ValueError:
        return []


def apply(db, request_ids: list[int]) -> tuple[int, int]:
    """Applique les propositions choisies. Renvoie (appliquées, ignorées car la demande a changé)."""
    from .service import set_status
    applied = skipped = 0
    remaining = []
    for p in load(db):
        if p["request_id"] not in request_ids:
            remaining.append(p)
            continue
        req = db.get(Request, p["request_id"])
        latest = None
        if req is not None:
            replies = [e for e in req.events if e.kind == "reply"]
            latest = replies[-1].id if replies else None
        if req is None or req.status != p["from_status"] or latest != p["event_id"]:
            skipped += 1  # statut modifié ou nouvelle réponse depuis l'analyse : on ne touche à rien
            continue
        due, reminders = req.due_at, req.reminder_count
        why = f"Réanalyse : {p['label']}" + (f" (« {p['reason']} »)" if p["reason"] else "")
        if p["to_status"] == FAILED:
            req.last_error = "Adresse e-mail du courtier invalide (message de non-distribution)"
        elif p["from_status"] == FAILED:
            req.last_error = None
        set_status(db, req, p["to_status"], why)
        if p["kind"] == "extension":
            base = req.sent_at or utcnow()
            req.due_at = max(due or utcnow(), base + timedelta(days=settings.response_days + 60))
            add_event(db, req, "info", f"Prolongation annoncée : nouvelle échéance le {req.due_at:%d/%m/%Y}")
        else:  # un reclassement ne redémarre pas le délai légal
            req.due_at, req.reminder_count = due, reminders
        if p["note"]:
            add_event(db, req, "info", p["note"])
        if p.get("advice"):
            add_event(db, req, "info", f"Conseil IA : {p['advice']}"[:500])
        applied += 1
    kv_set(db, PROPOSALS_KEY, json.dumps(remaining, ensure_ascii=False))
    db.commit()
    return applied, skipped


def dismiss(db, request_ids: list[int] | None = None) -> None:
    kept = [p for p in load(db) if request_ids is not None and p["request_id"] not in request_ids]
    kv_set(db, PROPOSALS_KEY, json.dumps(kept, ensure_ascii=False))
    db.commit()


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)
