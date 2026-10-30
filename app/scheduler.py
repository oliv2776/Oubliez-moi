"""Tâches périodiques (envoi lissé, relève IMAP, échéances, renvois, mise à jour des courtiers)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import backup, mailboxes, migration, service
from .config import settings
from .db import SessionLocal, kv_get, kv_set, utcnow

log = logging.getLogger(__name__)
logging.getLogger("apscheduler").setLevel(logging.WARNING)  # pas de log à chaque exécution
scheduler = BackgroundScheduler(timezone=settings.timezone, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})


def _safe(fn):
    def wrapper():
        try:
            result = fn()
            if result:
                log.info("%s : %s", fn.__name__, result)
        except Exception:  # noqa: BLE001
            log.exception("Tâche %s en erreur", fn.__name__)
    wrapper.__name__ = fn.__name__
    return wrapper


def maybe_update_brokers():
    if settings.broker_auto_update_days <= 0:
        return None
    with SessionLocal() as db:
        last = kv_get(db, "brokers_synced_at")
    if last and datetime.fromisoformat(last) > utcnow() - timedelta(days=settings.broker_auto_update_days):
        return None
    return service.update_brokers_from_remote()


def run_backup():
    """Sauvegarde planifiée ; l'erreur éventuelle est affichée dans Réglages."""
    with SessionLocal() as db:
        try:
            res = backup.scheduled()
            if res:
                kv_set(db, "backup_status", f"{utcnow().isoformat()}|ok|{res}")
        except Exception as e:  # noqa: BLE001
            kv_set(db, "backup_status", f"{utcnow().isoformat()}|error|{e}")
            db.commit()
            raise
        db.commit()
    return res


def daily():
    return {
        "renvois": service.schedule_resends(),
        "nouveaux_courtiers": service.enroll_new_brokers() if settings.auto_enroll_new_brokers else 0,
    }


def start() -> None:
    if settings.disable_scheduler:
        log.info("Planificateur désactivé (DISABLE_SCHEDULER)")
        return
    interval = max(10, 3600 // settings.send_per_hour)
    scheduler.add_job(_safe(service.send_tick), "interval", seconds=interval, id="send", next_run_time=utcnow_local())
    scheduler.add_job(_safe(service.poll_imap), "interval", minutes=max(1, settings.imap_poll_minutes), id="imap")
    scheduler.add_job(_safe(service.check_deadlines), "interval", hours=1, id="deadlines")
    scheduler.add_job(_safe(daily), "cron", hour=4, minute=17, id="daily")
    scheduler.add_job(_safe(maybe_update_brokers), "cron", hour=4, minute=43, id="brokers")
    scheduler.add_job(_safe(service.send_digest), "cron", hour=8, minute=7, id="digest")
    scheduler.add_job(_safe(run_backup), "cron", hour=3, minute=37, id="backup")
    scheduler.add_job(_safe(mailboxes.scan_all), "cron", hour=5, minute=13, id="mailboxes")
    scheduler.add_job(_safe(migration.inventory_all), "cron", hour=5, minute=41, id="migration")
    scheduler.start()
    log.info("Planificateur démarré : 1 e-mail toutes les %ss, relève IMAP toutes les %s min", interval, settings.imap_poll_minutes)


def reschedule() -> None:
    """Applique un nouveau débit / une nouvelle fréquence de relève sans redémarrer."""
    if not scheduler.running:
        return
    scheduler.reschedule_job("send", trigger="interval", seconds=max(10, 3600 // settings.send_per_hour))
    scheduler.reschedule_job("imap", trigger="interval", minutes=max(1, settings.imap_poll_minutes))


def utcnow_local():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(settings.timezone)) + timedelta(seconds=15)


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
