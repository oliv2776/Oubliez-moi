"""Comparateur de modèles : fait classer vos propres réponses de courtiers par plusieurs modèles Ollama.

Référence : le classement des règles, sur des réponses que les règles reconnaissent (elles ont été
vérifiées sur vos exports). Ce n'est donc pas une note absolue, mais un accord avec un classement
connu, mesuré sur vos données et votre matériel, avec le temps par réponse et la mémoire utilisée.
Rien n'est modifié : aucune demande ne change de statut, aucun e-mail ne part.
"""
from __future__ import annotations

import json
import statistics
import threading
import time

from fastapi import APIRouter, Depends, Form, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import actions, energy, llm, rules
from .config import settings
from .db import Request, SessionLocal, kv_get, kv_set, utcnow
from .web import get_db, render

router = APIRouter()
KV_KEY = "model_compare"
SIZES = (10, 20, 40)
MAX_MODELS = 5
# Les règles distinguent plus de cas que les modèles : on compare par familles.
GROUPS = {
    "completed_no_data": "clôture", "completed_deleted": "clôture", "ack": "accusé de réception",
    "id_request": "pièce d'identité", "confirm_link": "lien à cliquer", "extension": "délai prolongé",
    "refusal": "refus", "legit_refusal": "refus",
    "needs_action": "action demandée", "webform": "action demandée", "device_id": "action demandée",
    "clarification": "action demandée", "confirm_from_address": "action demandée", "residence_question": "action demandée",
}
_SKIP = {"reply", "bounce", "delivery_issue"}   # rien à comparer : incertain pour les règles, ou non-distribution

_lock = threading.Lock()
state: dict = {"running": False}


def sample(db: Session, size: int) -> list[dict]:
    """Dernières réponses que les règles reconnaissent, une par demande, sans laisser une famille dominer."""
    from .reanalysis import _from_event
    out, per_group = [], {}
    cap = max(2, size // 3)
    q = (select(Request).options(selectinload(Request.events), selectinload(Request.broker))
         .where(Request.last_reply_at.is_not(None)).order_by(Request.last_reply_at.desc()))
    for req in db.scalars(q):
        replies = [e for e in req.events if e.kind == "reply" and e.body]
        if not replies:
            continue
        kind = actions.kind_of_reply(replies[-1])
        group = GROUPS.get(kind)
        if kind in _SKIP or not group or per_group.get(group, 0) >= cap:
            continue
        m = _from_event(replies[-1])
        per_group[group] = per_group.get(group, 0) + 1
        out.append({"broker": req.broker.name, "subject": m.subject, "text": rules.latest_reply_text(m.text), "expected": group})
        if len(out) >= size:
            break
    return out


def _memory(model: str) -> dict:
    ps = energy.ollama_state() or {"models": []}
    for m in ps["models"]:
        if llm.normalize(m.get("name", "")) == llm.normalize(model):
            size, vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
            return {"memory_gb": round(size / 1e9, 1), "gpu_share": round(100 * vram / size) if size else 0}
    return {"memory_gb": None, "gpu_share": None}


def _run(models: list[str], items: list[dict]) -> None:
    results = []
    try:
        for model in models:
            mode = llm.catalog_kind(model) or "chat"
            row = {"model": model, "mode": mode, "agree": 0, "abstain": 0, "wrong": 0, "errors": 0, "mistakes": []}
            times = []
            for i, item in enumerate(items):
                if state.get("cancel"):
                    return
                state.update(model=model, done=len(results) * len(items) + i)
                started = time.monotonic()
                try:
                    res = llm.classify_with(model, mode, item["subject"], item["text"], timeout=300)
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                    row["errors"] += 1
                    row["last_error"] = str(e)[:160]
                    if row["errors"] >= 3 and row["errors"] == i + 1:
                        break  # le modèle ne répond pas du tout : inutile d'insister
                    continue
                times.append(time.monotonic() - started)
                got = GROUPS.get(res["category"]) if res else None
                if got == item["expected"]:
                    row["agree"] += 1
                elif got is None:
                    row["abstain"] += 1
                else:
                    row["wrong"] += 1
                    if len(row["mistakes"]) < 5:
                        row["mistakes"].append({"broker": item["broker"], "expected": item["expected"], "got": got})
            answered = row["agree"] + row["abstain"] + row["wrong"]
            row.update(total=len(items), answered=answered,
                       percent=round(100 * row["agree"] / answered) if answered else None,
                       # la première réponse inclut le chargement du modèle : on la met de côté
                       seconds=round(statistics.median(times[1:] or times), 1) if times else None,
                       load_seconds=round(times[0], 1) if times else None, **_memory(model))
            results.append(row)
        with SessionLocal() as db:
            kv_set(db, KV_KEY, json.dumps({"at": utcnow().isoformat(), "size": len(items), "results": results}, ensure_ascii=False))
            db.commit()
    finally:
        with _lock:
            state.clear()
            state["running"] = False


def start(models: list[str], size: int) -> str:
    """Lance la comparaison en arrière-plan. Renvoie un message d'erreur, ou ""."""
    installed = {llm.normalize(n) for n in (llm.installed_models() or [])}
    models = [m for m in dict.fromkeys(models) if llm.normalize(m) in installed][:MAX_MODELS]
    if not models:
        return "Choisissez au moins un modèle installé."
    with _lock:
        if state.get("running"):
            return "Une comparaison est déjà en cours."
        with SessionLocal() as db:
            items = sample(db, size if size in SIZES else SIZES[1])
        if len(items) < 5:
            return "Pas assez de réponses reconnues par les règles pour comparer (il en faut au moins 5)."
        state.update(running=True, cancel=False, models=models, total=len(models) * len(items), done=0, model=models[0],
                     since=time.time())
    threading.Thread(target=_run, args=(models, items), daemon=True, name="model-compare").start()
    return ""


def last(db: Session) -> dict | None:
    raw = kv_get(db, KV_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


@router.get("/settings/models/compare", response_class=HTMLResponse)
def compare_page(request: HttpRequest, db: Session = Depends(get_db)):
    installed = llm.installed_models() if settings.llm_url else None
    return render(request, "model_compare.html", db, installed=installed, catalog=llm.MODELS_BY_NAME, sizes=SIZES,
                  state=dict(state), last=last(db), max_models=MAX_MODELS,
                  current=llm.normalize(settings.llm_model), normalize=llm.normalize)


@router.post("/settings/models/compare")
def compare_start(request: HttpRequest, models: list[str] = Form(default=[]), size: int = Form(20)):
    err = start(models, size)
    if err:
        request.session.setdefault("flash", []).append(["error", err])
    return RedirectResponse("/settings/models/compare", status_code=303)


@router.post("/settings/models/compare/cancel")
def compare_cancel():
    if state.get("running"):
        state["cancel"] = True
    return RedirectResponse("/settings/models/compare", status_code=303)


@router.get("/settings/models/compare/status")
def compare_status():
    return JSONResponse({k: state.get(k) for k in ("running", "model", "done", "total")})
