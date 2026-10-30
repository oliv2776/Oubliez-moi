"""Règles de classement tirées de vraies réponses de courtiers (extraits courts, sans données personnelles)."""
from datetime import timedelta
from email.message import EmailMessage

import pytest

from app import mailer, reanalysis, service
from app.config import settings
from app.db import COMPLETED, FAILED, KV, NEEDS_ACTION, SENT, Broker, Event, Profile, Request, SessionLocal, utcnow

OUR_LETTER = (
    "Dear Sir or Madam,\n\nPursuant to Article 17 of the General Data Protection Regulation (GDPR, Regulation (EU) "
    "2016/679), I request that Acme erase all personal data concerning me.\n\nPlease confirm in writing that my data "
    "has been erased, or that you hold no data concerning me."
)


def kind(text, subject="Re: Request for erasure", sender="privacy@acme.example", bounce=False):
    m = mailer.ParsedMail(uid="", from_addr=sender, from_name="", subject=subject, message_id="", text=text, is_bounce=bounce)
    return mailer.classify(m).kind


# ------------------------------------------------------ notre lettre citée ---

@pytest.mark.parametrize("quote", [
    "-----Ursprüngliche Nachricht-----\nVon: Moi <moi@exemple.fr>\nGesendet: Donnerstag, 29. Oktober 2026 22:46\n"
    "An: Privacy <privacy@acme.example>\nBetreff: Request for erasure\n\n" + OUR_LETTER,
    "________________________________\nFrån: Moi <moi@exemple.fr>\nSkickat: den 29 oktober 2026 22:58\nTill: privacy\n"
    "Ämne: Request for erasure\n\n" + OUR_LETTER,
    "________________________________\nVan: Moi <moi@exemple.fr>\nVerzonden: donderdag 29 oktober 2026 21:27\n"
    "Aan: Info\nOnderwerp: Request for erasure\n\n" + OUR_LETTER,
    "-----------------\nOriginal message:\n-----------------\n  " + OUR_LETTER,
    "     On\n\n     Thu, 29 Oct at 11:18 PM\n\n     ,  Moi <moi@exemple.fr>  wrote:\n\n  " + OUR_LETTER,
    "29.10.2026 22:40 (Europe/Berlin) - Moi schrieb:\n" + OUR_LETTER,
    "Subject - Request for erasure\n\nTicket Description - " + OUR_LETTER,  # ticket qui recopie la demande
])
def test_our_quoted_letter_never_closes_the_request(quote):
    """« my data has been erased » vient de notre lettre : la réponse au-dessus est un simple accusé."""
    assert kind("Hi,\n\nWe have passed your request to our Legal team and will email you with an update.\n\n" + quote) == "ack"
    assert "Pursuant" not in mailer.latest_reply_text("Thanks.\n\n" + quote)


def test_question_above_a_swedish_quote():
    quote = "________________________________\nFrån: Moi\nSkickat: igår\nTill: privacy\nÄmne: x\n\n" + OUR_LETTER
    assert kind("Hi,\nWhich country did you sign up to in order to forward your request to the right team?\n\n" + quote) \
        == "residence_question"


# -------------------------------------------------- formulaire ou portail ---

@pytest.mark.parametrize("text", [
    "Thank you for contacting Privacy at Acme. We appreciate your interest in how we use personal data. " * 4
    + "To ask that data be deleted, please click here to fill out a request form. These requests cannot be made "
      "through privacy@acme.example.",
    "Hello and thank you for your email.\nPlease note: this mailbox is no longer monitored, and this is an automated "
    "response. Please use our Privacy Request Center.",
    "Dear Sender,\nThank you for reaching out. Unfortunately, this email inbox is currently nonfunctional and unable "
    "to receive or respond to messages.",
    "If you’re looking to make a data subject privacy request in order to see or delete your data, please visit this page.",
    "Please visit Acme’s Privacy Center to submit your request. Requests made through other channels cannot be processed.",
    "To submit an access or deletion request, you may submit a request through Acme's Privacy Rights Request Portal.",
])
def test_webform_required(text):
    assert kind(text) == "webform"


def test_optional_tool_is_still_an_ack():
    assert kind("Hi there! Thank you for your message. GDPR requests are usually processed within one month.\n\n"
                "You may also use our dedicated privacy tool.") == "ack"


# ------------------------------------------------ identifiant publicitaire ---

