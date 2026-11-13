"""Boîtes mail : connexion, analyse des expéditeurs, désinscriptions."""
from __future__ import annotations


from fastapi import APIRouter, Depends, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import crypto, mailboxes, oauth_ms
from ..db import (
    SENDER_STATUSES,
    Mailbox,
    Sender,
    utcnow,
)

from ..web import _get, _safe_next, back, flash, get_db, render

router = APIRouter()


# Analyse des expéditeurs en masse, indépendante des demandes RGPD aux courtiers.

@router.get("/mailboxes", response_class=HTMLResponse)
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


@router.get("/mailboxes/new", response_class=HTMLResponse)
def mailbox_new(request: HttpRequest, db: Session = Depends(get_db)):
    return render(request, "mailbox_form.html", db, mb=None, PROVIDERS=mailboxes.PROVIDERS,
                  ms_default=bool(oauth_ms.default_client_id()))


@router.get("/mailboxes/{mid}/edit", response_class=HTMLResponse)
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


@router.post("/mailboxes")
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


@router.post("/mailboxes/{mid}")
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


@router.post("/mailboxes/{mid}/delete")
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


@router.post("/mailboxes/settings-account/restore")
def mailbox_settings_restore(request: HttpRequest, db: Session = Depends(get_db)):
    mb = mailboxes.restore_settings_mailbox(db)
    if mb:
        flash(request, f"« {mb.address} » est de nouveau dans la liste. Lancez l'analyse quand vous voulez.")
    return back(f"/mailboxes?mailbox={mb.id}" if mb else "/mailboxes")


@router.post("/mailboxes/{mid}/test")
def mailbox_test(request: HttpRequest, mid: int, db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    try:
        flash(request, mailboxes.test_connection(mb, db))
    except mailboxes.MailboxError as e:
        flash(request, str(e), "error")
    return back(f"/mailboxes/{mid}/edit")


@router.post("/mailboxes/{mid}/scan")
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


@router.get("/mailboxes/status")
def mailboxes_status(db: Session = Depends(get_db)):
    boxes = db.scalars(select(Mailbox)).all()
    return JSONResponse({
        "scanning": [{"id": b.id, "label": b.label, "progress": b.progress} for b in boxes if b.scanning],
        "jobs": {k: v for k, v in mailboxes.jobs.items() if not v.get("seen")},
    })


@router.post("/mailboxes/{mid}/oauth/start")
def mailbox_oauth_start(mid: int, db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    try:
        flow = oauth_ms.start(mb.id, oauth_ms.client_id_for(mb.oauth_client_id), mb.oauth_tenant)
    except oauth_ms.OAuthError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"user_code": flow["user_code"], "uri": flow["uri"], "interval": flow["interval"]})


@router.post("/mailboxes/{mid}/oauth/poll")
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


@router.post("/senders/action")
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
