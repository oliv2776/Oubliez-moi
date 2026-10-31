"""Questions simples (lien avec l'entreprise, adresse concernée, compte…) : réponse type prête ou envoyée."""
import html
from email.message import EmailMessage

import pytest

from app import letters, mailer, service
from app.config import settings
from app.db import NEEDS_ACTION, SENT, Broker, Event, Profile, Request, SessionLocal

OMNICOM = ("Dear Olivier,\nThank you for your data subject requested dated October 29, 2026. In order to start processing "
           "your erasure of personal data, we do need to confirm your relationship with Omnicom Media Group as either an "
           "employee or consumer. Once you have responded back, we will be able to start processing your request.")
HAVAS = ("Bonjour,\nNous accusons réception de votre demande. Nous vous remercions de bien vouloir nous indiquer les "
         "éléments suivants :\n* Nom et pays de votre agence de publicité ;\n* Campagne concernée ;\n* Période concernée.")
SLS = ("Les données avec lesquelles vous nous avez adressé votre demande ne figure pas dans notre base. Pour répondre "
       "positivement à votre demande pourriez-vous nous confirmer l'adresse email\nconcernée ?")
BITMASTER = ("Nous ne parvenons pas à trouver de compte associé à votre adresse. Pourriez-vous nous indiquer votre nom "
             "d'utilisateur ainsi que l'adresse e-mail utilisée lors de votre inscription ?")
DEMANDBASE = ("In order to proceed, could you please respond by confirming your work e-mail address? We are a B2B company "
              "and do not process personal email addresses.")


def kind(text):
    return mailer.classify(mailer.ParsedMail(uid="", from_addr="a@b.example", from_name="", subject="Re", message_id="", text=text))


@pytest.mark.parametrize("text, topic", [
    (OMNICOM, "relationship"), (HAVAS, "unknown_details"), (SLS, "which_email"), (BITMASTER, "unknown_details"),
    (DEMANDBASE, "unknown_details"),
])
def test_simple_questions(text, topic):
    assert kind(text).kind == "clarification"
    assert mailer.clarification_topic(text) == topic


def test_information_without_question_is_not_a_question():
    assert kind("Our advertising campaigns use cookies. Thank you for contacting us, your request was received.").kind == "ack"


def _request(slug, lang="en"):
    with SessionLocal() as db:
        b = Broker(slug=slug, name="Omnicom Media Group", email=f"privacy@{slug}.example", countries=["fr"], categories=[])
        p = Profile(full_name="Clar Test", emails=["moi@exemple.fr", "autre@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add_all([b, p])
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference=f"OUBLI-{slug.upper().replace('-', '')[-6:]}",
                    status="sent", language=lang)
        db.add(r)
        db.commit()
        return r.id, r.reference


def test_letters_answer_without_new_personal_data(client):
    rid, _ = _request("clar-ll")
    with SessionLocal() as db:
        r = db.get(Request, rid)
        _, rel = letters.auto_reply_letter(r, "clarification", "Re: x", OMNICOM)
        assert "neither an employee nor a customer of Omnicom Media Group" in rel
        r.language = "fr"
        _, which = letters.auto_reply_letter(r, "clarification", "Re: x", SLS)
        assert "moi@exemple.fr ; autre@exemple.fr" in which
        _, unknown = letters.auto_reply_letter(r, "clarification", "Re: x", HAVAS)
        assert "Je ne dispose pas de ces informations" in unknown and "article 12, paragraphe 2" in unknown


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_message", lambda msg, force=False: sent.append(msg))
    return sent


def _deliver(rid, ref, slug, text):
    with SessionLocal() as db:
        m = EmailMessage()
        m["From"] = f"privacy@{slug}.example"
        m["Authentication-Results"] = f"mx.test; dkim=pass header.d={slug}.example; dmarc=pass header.from={slug}.example"
        m["Subject"] = f"Re: Request for erasure – Ref. {ref}"
        m["Message-ID"] = f"<q1@{slug}.example>"
        m.set_content(text)
        service.process_incoming(db, mailer.parse_mail(bytes(m)))
        db.commit()
        return db.get(Request, rid).status


def test_automatic_answer(client, outbox, monkeypatch):
    monkeypatch.setattr(settings, "auto_replies", True)
    rid, ref = _request("clar-aa")
    assert _deliver(rid, ref, "clar-aa", OMNICOM) == SENT
    assert outbox and "neither an employee nor a customer" in outbox[0].get_content()


def test_one_click_answer_when_not_automatic(client, outbox, monkeypatch):
    monkeypatch.setattr(settings, "auto_replies", False)
    rid, ref = _request("clar-bb")
    assert _deliver(rid, ref, "clar-bb", OMNICOM) == NEEDS_ACTION and not outbox
    page = html.unescape(client.get(f"/tasks?id={rid}").text)
    assert "Réponse type prête" in page and "neither an employee nor a customer" in page
    client.post(f"/requests/{rid}/send-proposed", data={"next": "/tasks"})
    assert outbox and outbox[0]["To"] == "privacy@clar-bb.example"
    with SessionLocal() as db:
        r = db.get(Request, rid)
        assert r.status == SENT
        assert service.proposed_reply(r) is None  # déjà envoyée : plus proposée


def test_old_requests_get_the_proposal_too(client, outbox):
    """Demandes classées avant cette version (ex. Tradedoubler : question de pays) : la réponse est proposée."""
    rid, _ = _request("clar-cc")
    with SessionLocal() as db:
        r = db.get(Request, rid)
        r.status = NEEDS_ACTION
        db.add(Event(request_id=rid, kind="reply", summary="Réponse reçue",
                     body="De : Privacy <privacy@clar-cc.example>\nDate : hier\nObjet : Sv: x\n\nHi,\nWhich country did you sign up "
                          "to in order to forward your request to the right team?"))
        db.commit()
        p = service.proposed_reply(db.get(Request, rid))
        assert p["kind"] == "residence_question" and p["to"] == "privacy@clar-cc.example"
