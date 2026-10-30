import json

import pytest
from sqlalchemy import select

from app import mailer, service, settings_store
from app.config import settings
from app.db import KV, QUEUED, SENT, Broker, Profile, Request, SessionLocal, kv_set


FORM = {
    "_settings_form": "1", "tab": "compte",
    "dry_run": "true", "send_per_hour": "20",
    "smtp_from": "rgpd@exemple.org", "smtp_user": "rgpd@exemple.org", "smtp_password": "secret1",
    "smtp_host": "smtp.exemple.org", "smtp_port": "465", "smtp_security": "ssl",
    "imap_host": "imap.exemple.org", "imap_port": "993", "imap_security": "ssl",
    "imap_folders": "INBOX, Spam", "imap_poll_minutes": "5", "imap_user": "", "imap_password": "",
}


def test_settings_saved_from_ui(client):
    r = client.post("/settings", data=FORM, follow_redirects=True)
    assert "Réglages enregistrés" in r.text
    assert settings.smtp_host == "smtp.exemple.org" and settings.smtp_port == 465
    assert settings.imap_folders == ["INBOX", "Spam"] and settings.send_per_hour == 20
    assert settings.imap_login == ("rgpd@exemple.org", "secret1")  # repli sur les identifiants SMTP
    assert settings.smtp_bcc_self is False
    assert "secret1" not in r.text  # le mot de passe n'est jamais réaffiché

    # Mot de passe laissé vide : conservé
    client.post("/settings", data={**FORM, "smtp_password": ""})
    assert settings.smtp_password == "secret1"

    # Rechargé depuis la base (simulation d'un redémarrage)
    settings.smtp_host = "autre"
    settings_store.apply_overrides()
    assert settings.smtp_host == "smtp.exemple.org"


def test_settings_validation(client):
    r = client.post("/settings", data={**FORM, "smtp_from": "pas-une-adresse", "smtp_port": "abc"}, follow_redirects=True)
    assert "adresse e-mail valide" in r.text and "nombre entier" in r.text
    assert settings.smtp_from == "rgpd@exemple.org"  # rien n'a été enregistré


def test_switch_to_real_requeues_simulated(client):
    with SessionLocal() as db:
        p = Profile(full_name="Sim Ulé", emails=["sim@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add(p)
        db.commit()
        b = db.scalars(select(Broker).where(Broker.enabled.is_(True), Broker.email.is_not(None))).first()
        req = service._create_request(db, p, b, cycle=1)
        db.commit()
        rid = req.id
    with SessionLocal() as db:
        service.send_initial(db, db.get(Request, rid))  # dry_run=True -> simulé
        assert db.get(Request, rid).status == SENT
    r = client.post("/settings", data={**FORM, "dry_run": "false"}, follow_redirects=True)
    assert "remise(s) en file" in r.text
    with SessionLocal() as db:
        req = db.get(Request, rid)
        assert req.status == QUEUED and req.sent_at is None and req.message_ids == []


def test_reset_to_env(client):
    client.post("/settings/reset")
    assert settings.smtp_host == "" and settings.dry_run is True and settings.smtp_from == "moi@exemple.fr"


def test_unreadable_encrypted_secret_is_preserved_and_reported(client):
    old_password, old_dry_run = settings.smtp_password, settings.dry_run
    try:
        settings.smtp_password = ""
        settings.dry_run = False
        with SessionLocal() as db:
            kv_set(db, "cfg:smtp_password", json.dumps("enc:v1:not-a-valid-fernet-token"))
            db.commit()

        settings_store.apply_overrides()

        assert settings_store.secret_unavailable("smtp_password")
        assert settings.smtp_password == ""  # ne jamais remplacer silencieusement par une autre valeur
        with SessionLocal() as db:
            row = db.get(KV, "cfg:smtp_password")
            assert row is not None and json.loads(row.value) == "enc:v1:not-a-valid-fernet-token"

        msg = mailer.build_message("privacy@example.org", "Test", "Body", "Test")
        with pytest.raises(mailer.SendError, match="ENCRYPTION_KEY"):
            mailer.send_message(msg)
    finally:
        with SessionLocal() as db:
            row = db.get(KV, "cfg:smtp_password")
            if row:
                db.delete(row)
                db.commit()
        settings_store.DECRYPTION_FAILURES.discard("smtp_password")
        settings.smtp_password, settings.dry_run = old_password, old_dry_run
