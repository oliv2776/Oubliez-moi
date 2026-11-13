"""Réglages, tests de connexion, IA locale, sauvegardes, sobriété énergétique."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from .. import backup, crypto, llm, mailer, notify, scheduler, service, settings_store
from .. import energy, reanalysis
from ..config import settings
from ..db import (
    kv_get,
    kv_set,
    utcnow,
)

from ..web import back, flash, fmt_dt, get_db, render

router = APIRouter()


@router.get("/settings/energy", response_class=HTMLResponse)
def energy_page(request: HttpRequest, db: Session = Depends(get_db)):
    checks = energy.checks(db)
    return render(request, "energy.html", db, checks=checks,
                  n_warn=sum(1 for c in checks if c.status == "warn"), n_ok=sum(1 for c in checks if c.status == "ok"))


@router.post("/settings/energy/test-llm")
def energy_test_llm(request: HttpRequest):
    err = energy.warm_up_model()
    flash(request, f"Test impossible : {err}" if err else "Modèle chargé : voici où il tourne.", "error" if err else "ok")
    return back("/settings/energy")


@router.get("/settings", response_class=HTMLResponse)
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


@router.post("/settings")
async def settings_save(request: HttpRequest):
    form = dict(await request.form())
    model_before = reanalysis.current_model()
    if _save_settings(request, form):
        flash(request, "Réglages enregistrés.")
        if reanalysis.current_model() and reanalysis.current_model() != model_before:
            flash(request, f"Nouveau modèle d'IA ({settings.llm_model}) : vous pouvez réanalyser les réponses en attente "
                           "depuis « À traiter ».", "info")
    return back(_tab_url(form))


@router.post("/settings/reset")
def settings_reset(request: HttpRequest):
    settings_store.reset_to_env()
    scheduler.reschedule()
    flash(request, "Réglages de l'interface effacés : les valeurs du fichier .env s'appliquent de nouveau.")
    return back("/settings")


@router.post("/settings/test-smtp")
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


@router.post("/settings/test-imap")
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


@router.post("/settings/detect-authserv")
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


@router.post("/settings/test-llm")
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


@router.post("/settings/llm-pull")
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


@router.post("/settings/backup-now")
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


@router.get("/settings/encryption-key")
def encryption_key():
    return JSONResponse({"key": crypto.export_key()}, headers={"Cache-Control": "no-store"})


@router.post("/settings/llm-delete")
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


@router.get("/settings/llm-status")
def llm_status(model: str = ""):
    model = model or settings.llm_model
    installed = llm.installed_models(max_age=0 if llm.pull_status(model).get("done") else 20)
    return JSONResponse({
        "model": model,
        "reachable": installed is not None,
        "installed": llm.normalize(model) in {llm.normalize(n) for n in (installed or [])},
        "pull": llm.pull_status(model),
    })


@router.post("/settings/test-notify")
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


@router.post("/settings/poll")
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


@router.post("/settings/send-now")
def send_now(request: HttpRequest):
    res = service.send_tick()
    flash(request, f"Envoi : {res}" if res else "Rien à envoyer pour le moment.", "info")
    return back(request.headers.get("referer") or "/")
