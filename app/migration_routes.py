"""Pages de la migration des comptes vers des alias Proton (voir migration.py et simplelogin.py)."""
from __future__ import annotations

import re
import threading

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import migration, settings_store, simplelogin
from .config import settings
from .db import MIGRATION_STATUSES, AccountMigration, Mailbox, kv_get
from .web import get_db, render

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FILTERS = {
    "todo": ("À faire", ["todo", "alias"]),
    "migrated": ("Migrés", ["migrated"]),
    "abandoned": ("À abandonner", ["abandoned"]),
    "ignored": ("Pas des comptes", ["ignored"]),
    "all": ("Tous", list(MIGRATION_STATUSES)),
}


def sl_settings_url() -> str:
    return (settings.simplelogin_url or "https://app.simplelogin.io").rstrip("/") + "/dashboard/setting"


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _get(db: Session, acc_id: int) -> AccountMigration:
    acc = db.get(AccountMigration, acc_id)
    if acc is None:
        raise HTTPException(404)
    return acc


def _return(form_next: str | None, acc: AccountMigration | None = None) -> str:
    target = form_next if form_next and form_next.startswith("/migration") and "//" not in form_next else "/migration"
    return target + (f"#compte-{acc.id}" if acc else "")


@router.get("/migration", response_class=HTMLResponse)
def migration_page(request: Request, filter: str = "todo", kind: str = "", q: str = "", db: Session = Depends(get_db)):
    if filter not in FILTERS:
        filter = "todo"
    started = kv_get(db, migration.STARTED_KEY)
    stmt = select(AccountMigration).where(AccountMigration.status.in_(FILTERS[filter][1]))
    if kind == "account":
        stmt = stmt.where(AccountMigration.old_account_count > 0)
    if q.strip():
        like = "%" + q.strip()[:80].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(or_(AccountMigration.domain.ilike(like, escape="\\"), AccountMigration.name.ilike(like, escape="\\"),
                              AccountMigration.alias.ilike(like, escape="\\")))
    accounts = db.scalars(stmt.order_by((AccountMigration.old_account_count > 0).desc(),
                                        AccountMigration.old_last_seen.desc().nullslast(), AccountMigration.domain).limit(500)).all()
    counts = dict(db.execute(select(AccountMigration.status, func.count()).group_by(AccountMigration.status)).all())
    chips = [(key, label, sum(counts.get(s, 0) for s in statuses)) for key, (label, statuses) in FILTERS.items()]
    boxes = db.scalars(select(Mailbox).order_by(Mailbox.linked_settings.desc(), Mailbox.id)).all()
    return render(request, "migration.html", db, accounts=accounts, counts=counts, chips=chips, filter=filter, kind=kind,
                  q=q, started=started, boxes=boxes, role_of=migration.role_of, running=migration.running(),
                  progress=list(migration.progress.values()), sl_configured=simplelogin.configured(),
                  sl_unreadable=settings_store.secret_unavailable("simplelogin_api_key"), STATUSES=MIGRATION_STATUSES,
                  sl_settings_url=sl_settings_url())


@router.get("/migration/status")
def migration_status():
    return JSONResponse({"running": migration.running(), "progress": list(migration.progress.values())})


def _save_roles(db: Session, form) -> list[int]:
    ids = []
    for mb in db.scalars(select(Mailbox)):
        role = form.get(f"role_{mb.id}")
        if role in ("old", "new", "none"):
            mb.migration_role = role
        if migration.role_of(mb) != "none":
            ids.append(mb.id)
    db.commit()
    return ids


