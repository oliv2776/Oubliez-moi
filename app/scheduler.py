"""Tâches périodiques (envoi lissé, relève IMAP, échéances, renvois, mise à jour des courtiers)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import service
from .config import settings
from .db import SessionLocal, kv_get, utcnow

log = logging.getLogger(__name__)
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
    scheduler.start()
    log.info("Planificateur démarré : 1 e-mail toutes les %ss, relève IMAP toutes les %s min", interval, settings.imap_poll_minutes)


def utcnow_local():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(settings.timezone)) + timedelta(seconds=15)


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
