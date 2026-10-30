"""Confirmation depuis une autre adresse du profil (cas réel : smartclip), et orthographe sans accents."""
import html

import pytest

from app import letters, mailboxes, mailer, oauth_ms, service
from app.config import settings
from app.db import NEEDS_ACTION, SENT, Broker, Mailbox, Profile, Request, SessionLocal

SMARTCLIP = """Dear Mr Deschênes,
We have completed our checks for the email address
moi@exemple.fr, from which your request was submitted.
We could not identify any matching personal data associated with this email
address in the systems reviewed.
Your request also refers to the following email address:
autre.adresse@hotmail.example
Before proceeding with checks concerning personal data associated with this
additional address, we would like to establish that it belongs to you.
We therefore ask you to send a brief confirmation from autre.adresse@hotmail.example
to privacy@confirm-a.example, confirming that you wish to exercise your erasure
and objection rights in relation to this address.
Your request will remain open while the outstanding address is clarified."""


def _request(slug, with_mailbox=True):
    with SessionLocal() as db:
        b = Broker(slug=slug, name=f"Courtier {slug}", email=f"dpo@{slug}.example", web=f"https://{slug}.example",
                   countries=["fr"], categories=[])
        p = Profile(full_name="Olivier Deschênes", emails=["moi@exemple.fr", "autre.adresse@hotmail.example"],
                    phones=[], addresses=[], other_names=[])
        db.add_all([b, p])
        db.query(Mailbox).filter(Mailbox.address == "autre.adresse@hotmail.example").delete()
        if with_mailbox:
            db.add(Mailbox(label="Hotmail", address="autre.adresse@hotmail.example", provider="outlook",
                           host="outlook.office365.com", auth="oauth_ms", username="autre.adresse@hotmail.example"))
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference=f"OUBLI-{slug.upper().replace('-', '')[-6:]}", status="sent",
                    language="en")
        db.add(r)
        db.commit()
        return r.id, r.reference


def test_classified_and_planned():
    m = mailer.ParsedMail(uid="", from_addr="privacy@confirm-a.example", from_name="", subject="Re: x", message_id="",
                          text=SMARTCLIP)
    assert mailer.classify(m).kind == "confirm_from_address"


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(mailboxes, "send_from", lambda mb, msg, db=None: out.append((mb.address, msg)))
    monkeypatch.setattr(settings, "auto_replies", True)
    return out


def _deliver(rid, ref, slug):
    from email.message import EmailMessage
    with SessionLocal() as db:
        m = EmailMessage()
        m["From"] = f"privacy@{slug}.example"
        m["Authentication-Results"] = f"mx.test; dkim=pass header.d={slug}.example; dmarc=pass header.from={slug}.example"
        m["Subject"] = f"Re: Request for erasure – Ref. {ref}"
        m["Message-ID"] = f"<c1@{slug}.example>"
        m.set_content(SMARTCLIP.replace("confirm-a", slug))
        service.process_incoming(db, mailer.parse_mail(bytes(m)))
        db.commit()
        r = db.get(Request, rid)
        return r.status, [e.summary for e in r.events]


def test_sent_automatically_from_the_other_mailbox(client, sent):
    rid, ref = _request("confirm-a")
    status, events = _deliver(rid, ref, "confirm-a")
    assert status == SENT and any("confirmation envoyée depuis autre.adresse@hotmail.example" in e for e in events)
    address, msg = sent[0]
    assert address == "autre.adresse@hotmail.example"
    assert "autre.adresse@hotmail.example" in msg["From"] and msg["To"] == "privacy@confirm-a.example"
    assert msg["Reply-To"] == settings.smtp_from  # les réponses reviennent à l'adresse suivie par l'appli
    assert msg["In-Reply-To"] == "<c1@confirm-a.example>" and "erasure" in msg.get_content()


def test_without_mailbox_it_waits_for_the_user(client, sent):
    rid, ref = _request("confirm-b", with_mailbox=False)
    status, _ = _deliver(rid, ref, "confirm-b")
    assert status == NEEDS_ACTION and not sent
    page = html.unescape(client.get(f"/tasks?id={rid}").text)
    assert "À envoyer depuis autre.adresse@hotmail.example" in page and "Copier le texte" in page


def test_button_sends_it(client, sent, monkeypatch):
    monkeypatch.setattr(settings, "auto_replies", False)
    rid, ref = _request("confirm-c")
    status, _ = _deliver(rid, ref, "confirm-c")
    assert status == NEEDS_ACTION and not sent
    assert "Envoyer la confirmation depuis autre.adresse@hotmail.example" in html.unescape(client.get(f"/tasks?id={rid}").text)
    client.post(f"/requests/{rid}/confirm-from", data={"next": "/tasks"})
    assert sent and sent[0][1]["To"] == "privacy@confirm-c.example"
    with SessionLocal() as db:
        assert db.get(Request, rid).status == SENT


def test_only_profile_addresses_are_used(client, sent):
    """Une adresse inconnue du profil n'est jamais utilisée comme expéditeur."""
    rid, ref = _request("confirm-d")
    with SessionLocal() as db:
        r = db.get(Request, rid)
        plan = service.confirm_from_plan(db, r, "Re", SMARTCLIP.replace("autre.adresse@hotmail.example", "x@ailleurs.example"))
    assert plan is None


def test_sending_servers_and_microsoft_permission():
    assert mailboxes.smtp_server(Mailbox(address="a@gmail.com", provider="gmail", auth="password", host="imap.gmail.com"))[:3] \
        == ("smtp.gmail.com", 465, "ssl")
    assert mailboxes.smtp_server(Mailbox(address="a@hotmail.fr", provider="outlook", auth="oauth_ms", host="x"))[:3] \
        == ("smtp-mail.outlook.com", 587, "starttls")
    assert mailboxes.smtp_server(Mailbox(address="a@x.fr", provider="imap", auth="password", host="imap.x.fr", verify_tls=True))[:2] \
        == ("smtp.x.fr", 587)
    assert "SMTP.Send" in oauth_ms.CONNECT_SCOPE and "SMTP.Send" not in oauth_ms.SCOPE  # la relève ne dépend pas de l'envoi


def test_letters_also_give_the_name_without_accents(client):
    with SessionLocal() as db:
        p = Profile(full_name="Olivier Deschênes", emails=["moi@exemple.fr"], phones=[], addresses=[], other_names=["Olivier Deschênes-Tremblay"])
        assert letters.name_variants(p) == ["Olivier Deschênes-Tremblay", "Olivier Deschenes", "Olivier Deschenes-Tremblay"]
        p2 = Profile(full_name="Jean Martin", emails=[], phones=[], addresses=[], other_names=[])
        assert letters.name_variants(p2) == []
        b = db.query(Broker).first()
        for lang in ("fr", "en"):
            _, body = letters.erasure_letter(p, b, "OUBLI-ACCENT", lang)
            assert "Olivier Deschenes" in body and "Olivier Deschênes" in body
        db.rollback()