@pytest.mark.parametrize("text", [
    "We do not hold your name or email. Further information such as your online identifier is required. "
    "In the absence of such identifiers, we are unable to action your request.",
    "Based on the email address you provided, we do not have any information related to you, however, "
    "we process hashed emails and cookie IDs. We need you to provide us with your Cookie ID.",
    "Diese Kennung ist die einzige Angabe, mit der wir Ihre Anfrage zuordnen können. Bitte teilen Sie uns Ihre "
    "Onlinekennung per E-Mail mit.",
])
def test_device_identifier_requested(text):
    assert kind(text) == "device_id"


# --------------------------------------------- « aucune donnée », mais… ---

@pytest.mark.parametrize("text, expected", [
    ("Les données avec lesquelles vous nous avez adressé votre demande ne figure pas dans notre base de données.\n"
     "Pour répondre positivement à votre demande pourriez-vous nous confirmer l'adresse email concernée ?", "needs_action"),
    ("Nous ne parvenons pas à trouver de compte associé à votre adresse. Pourriez-vous nous indiquer votre nom "
     "d'utilisateur ?", "needs_action"),
    ("We could not identify any matching personal data associated with this email\naddress. Your request also refers "
     "to another address.\nWe therefore ask you to send a brief confirmation from that\naddress.", "needs_action"),
    ("Thank you for your email. We have reviewed your request. We did not identify any such personal data, other than "
     "the records associated with your request.", "completed_no_data"),
    ("Thank you for getting in touch. Therefore, there is no data to be erased.", "completed_no_data"),
    ("Sur cette base, nous n'avons pas pu trouver un enregistrement correspondant dans notre système.", "completed_no_data"),
])
def test_no_data_only_closes_when_nothing_else_is_expected(text, expected):
    assert kind(text) == expected


def test_no_data_shared_is_not_no_data_held():
    assert kind("Catalina ne transmet pas les données de ses utilisateurs. Par conséquent, aucune donnée vous "
                "concernant n'a fait l'objet de communication à autrui.") != "completed_no_data"


def test_identity_document_not_required_is_not_an_id_request():
    assert kind("We do not require an identity document as a standard prerequisite. "
                "Your request will remain open.") != "id_request"
    assert kind("We may need to confirm your identity, in which case we will contact you to establish proof of "
                "your identity. Thank you for submitting your request.") == "ack"


def test_long_courtesy_reply_is_read_not_parked():
    """« Thank you for contacting… » suivi d'explications : à lire, pas un simple accusé."""
    info = ("Thank you for contacting Acme.\n\nWho we are: Acme uses cookies to deliver advertising on partner sites. "
            "Your browser may let you delete or disable cookies. ") * 6
    assert kind(info) == "reply"
    assert kind("Thank you for contacting Acme. Your message has been received.") == "ack"


# --------------------------------------------- messages de non-distribution ---

def test_delay_and_full_mailbox_are_not_failures():
    delay = ("** Message non distribué **\nUn problème temporaire est survenu lors de la distribution de votre message. "
             "D'autres tentatives vont être effectuées pendant 47 heures.")
    assert kind(delay, "Delivery Status Notification (Delay)", "mailer-daemon@googlemail.com", True) == "delivery_issue"
    full = "Delivery has failed to these recipients or groups:\nThe recipient's mailbox is full and can't accept messages now."
    assert kind(full, "Undeliverable: x", "postmaster@acme.example", True) == "delivery_issue"
    gone = "Your message to privacy@acme.example couldn't be delivered. privacy wasn't found at acme.example."
    assert kind(gone, "Undeliverable: x", "postmaster@acme.example", True) == "bounce"


def test_bounced_recipients():
    assert mailer.bounced_recipients(
        "Delivery has failed to these recipients or groups:\n\njdoe@acme.com<mailto:jdoe@acme.com>\nThe email system...") \
        == {"jdoe@acme.com"}
    assert mailer.bounced_recipients("Your message to privacy@acme.example couldn't be delivered.") == {"privacy@acme.example"}


def test_delivery_status_part_is_readable_text():
    m = EmailMessage()
    m["From"] = "mailer-daemon@googlemail.com"
    m["Subject"] = "Delivery Status Notification (Failure)"
    m.set_content("Address not found")
    report = EmailMessage()
    report["Content-Type"] = "message/delivery-status"
    m.make_mixed()
    m.attach(report)
    m.get_payload()[1].set_payload([EmailMessage(), EmailMessage()])
    m.get_payload()[1].get_payload()[0]["Reporting-MTA"] = "dns; mx.example"
    text = mailer.parse_mail(bytes(m)).text
    assert "EmailMessage object" not in text and "Reporting-MTA" in text


