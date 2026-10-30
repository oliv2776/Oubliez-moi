import json
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app import backup, crypto, settings_store
from app.config import settings
from app.db import KV, SessionLocal


FORM = {"_settings_form": "1", "tab": "securite", "dry_run": "true", "send_per_hour": "30",
        "smtp_host": "smtp.exemple.org", "smtp_from": "rgpd@exemple.org", "smtp_user": "rgpd@exemple.org",
        "backup_schedule": "daily", "backup_keep": "3", "backup_encrypt": "true"}


def test_secrets_are_encrypted_at_rest(client):
    client.post("/settings", data={**FORM, "smtp_password": "Sup3r-Secret!"})
    with SessionLocal() as db:
        raw = db.get(KV, "cfg:smtp_password").value
    assert "Sup3r-Secret" not in raw and json.loads(raw).startswith("enc:v1:")
    assert settings.smtp_password == "Sup3r-Secret!"
    settings.smtp_password = ""
    settings_store.apply_overrides()
    assert settings.smtp_password == "Sup3r-Secret!"


def test_plaintext_secrets_from_older_versions_are_migrated(client):
    with SessionLocal() as db:
        db.merge(KV(key="cfg:imap_password", value=json.dumps("ancien-en-clair")))
        db.commit()
    assert settings_store.apply_overrides() >= 1
    with SessionLocal() as db:
        assert json.loads(db.get(KV, "cfg:imap_password").value).startswith("enc:v1:")
    assert settings.imap_password == "ancien-en-clair"
    with SessionLocal() as db:  # nettoyage pour les autres tests
        db.delete(db.get(KV, "cfg:imap_password"))
        db.commit()
    settings.imap_password = ""


def test_crypto_roundtrip_and_wrong_key():
    token = crypto.encrypt("bonjour")
    assert token.startswith("enc:v1:") and crypto.decrypt(token) == "bonjour"
    assert crypto.decrypt("valeur-en-clair") == "valeur-en-clair"
    assert crypto.encrypt("") == ""
    from cryptography.fernet import Fernet
    other = "enc:v1:" + Fernet(Fernet.generate_key()).encrypt(b"x").decode()
    before = crypto.decrypt_errors
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(other)
    assert crypto.decrypt_errors == before + 1


@pytest.fixture()
def backup_dir():
    d = Path(tempfile.mkdtemp(prefix="oubli-backups-"))
    old = settings.backup_dir
    settings.backup_dir = d
    yield d
    settings.backup_dir = old


def test_authentication_results_requires_trusted_authserv_and_dmarc_pass():
    from email.message import EmailMessage
    from app import mailer
    from app.config import settings
    previous = settings.imap_authserv_id
    settings.imap_authserv_id = "mx.test"
    try:
        forged = EmailMessage()
        forged["From"] = "privacy@example.org"
        forged["Authentication-Results"] = "evil.example; spf=pass; dkim=pass; dmarc=pass"
        forged.set_content("Your data has been deleted.")
        assert mailer.parse_mail(bytes(forged)).dmarc_pass is False

        authenticated = EmailMessage()
        authenticated["From"] = "privacy@example.org"
        authenticated["Authentication-Results"] = "mx.test; spf=pass; dkim=pass; dmarc=pass"
        authenticated.set_content("Your data has been deleted.")
        assert mailer.parse_mail(bytes(authenticated)).dmarc_pass is True

        duplicated = EmailMessage()
        duplicated["From"] = "privacy@example.org"
        duplicated["Authentication-Results"] = "mx.test; spf=pass; dkim=pass; dmarc=pass"
        duplicated["Authentication-Results"] = "mx.test; spf=fail; dkim=fail; dmarc=fail"
        duplicated.set_content("Your data has been deleted.")
        assert mailer.parse_mail(bytes(duplicated)).dmarc_pass is False

        failed = EmailMessage()
        failed["From"] = "privacy@example.org"
        failed["Authentication-Results"] = "mx.test; spf=fail; dkim=fail; dmarc=fail"
        failed.set_content("Your data has been deleted.")
        assert mailer.parse_mail(bytes(failed)).dmarc_pass is False
    finally:
        settings.imap_authserv_id = previous


def test_wrong_key_does_not_overwrite_secret_settings(client):
    from cryptography.fernet import Fernet
    with SessionLocal() as db:
        db.merge(KV(key="cfg:smtp_password", value=json.dumps("enc:v1:" + Fernet(Fernet.generate_key()).encrypt(b"secret").decode())))
        db.commit()
    previous = settings.smtp_password
    settings_store.apply_overrides()
    assert settings.smtp_password == previous
    with SessionLocal() as db:
        assert json.loads(db.get(KV, "cfg:smtp_password").value).startswith("enc:v1:")


def test_csrf_rejects_missing_origin():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anon:
        response = anon.post("/login", data={"username": "admin", "password": "test-password"})
        assert response.status_code == 403


def test_csrf_rejects_cross_origin_post():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anon:
        response = anon.post("/login", headers={"Origin": "https://attacker.example"},
                             data={"username": "admin", "password": "test-password"})
        assert response.status_code == 403


def test_backup_encrypted_restore_and_rotation(client, backup_dir):
    settings.backup_encrypt, settings.backup_keep = True, 2
    paths = [backup.create() for _ in range(3)]
    manual = backup.create(manual=True)
    names = {b["name"] for b in backup.list_backups()}
    assert manual.name in names and paths[0].name not in names  # rotation : 2 automatiques + la manuelle
    data = manual.read_bytes()
    assert not data.startswith(b"SQLite format 3")  # illisible sans la clé
    assert backup.read_backup(manual).startswith(b"SQLite format 3")
    with pytest.raises(backup.BackupError):
        backup.read_backup(manual, key="pas-la-bonne-cle")
    # Restauration dans une base de test (pas la vraie)
    target = backup_dir / "restored.db"
    old = backup.DB_PATH
    backup.DB_PATH = target
    try:
        backup.restore(manual)
        import sqlite3
        con = sqlite3.connect(target)
        assert con.execute("select count(*) from brokers").fetchone()[0] > 100
        con.close()
    finally:
        backup.DB_PATH = old


def test_backup_route_and_errors(client, backup_dir):
    r = client.post("/settings/backup-now", data=FORM, follow_redirects=True)
    assert "Sauvegarde créée" in r.text and backup.list_backups()
    page = client.get("/settings")
    assert "Sécurité et sauvegardes" in page.text and "chiffrée" in page.text
    assert client.get("/settings/encryption-key").json()["key"]
    settings.backup_dir = backup_dir / "absent"
    r = client.post("/settings/backup-now", data=FORM, follow_redirects=True)
    assert "Sauvegarde impossible" in r.text and "BACKUP_PATH" in r.text
