"""À traiter : tri des réponses, réponses types, export pour analyse, réanalyse."""
from __future__ import annotations

import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import letters, llm, mailview, service
from .. import actions, case_management, device_ids, history, insight, reanalysis, task_export
from ..config import settings
from ..db import (
    FAILED,
    NEEDS_ACTION,
    OVERDUE,
    REPLIED,
    SENT,
    STATUS_LABELS,
    Event,
    Request,
    add_event,
)

from ..web import _get, _safe_next, back, flash, fmt_d, get_db, render, templates

from .requests import _form_help

router = APIRouter()


TRIAGE = [
    ("replied", "Réponses à examiner", [REPLIED]),
    ("needs", "Actions requises", [NEEDS_ACTION]),
    ("overdue", "Sans réponse", [OVERDUE]),
    ("failed", "Échecs d'envoi", [FAILED]),
]


@router.get("/tasks", response_class=HTMLResponse)
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
        ctx["ai_note"] = next((e.summary for e in reversed(infos) if e.actor == history.AI and e.category == history.SUMMARY), None)
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


@router.get("/tasks/export")
def tasks_export(request: HttpRequest, db: Session = Depends(get_db)):
    """Fichier JSON des réponses (à traiter, terminées…) pour chercher de nouvelles règles de classement."""
    parts = {p for p in request.query_params.getlist("parts") if p in (*task_export.SECTIONS, "examples")}
    data = task_export.build(db, anonymize=request.query_params.get("brut") != "1", sections=parts or None)
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    name = f"oubliez-moi-analyse-{datetime.now():%Y%m%d-%H%M}.json"
    return StreamingResponse(iter([payload]), media_type="application/json; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.post("/requests/{rid}/send-proposed")
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


@router.post("/requests/{rid}/confirm-from")
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


@router.post("/requests/{rid}/summarize")
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



@router.get("/tasks/reanalysis", response_class=HTMLResponse)
def reanalysis_page(request: HttpRequest, db: Session = Depends(get_db)):
    return render(request, "reanalysis.html", db, proposals=reanalysis.load(db), rp=reanalysis.progress(),
                  model=reanalysis.current_model(), status_label=reanalysis.status_label)


@router.get("/tasks/reanalysis/status")
def reanalysis_status():
    return JSONResponse(reanalysis.progress())


@router.post("/tasks/reanalysis/start")
def reanalysis_start(request: HttpRequest, rid: int | None = Form(None)):
    if reanalysis.start([rid] if rid else None):
        flash(request, "Réanalyse lancée : les propositions s'affichent dès qu'elle est terminée. Rien n'est modifié sans votre accord.")
    else:
        flash(request, "Une réanalyse est déjà en cours.", "info")
    return back("/tasks/reanalysis")


@router.post("/tasks/reanalysis/apply")
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


@router.post("/tasks/reanalysis/dismiss")
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