# --------------------------------------------------------- dans le service ---

def _request(slug, email, web=None, status=SENT, last_error=None):
    with SessionLocal() as db:
        b = Broker(slug=slug, name=f"Courtier {slug}", email=email, web=web, countries=["fr"], categories=[])
        p = Profile(full_name="Regle Test", emails=["moi@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add_all([b, p])
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference=f"OUBLI-{slug.upper()[:6]}", status=status,
                    sent_at=utcnow() - timedelta(days=1), due_at=utcnow() + timedelta(days=29), last_error=last_error)
        db.add(r)
        db.commit()
        return r.id, r.reference


def _deliver(rid, sender, subject, body, msgid):
    with SessionLocal() as db:
        m = EmailMessage()
        m["From"] = sender
        domain = sender.rsplit("@", 1)[-1]
        m["Authentication-Results"] = f"mx.test; dkim=pass header.d={domain}; dmarc=pass header.from={domain}"
        m["Subject"] = subject
        m["Message-ID"] = msgid
        m.set_content(body)
        service.process_incoming(db, mailer.parse_mail(bytes(m)))
        db.commit()
        r = db.get(Request, rid)
        return r.status, r.last_error, [e.summary for e in r.events]


def test_failure_of_a_list_member_keeps_the_request_waiting(client):
    rid, ref = _request("listea", "privacy@liste-a.example", web="https://www.liste-a.example")
    status, error, events = _deliver(
        rid, "postmaster@liste-a.example", f"Undeliverable: Request for erasure – Ref. {ref}",
        "Delivery has failed to these recipients or groups:\n\njdoe@liste-a.example<mailto:jdoe@liste-a.example>\n"
        "The email system had a problem processing this message. It won't try to deliver this message again.",
        "<dsn1@liste-a.example>")
    assert status == SENT and error is None
    assert any("membre de la liste" in e for e in events)


def test_reply_after_a_bounce_restores_the_request(client):
    rid, ref = _request("rebondb", "privacy@rebond-b.example", status=FAILED, last_error=service.BOUNCE_ERROR)
    status, error, events = _deliver(
        rid, "privacy@rebond-b.example", f"Automatic reply: Request for erasure – Ref. {ref}",
        "Thank you for contacting the privacy team. We have received your request.", "<ack1@rebond-b.example>")
    assert status == SENT and error is None
    assert any("son adresse fonctionne" in e for e in events)


def test_webform_reply_needs_action(client):
    rid, ref = _request("portac", "privacy@portail-c.example")
    status, _, _ = _deliver(rid, "privacy@portail-c.example", f"Re: Ref. {ref}",
                            "Hello. This inbox is not actively monitored. Please use our Privacy Request Center.",
                            "<wf1@portail-c.example>")
    assert status == NEEDS_ACTION


# ------------------------------------------- réanalyse des clôtures automatiques ---

@pytest.fixture
def no_imap(monkeypatch):
    monkeypatch.setattr(settings, "imap_host", "")
    yield
    with SessionLocal() as db:
        db.query(KV).filter(KV.key.like("reanalysis:%")).delete(synchronize_session=False)
        db.commit()


def _closed(slug, closing_summary):
    rid, ref = _request(slug, f"dpo@{slug}.example", status=COMPLETED)
    with SessionLocal() as db:
        body = ("De : DPO <dpo@%s.example>\nDate : hier\nObjet : Re: x\n\nHi,\nThank you for contacting us. Your request has "
                "been received and forwarded to our Legal team.\n\n-----Ursprüngliche Nachricht-----\nVon: Moi\n"
                "Gesendet: gestern\nAn: dpo\nBetreff: x\n\n%s" % (slug, OUR_LETTER))
        db.add(Event(request_id=rid, kind="reply", summary="Réponse reçue", body=body, at=utcnow() - timedelta(hours=2)))
        db.add(Event(request_id=rid, kind="status", summary=closing_summary, at=utcnow() - timedelta(hours=1)))
        db.commit()
    return rid


def test_reanalysis_reopens_automatic_closures_only(client, no_imap):
    auto = _closed("auto-d", "Classement automatique : Terminée (« has been erased »)")
    manual = _closed("manu-e", "Statut changé manuellement : Terminée")
    proposals = {p["request_id"]: p for p in reanalysis.analyse()}
    assert proposals[auto]["from_status"] == COMPLETED and proposals[auto]["to_status"] == SENT
    assert manual not in proposals  # une décision prise à la main n'est jamais remise en cause