@router.post("/migration/start")
async def migration_start(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ids = _save_roles(db, form)
    if not ids:
        flash(request, "Choisissez au moins une boîte à inventorier.", "error")
    elif migration.running():
        flash(request, "Un inventaire est déjà en cours.", "info")
    else:
        migration.start_in_background(ids)
        flash(request, "Inventaire lancé : les comptes apparaissent au fur et à mesure (quelques minutes pour deux ans d'e-mails).")
    return back("/migration")


@router.post("/migration/simplelogin")
async def migration_simplelogin(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    if form.get("clear"):
        settings_store.set_value("simplelogin_api_key", "")
        flash(request, "Clé d'API SimpleLogin effacée.")
        return back("/migration#reglages")
    key = (form.get("api_key") or "").strip()
    if key:
        settings_store.set_value("simplelogin_api_key", key)
    try:
        info = simplelogin.user_info()
    except simplelogin.SimpleLoginError as e:
        flash(request, str(e), "error")
        return back("/migration#reglages")
    flash(request, f"Connecté à SimpleLogin ({info.get('email', 'compte')}) : vos alias Proton Pass sont en cours de lecture.")
    _sync_in_background()
    return back("/migration")


def _sync_in_background() -> None:
    def run():
        with migration._lock:
            try:
                simplelogin.sync()
            except simplelogin.SimpleLoginError as e:
                migration.log.warning("Synchronisation des alias : %s", e)
    threading.Thread(target=run, daemon=True).start()


@router.post("/migration/sync")
def migration_sync(request: Request):
    if not simplelogin.configured():
        flash(request, "Ajoutez d'abord votre clé d'API SimpleLogin.", "error")
    elif migration.running():
        flash(request, "Un inventaire est déjà en cours : les alias seront vérifiés à la fin.", "info")
    else:
        _sync_in_background()
        flash(request, "Vérification des alias lancée.")
    return back("/migration")


@router.post("/migration/{acc_id}/alias/create")
def migration_alias_create(request: Request, acc_id: int, next: str = Form(""), db: Session = Depends(get_db)):
    acc = _get(db, acc_id)
    try:
        address = simplelogin.create_alias(acc)
        db.commit()
        flash(request, f"Alias créé pour {acc.domain} : {address}. Mettez-le comme adresse e-mail dans votre compte {acc.domain}.")
    except simplelogin.SimpleLoginError as e:
        flash(request, str(e), "error")
    return back(_return(next, acc))


@router.post("/migration/{acc_id}/alias")
def migration_alias_set(request: Request, acc_id: int, alias: str = Form(""), next: str = Form(""),
                        db: Session = Depends(get_db)):
    acc = _get(db, acc_id)
    alias = alias.strip().lower()
    if alias and not EMAIL_RE.match(alias):
        flash(request, "Adresse d'alias invalide.", "error")
        return back(_return(next, acc))
    # Refuser un alias deviné l'empêche d'être reproposé ; l'activité réelle du service reste prise en compte.
    rejected = "rejected" if not alias and acc.alias_source == "guessed" else None
    acc.alias, acc.alias_id, acc.alias_source = (alias or None), None, ("manual" if alias else rejected)
    acc.refresh_status()
    db.commit()
    return back(_return(next, acc))


@router.post("/migration/{acc_id}/alias/confirm")
def migration_alias_confirm(acc_id: int, next: str = Form(""), db: Session = Depends(get_db)):
    acc = _get(db, acc_id)
    if acc.alias:
        acc.alias_source = "manual"
        db.commit()
    return back(_return(next, acc))


@router.post("/migration/{acc_id}/status")
def migration_set_status(request: Request, acc_id: int, status: str = Form(""), next: str = Form(""),
                         db: Session = Depends(get_db)):
    acc = _get(db, acc_id)
    if status and status not in ("migrated", "abandoned", "ignored"):
        raise HTTPException(400)
    acc.user_status = status or None
    acc.refresh_status()
    db.commit()
    return back(_return(next, acc))


@router.post("/migration/roles")
async def migration_roles(request: Request, db: Session = Depends(get_db)):
    _save_roles(db, await request.form())
    flash(request, "Rôle des boîtes enregistré.")
    return back("/migration#reglages")


@router.post("/migration/reset")
def migration_reset(request: Request, db: Session = Depends(get_db)):
    if migration.running():
        flash(request, "Attendez la fin de l'inventaire en cours.", "error")
        return back("/migration")
    migration.reset(db)
    flash(request, "Inventaire effacé. Vos alias, eux, ne sont pas touchés.")
    return back("/migration")
