from email.message import EmailMessage

from app import mailer


def _raw(subject, body, sender="dpo@broker.example", **headers):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "moi@exemple.fr"
    m["Subject"] = subject
    m["Message-ID"] = "<abc@broker.example>"
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return bytes(m)


def test_classify_deleted_fr():
    m = mailer.parse_mail(_raw("Re: Réf. OUBLI-ABC123", "Bonjour, vos données ont été supprimées de nos bases."))
    assert mailer.classify(m).kind == "completed_deleted"
    assert mailer.find_references(m) == {"OUBLI-ABC123"}


def test_classify_no_data_en():
    m = mailer.parse_mail(_raw("Your request", "We do not hold any personal data about you."))
    assert mailer.classify(m).kind == "completed_no_data"


def test_classify_verification_wins():
    m = mailer.parse_mail(_raw("Request", "To proceed, please verify your identity by sending a copy of your ID."))
    assert mailer.classify(m).kind == "needs_action"


def test_classify_ack():
    m = mailer.parse_mail(_raw("Accusé de réception", "Nous avons bien reçu votre demande et y répondrons sous 30 jours."))
    assert mailer.classify(m).kind == "ack"


def test_classify_other():
    m = mailer.parse_mail(_raw("Question", "Pourriez-vous préciser votre ancienne adresse ?"))
    assert mailer.classify(m).kind == "reply"


def test_html_body():
    m = EmailMessage()
    m["From"] = "x@y.com"
    m["Subject"] = "hi"
    m.set_content("<p>Your data <b>has been deleted</b>.</p>", subtype="html")
    p = mailer.parse_mail(bytes(m))
    assert "has been deleted" in p.text
    assert mailer.classify(p).kind == "completed_deleted"


def test_bounce():
    m = mailer.parse_mail(_raw("Undelivered Mail Returned to Sender", "Réf. OUBLI-ZZZ999 user unknown", sender="MAILER-DAEMON@mx.example"))
    assert m.is_bounce and mailer.classify(m).kind == "bounce"


def test_registered_domain():
    assert mailer.registered_domain("mail.criteo.com") == "criteo.com"
    assert mailer.registered_domain("https://www.experian.co.uk/path") == "experian.co.uk"
