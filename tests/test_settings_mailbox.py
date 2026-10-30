"""La boîte configurée dans les réglages (relève des réponses) apparaît dans « Boîtes mail »."""
import pytest
from sqlalchemy import or_, select

from app import crypto, mailboxes
from app.config import settings
from app.db import KV, Mailbox, SessionLocal

ADDRESS = "demandes@gmail.com"


@pytest.fixture
def imap_settings(client, monkeypatch):
    values = {"imap_host": "imap.gmail.com", "imap_port": 993, "imap_security": "ssl", "imap_user": "", "imap_password": "",
              "smtp_user": ADDRESS, "smtp_password": "mot-de-passe-application", "smtp_from": ADDRESS,
              "imap_folders": ["INBOX", "[Gmail]/Spam"]}
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)
    yield
    with SessionLocal() as db:
        for mb in db.scalars(select(Mailbox).where(or_(Mailbox.linked_settings.is_(True), Mailbox.address.like("demandes%")))):
            db.delete(mb)
        db.query(KV).filter(KV.key == mailboxes.SETTINGS_DISMISSED).delete()
        db.commit()


def _linked():
    with SessionLocal() as db:
        mb = db.scalar(select(Mailbox).where(Mailbox.linked_settings.is_(True)))
        if mb:
            db.expunge(mb)
        return mb


def test_settings_mailbox_is_listed_but_nothing_is_read(client, imap_settings, monkeypatch):
    started = []
    monkeypatch.setattr(mailboxes, "scan_in_background", started.append)
    page = client.get("/mailboxes").text
    assert ADDRESS in page and "Boîte des demandes" in page and "Pas encore analysée" in page
    mb = _linked()
    assert mb and mb.auto_scan is False and mb.secret_enc == "" and mb.username == ""  # aucun identifiant copié
    assert mb.folders == ["INBOX", "[Gmail]/Spam"] and mb.provider == "gmail"
    assert not started
    client.get("/mailboxes")
    with SessionLocal() as db:  # pas de doublon d'un affichage à l'autre
        assert db.scalar(select(Mailbox.id).where(Mailbox.linked_settings.is_(True)).offset(1)) is None


def test_connection_reads_settings_each_time(client, imap_settings, monkeypatch):
    logins = []

    class FakeSSL:
        def __init__(self, host, port, ssl_context=None, timeout=None):
            self.host, self.port = host, port

        def login(self, user, password):
            logins.append((self.host, self.port, user, password))

    monkeypatch.setattr(mailboxes, "_PinnedIMAP4_SSL", FakeSSL)
    client.get("/mailboxes")
    mb = _linked()
    mailboxes.connect(mb)
    monkeypatch.setattr(settings, "smtp_password", "nouveau-mot-de-passe")  # changé dans Réglages
    mailboxes.connect(mb)
    assert logins == [("imap.gmail.com", 993, ADDRESS, "mot-de-passe-application"),
                      ("imap.gmail.com", 993, ADDRESS, "nouveau-mot-de-passe")]
    monkeypatch.setattr(settings, "imap_host", "")
    with pytest.raises(mailboxes.MailboxError, match="Réglages"):
        mailboxes.connect(mb)


def test_no_duplicate_when_same_address_added_by_hand(client, imap_settings):
    with SessionLocal() as db:
        db.add(Mailbox(label="Gmail perso", address=ADDRESS, provider="gmail", host="imap.gmail.com", port=993,
                       security="ssl", auth="password", secret_enc=crypto.encrypt("x"), folders=["INBOX"], state={}))
        db.commit()
    page = client.get("/mailboxes").text
    assert _linked() is None
    assert "Gmail perso" in page and "Aussi la boîte qui relève les réponses des courtiers" in page


def test_remove_then_restore(client, imap_settings):
    client.get("/mailboxes")
    mid = _linked().id
    r = client.post(f"/mailboxes/{mid}/delete", follow_redirects=True)
    assert "reste utilisée pour relever les réponses" in r.text
    assert _linked() is None
    page = client.get("/mailboxes").text
    assert _linked() is None and "L'ajouter à la liste" in page  # ne réapparaît pas toute seule
    client.post("/mailboxes/settings-account/restore")
    assert _linked() is not None


def test_edit_changes_only_analysis_options(client, imap_settings):
    client.get("/mailboxes")
    mid = _linked().id
    assert "Réglages › Relève des réponses" in client.get(f"/mailboxes/{mid}/edit").text
    client.post(f"/mailboxes/{mid}", data={"label": "Gmail des demandes", "host": "evil.example", "address": "autre@evil.example",
                                           "folders": "INBOX", "months": "24", "file_folder": "Pubs", "auto_scan": "1"})
    mb = _linked()
    assert (mb.label, mb.folders, mb.months, mb.file_folder, mb.auto_scan) == ("Gmail des demandes", ["INBOX"], 24, "Pubs", True)
    assert (mb.host, mb.address) == ("imap.gmail.com", ADDRESS)


def test_first_scan_turns_on_nightly_analysis(client, imap_settings, monkeypatch):
    monkeypatch.setattr(mailboxes, "scan_in_background", lambda mid: None)
    client.get("/mailboxes")
    mid = _linked().id
    client.post(f"/mailboxes/{mid}/scan")
    assert _linked().auto_scan is True


def test_account_change_follows_settings(client, imap_settings, monkeypatch):
    client.get("/mailboxes")
    with SessionLocal() as db:
        mb = db.scalar(select(Mailbox).where(Mailbox.linked_settings.is_(True)))
        mb.state = {"INBOX": "42:100"}
        db.commit()
    monkeypatch.setattr(settings, "smtp_user", "demandes2@gmail.com")
    monkeypatch.setattr(settings, "smtp_from", "demandes2@gmail.com")
    client.get("/mailboxes")
    mb = _linked()
    assert mb.address == "demandes2@gmail.com" and mb.state == {}  # autre compte : l'analyse repart de zéro
