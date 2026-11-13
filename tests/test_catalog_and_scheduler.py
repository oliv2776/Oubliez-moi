"""Catalogue des courtiers (lecture de l'archive Datenanfragen, mise à jour) et tâches planifiées."""
import io
import json
import logging
import zipfile
from datetime import timedelta

from app import brokers, scheduler, service
from app.config import settings
from app.db import FAILED, QUEUED, Broker, Profile, Request, SessionLocal, kv_get, kv_set, utcnow
from tools import replay_export

ADS = {"slug": "acme-ads", "name": "Acme Ads", "categories": ["ads"], "email": " dpo@acme-ads.example ",
       "relevant-countries": ["fr"], "needs-id-document": True, "web": "https://acme-ads.example"}


def _zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content if isinstance(content, str) else json.dumps(content))
    return buf.getvalue()


def test_normalize_keeps_only_reachable_brokers():
    rec = brokers.normalize_record(ADS)
    assert rec["email"] == "dpo@acme-ads.example" and rec["needs_id"] and rec["countries"] == ["fr"]
    assert brokers.normalize_record({**ADS, "categories": ["commerce"]}) is None     # pas un courtier en données
    assert brokers.normalize_record({**ADS, "email": "", "webform": ""}) is None      # aucun moyen de le joindre
    assert brokers.normalize_record({**ADS, "slug": ""}) is None
    assert brokers.normalize_record({**ADS, "email": None, "webform": "https://acme-ads.example/form"})["webform"]
    assert brokers.normalize_record({k: v for k, v in ADS.items() if k != "relevant-countries"})["countries"] == ["all"]


def test_parse_zip_ignores_other_files_and_broken_entries():
    data = _zip({
        "data-main/companies/acme-ads.json": ADS,
        "data-main/companies/broken.json": "{pas du json",
        "data-main/companies/shop.json": {"slug": "shop", "name": "Shop", "categories": ["commerce"], "email": "a@shop.example"},
        "data-main/README.md": "x",
        "data-main/companies/sub/ignored.json": ADS,
    })
    assert [b["slug"] for b in brokers.parse_zip(data)] == ["acme-ads"]


def test_parse_directory(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(ADS), encoding="utf-8")
    (tmp_path / "b.json").write_text("{", encoding="utf-8")
    assert [b["name"] for b in brokers.parse_directory(tmp_path)] == ["Acme Ads"]


def test_catalog_update_keeps_user_choices_and_requeues_failed(client):
    rec = brokers.normalize_record(ADS)
    with SessionLocal() as db:
        assert service.sync_brokers(db, [rec])["added"] == 1
        b = db.query(Broker).filter_by(slug="acme-ads").one()
        b.enabled, b.enabled_overridden = False, True           # désactivé à la main
        custom = Broker(slug="perso", name="Mon courtier", email="a@perso.example", source="custom", countries=["fr"], categories=[])
        p = Profile(full_name="Cat Test", emails=["moi@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add_all([custom, p])
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference="OUBLI-CATA01", status=FAILED)
        db.add(r)
        db.commit()
        res = service.sync_brokers(db, [{**rec, "email": "privacy@acme-ads.example"},
                                        {"slug": "perso", "name": "Écrasé ?", "email": "x@y.example", "source": "datenanfragen"}])
        assert res == {"added": 0, "updated": 1, "total": 2}
        db.refresh(b)
        assert b.email == "privacy@acme-ads.example" and b.enabled is False   # votre choix est gardé
        assert db.get(Request, r.id).status == QUEUED                           # nouvelle adresse : on renvoie
        assert db.query(Broker).filter_by(slug="perso").one().name == "Mon courtier"
        db.delete(db.query(Broker).filter_by(slug="acme-ads").one())
        db.delete(db.query(Broker).filter_by(slug="perso").one())
        db.commit()


def test_catalog_auto_update_respects_its_interval(client, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "update_brokers_from_remote", lambda: calls.append(1) or {"added": 0})
    monkeypatch.setattr(settings, "broker_auto_update_days", 0)
    assert scheduler.maybe_update_brokers() is None and not calls            # désactivé
    monkeypatch.setattr(settings, "broker_auto_update_days", 7)
    with SessionLocal() as db:
        before = kv_get(db, "brokers_synced_at")
        kv_set(db, "brokers_synced_at", (utcnow() - timedelta(days=2)).isoformat())
        db.commit()
    assert scheduler.maybe_update_brokers() is None and not calls            # trop récent
    with SessionLocal() as db:
        kv_set(db, "brokers_synced_at", (utcnow() - timedelta(days=9)).isoformat())
        db.commit()
    assert scheduler.maybe_update_brokers() == {"added": 0} and calls        # échu
    with SessionLocal() as db:
        kv_set(db, "brokers_synced_at", before or utcnow().isoformat())
        db.commit()


def test_a_failing_job_is_logged_and_does_not_stop_the_scheduler(caplog):
    def boom():
        raise RuntimeError("panne")
    job = scheduler._safe(boom)
    with caplog.at_level(logging.ERROR):
        job()                                   # ne lève pas
    assert job.__name__ == "boom" and "Tâche boom en erreur" in caplog.text


def test_backup_job_records_its_outcome(client, monkeypatch):
    monkeypatch.setattr(scheduler.backup, "scheduled", lambda: "sauvegarde.db.enc")
    assert scheduler.run_backup() == "sauvegarde.db.enc"
    with SessionLocal() as db:
        assert kv_get(db, "backup_status").endswith("|ok|sauvegarde.db.enc")

    def fail():
        raise OSError("disque plein")
    monkeypatch.setattr(scheduler.backup, "scheduled", fail)
    try:
        scheduler.run_backup()
        raise AssertionError("l'erreur doit remonter au journal")
    except OSError:
        pass
    with SessionLocal() as db:
        assert "|error|disque plein" in kv_get(db, "backup_status")
        kv_set(db, "backup_status", "")
        db.commit()


def test_daily_job_and_disabled_scheduler(client, monkeypatch):
    monkeypatch.setattr(settings, "retention_months", 0)
    assert set(scheduler.daily()) == {"renvois", "nouveaux_courtiers", "messages_effaces"}
    scheduler.start()        # DISABLE_SCHEDULER dans les tests : ne démarre rien
    assert not scheduler.scheduler.running
    scheduler.reschedule()   # sans effet tant qu'il ne tourne pas
    scheduler.shutdown()


def test_replay_tool_reports_changed_classifications():
    data = {"elements": [
        {"statut": "needs_action", "courtier": {"nom": "Acme"}, "classement_enregistre": {"categorie": "needs_action"},
         "reponses": [{"de": "DPO <dpo@acme.example>", "objet": "Re", "texte": "Please use our online form to submit your request."}]},
        {"statut": "sent", "courtier": {"nom": "Bêta"}, "classement_enregistre": {"categorie": "ack"},
         "reponses": [{"de": "<a@beta.example>", "objet": "Re", "texte": "We have received your request."}]},
        {"statut": "failed", "courtier": {"nom": "Sans réponse"}, "reponses": []},
    ]}
    counts, changes = replay_export.replay(data)
    assert counts == {"webform": 1, "ack": 1}
    assert [(c["courtier"], c["enregistre"], c["actuel"]) for c in changes] == [("Acme", "needs_action", "webform")]
