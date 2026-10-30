"""Authentification des réponses : formats réels (Gmail, Outlook) et détection de l'authserv-id."""
import pytest

from app import mailer, settings_store
from app.config import settings
from app.db import SessionLocal, kv_get
from app.main import _authserv_feedback

GMAIL_DIRECT = (
    "mx.google.com;\r\n       dkim=pass header.i=@acxiom.com header.s=s1 header.b=AbCdEf;\r\n"
    "       spf=pass (google.com: domain of privacy@acxiom.com designates 1.2.3.4 as permitted sender) "
    "smtp.mailfrom=privacy@acxiom.com;\r\n"
    "       dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=acxiom.com"
)
# Réponse envoyée via un outil de tickets : SPF sur le domaine du prestataire, DKIM aligné.
GMAIL_VIA_ESP = (
    "mx.google.com;\r\n       dkim=pass header.i=@acxiom.com header.s=zendesk1 header.b=XyZ;\r\n"
    "       spf=pass (google.com: domain of bounce+abc@em1234.zendesk.com designates 1.2.3.4 as permitted sender) "
    "smtp.mailfrom=\"bounce+abc@em1234.zendesk.com\";\r\n"
    "       dmarc=pass (p=QUARANTINE sp=NONE dis=NONE) header.from=acxiom.com"
)
GMAIL_ESP_UNALIGNED = (
    "mx.google.com; dkim=pass header.i=@zendesk.com header.s=z1; "
    "spf=pass smtp.mailfrom=bounce@em1234.zendesk.com; dmarc=pass header.from=acxiom.com"
)
OUTLOOK = (
    "spf=pass (sender IP is 1.2.3.4) smtp.mailfrom=acxiom.com; dkim=pass (signature was verified) "
    "header.d=acxiom.com;dmarc=pass action=none header.from=acxiom.com;compauth=pass reason=100"
)


def _mail(auth_results: str, sender: str = "privacy@acxiom.com") -> bytes:
    """Message brut, avec l'en-tête plié sur plusieurs lignes comme le fait Gmail."""
    return (f"Authentication-Results: {auth_results}\r\nFrom: {sender}\r\nTo: moi@gmail.com\r\n"
            "Subject: Re: demande\r\nContent-Type: text/plain\r\n\r\nYour data has been deleted.\r\n").encode()


@pytest.fixture
def gmail_trust():
    previous = settings.imap_authserv_id
    settings.imap_authserv_id = "mx.google.com"
    yield
    settings.imap_authserv_id = previous


def test_gmail_direct_and_via_ticketing_tool(gmail_trust):
    assert mailer.parse_mail(_mail(GMAIL_DIRECT)).dmarc_pass is True
    # Gmail n'écrit que header.i=@domaine : il doit suffire pour l'alignement DKIM.
    assert mailer.parse_mail(_mail(GMAIL_VIA_ESP)).dmarc_pass is True


def test_gmail_unaligned_signature_rejected(gmail_trust):
    assert mailer.parse_mail(_mail(GMAIL_ESP_UNALIGNED)).dmarc_pass is False


def test_header_without_authserv_id_is_never_trusted(gmail_trust):
    assert mailer.authserv_id(OUTLOOK) == ""
    assert mailer.authserv_id(GMAIL_DIRECT) == "mx.google.com"
    assert mailer.parse_mail(_mail(OUTLOOK)).dmarc_pass is False


class _FakeIMAP:
    def __init__(self, headers: list[list[str]]):
        self.headers = headers

    def select(self, folder, readonly=False):
        assert folder == "INBOX" and readonly
        return "OK", [b""]

    def uid(self, cmd, *args):
        if cmd == "search":
            return "OK", [" ".join(str(i + 1) for i in range(len(self.headers))).encode()]
        assert "BODY.PEEK" in args[1]  # lecture seule : aucun indicateur modifié
        out = []
        for i, values in enumerate(self.headers):
            raw = "".join(f"Authentication-Results: {v}\r\n" for v in values) + "\r\n"
            out += [(f"{i + 1} (UID {i + 1} BODY[HEADER.FIELDS (AUTHENTICATION-RESULTS)] {{{len(raw)}}}".encode(), raw.encode()), b")"]
        return "OK", out

    def logout(self):
        pass


def _detect(monkeypatch, headers):
    monkeypatch.setattr(mailer, "_imap_connect", lambda: _FakeIMAP(headers))
    return mailer.detect_authserv_ids()


def test_detection_picks_the_receiving_server(monkeypatch):
    headers = [[GMAIL_DIRECT]] * 18 + [["evil.example; dmarc=pass header.from=x.example", GMAIL_DIRECT]] + [[]]
    res = _detect(monkeypatch, headers)
    assert res["scanned"] == 20 and res["suggestion"] == "mx.google.com" and dict(res["ids"])["mx.google.com"] == 19


def test_detection_outlook_has_no_identifier(monkeypatch):
    res = _detect(monkeypatch, [[OUTLOOK]] * 10)
    assert res["suggestion"] == "" and res["without_id"] == 10
    message, kind = _authserv_feedback(res)
    assert kind == "warn" and "n'indique pas d'identifiant" in message


def test_detection_without_majority(monkeypatch):
    res = _detect(monkeypatch, [["a.example; dmarc=pass"]] * 3 + [["b.example; dmarc=pass"]] * 3)
    assert res["suggestion"] == ""
    assert "Aucun identifiant ne domine" in _authserv_feedback(res)[0]


def test_feedback_fills_empty_setting_but_never_overwrites(client, monkeypatch):
    previous = settings.imap_authserv_id
    res = {"scanned": 20, "ids": [("mx.google.com", 19)], "without_id": 0, "suggestion": "mx.google.com"}
    try:
        settings.imap_authserv_id = ""
        message, kind = _authserv_feedback(res)
        assert kind == "ok" and "enregistré" in message and settings.imap_authserv_id == "mx.google.com"
        with SessionLocal() as db:  # la valeur est bien persistée en base
            assert kv_get(db, "cfg:imap_authserv_id") == '"mx.google.com"'
        assert _authserv_feedback(res)[1] == "ok"
        settings.imap_authserv_id = "mx.autre.example"
        message, kind = _authserv_feedback(res)
        assert kind == "error" and settings.imap_authserv_id == "mx.autre.example"
    finally:
        settings_store.set_value("imap_authserv_id", previous)


def test_authserv_setting_validated_from_form(client):
    form = {"_settings_form": "1", "tab": "releve", "dry_run": "true"}
    try:
        r = client.post("/settings", data={**form, "imap_authserv_id": "  MX.Google.com "}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/settings#releve"
        assert settings.imap_authserv_id == "mx.google.com"
        client.post("/settings", data={**form, "imap_authserv_id": "mx google; dmarc=pass"})
        assert settings.imap_authserv_id == "mx.google.com"  # valeur invalide refusée
    finally:
        settings_store.reset_to_env()
