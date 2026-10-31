"""« Suivez le lien de l'e-mail que vous avez reçu de … » : le lien est dans une réponse précédente (cas Branch)."""
import html
from email.message import EmailMessage

import pytest

from app import confirm, mailer, service
from app.config import settings
from app.db import NEEDS_ACTION, SENT, Broker, Event, Profile, Request, SessionLocal

VERIFY = ("De : Branch Privacy Team <privacy-requests@branch-ex.example>\nDate : hier\nObjet : Privacy Request: Verify Your Email\n\n"
          "Please verify your email address by following this link:\nhttps://branch-ex.datagrail.io/verification?token=abc123\n"
          "You are receiving this email because someone submitted a privacy data subject request.")
FOLLOW = ("Hello,\nWe have recorded your request to opt out of the sale and sharing of your personal information. "
          "We have submitted your requests for data deletion. Please verify your identity by following the prompt in "
          "the email you received from privacy-requests@branch-ex.example. Once you verify your identity, we will respond "
          "within the timeframe required under applicable law.\nBest,")


def _request(slug, events=()):
    with SessionLocal() as db:
        b = Broker(slug=slug, name="Branch", email=f"privacy@{slug}.example", web=f"https://{slug}.example",
                   countries=["fr"], categories=[])
        p = Profile(full_name="Lien Test", emails=["moi@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add_all([b, p])
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference=f"OUBLI-{slug.upper().replace('-', '')[-6:]}", status="sent")
        db.add(r)
        db.flush()
        for kind, summary, body in events:
            db.add(Event(request_id=r.id, kind=kind, summary=summary, body=body))
        db.commit()
        return r.id, r.reference


def test_follow_the_prompt_is_a_confirmation_request():
    m = mailer.ParsedMail(uid="", from_addr="privacy@branch.io", from_name="", subject="Re: x", message_id="", text=FOLLOW)
    assert mailer.classify(m).kind == "confirm_link"


@pytest.fixture
def clicks(monkeypatch):
    done = []

    def fake_click(url, domains=None):
        done.append(url)
        return confirm.ClickResult(True, url, "page de confirmation affichée")
    monkeypatch.setattr(confirm, "click", fake_click)
    monkeypatch.setattr(settings, "auto_confirm_links", True)
    return done


def _deliver(rid, ref, slug, text):
    with SessionLocal() as db:
        m = EmailMessage()
        m["From"] = f"privacy@{slug}.example"
        m["Authentication-Results"] = f"mx.test; dkim=pass header.d={slug}.example; dmarc=pass header.from={slug}.example"
        m["Subject"] = f"Re: Request for erasure – Ref. {ref}"
        m["Message-ID"] = f"<f1@{slug}.example>"
        m.set_content(text)
        service.process_incoming(db, mailer.parse_mail(bytes(m)))
        db.commit()
        return db.get(Request, rid).status


def test_link_from_the_earlier_message_is_opened(client, clicks):
    rid, ref = _request("branch-ex", [("reply", "Réponse reçue", VERIFY.replace("branch-ex.datagrail", "branch-ex.datagrail"))])
    assert _deliver(rid, ref, "branch-ex", FOLLOW.replace("branch-ex", "branch-ex")) == SENT
    assert clicks == ["https://branch-ex.datagrail.io/verification?token=abc123"]


def test_page_offers_the_earlier_link_when_not_opened(client, monkeypatch):
    monkeypatch.setattr(settings, "auto_confirm_links", False)
    rid, ref = _request("branch-ey", [("reply", "Réponse reçue", VERIFY.replace("branch-ex", "branch-ey"))])
    assert _deliver(rid, ref, "branch-ey", FOLLOW.replace("branch-ex", "branch-ey")) == NEEDS_ACTION
    page = html.unescape(client.get(f"/tasks?id={rid}").text)
    assert "https://branch-ey.datagrail.io/verification?token=abc123" in page


def test_historical_request_shows_the_link_from_the_earlier_message(client):
    """Demande classée avant cette version : aucun événement « lien à ouvrir », on retrouve le lien quand même."""
    rid, _ = _request("branch-ev", [
        ("reply", "Réponse reçue", VERIFY.replace("branch-ex", "branch-ev")),
        ("reply", "Réponse reçue", "De : Privacy <privacy@branch-ev.example>\nDate : hier\nObjet : Re: x\n\n" + FOLLOW),
        ("status", "Classement automatique : Action requise (« identity verification »)", None),
    ])
    with SessionLocal() as db:
        db.get(Request, rid).status = NEEDS_ACTION
        db.commit()
    page = html.unescape(client.get(f"/tasks?id={rid}").text)
    assert "https://branch-ev.datagrail.io/verification?token=abc123" in page
    assert "Privacy Request: Verify Your Email" in page and "branch-ev.datagrail.io" in page


def test_already_opened_link_is_not_offered_again(client):
    rid, _ = _request("branch-ez", [
        ("reply", "Réponse reçue", VERIFY.replace("branch-ex", "branch-ez")),
        ("confirm", "Lien de confirmation ouvert automatiquement", "https://branch-ez.datagrail.io/verification?token=abc123"),
    ])
    with SessionLocal() as db:
        assert service.earlier_confirm_link(db.get(Request, rid)) is None


def test_link_outside_the_broker_domains_is_never_used(client):
    rid, _ = _request("branch-ew", [("reply", "Réponse reçue",
                                     VERIFY.replace("https://branch-ex.datagrail.io", "https://evil.example"))])
    with SessionLocal() as db:
        assert service.earlier_confirm_link(db.get(Request, rid)) is None
