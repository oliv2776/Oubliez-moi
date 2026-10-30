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


def test_classify_id_request():
    m = mailer.parse_mail(_raw("Request", "To proceed, please verify your identity by sending a copy of your ID."))
    assert mailer.classify(m).kind == "id_request"
    m = mailer.parse_mail(_raw("Demande", "Merci de nous transmettre une copie de votre pièce d'identité."))
    assert mailer.classify(m).kind == "id_request"


def test_classify_new_kinds():
    cases = {
        "Please confirm your request by clicking the link below: https://privacy.acme.com/confirm?t=1": "confirm_link",
        "Cliquez sur le lien ci-dessous pour confirmer votre demande.": "confirm_link",
        "Please use our online form at https://acme.com/privacy to submit your request.": "webform",
        "Due to the number of requests, we need to extend the response period by an additional two months.": "extension",
        "Nous devons prolonger le délai de traitement de deux mois supplémentaires.": "extension",
        "We are unable to delete your data as we process it on the basis of our legitimate interest.": "legit_refusal",
        "We cannot comply with your request.": "refusal",
        "Could you please confirm your country of residence?": "residence_question",
        "Nous accusons réception de votre demande.": "ack",
        "Your data has been deleted. If you have questions, please verify your identity with us.": "completed_deleted",
    }
    for text, kind in cases.items():
        assert mailer.classify(mailer.parse_mail(_raw("Re", text))).kind == kind, text


def test_links_extracted():
    m = EmailMessage()
    m["From"] = "dpo@acme.com"
    m["Subject"] = "x"
    m.set_content("Voir https://acme.com/a.")
    m.add_alternative('<p><a href="https://privacy.acme.com/confirm?t=ab&amp;x=1">Confirm my request</a></p>', subtype="html")
    links = mailer.parse_mail(bytes(m)).links
    assert ("https://privacy.acme.com/confirm?t=ab&x=1", "Confirm my request") in links


def test_classify_ack():
    m = mailer.parse_mail(_raw("Accusé de réception", "Nous avons bien reçu votre demande et y répondrons sous 30 jours."))
    assert mailer.classify(m).kind == "ack"


def test_classify_other():
    m = mailer.parse_mail(_raw("Question", "Nous étudions votre message."))
    assert mailer.classify(m).kind == "reply"
    # Une question du courtier attend une réponse de votre part.
    m = mailer.parse_mail(_raw("Question", "Pourriez-vous préciser votre ancienne adresse ?"))
    assert mailer.classify(m).kind == "needs_action"


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


def test_registered_domain_uses_public_suffix_list_and_private_suffixes():
    assert mailer.registered_domain("mail.example.com.au") == "example.com.au"
    assert mailer.registered_domain("mail.example.edu.au") == "example.edu.au"
    assert mailer.registered_domain("tenant-one.github.io") == "tenant-one.github.io"
    assert mailer.registered_domain("tenant-two.github.io") == "tenant-two.github.io"
    assert mailer.registered_domain("https://www.experian.co.uk/path") == "experian.co.uk"
    assert mailer.registered_domain("unknown.invalidtld") == ""


def test_classification_ignores_quoted_old_deletion_when_latest_reply_requests_id():
    body = (
        "Bonjour, merci de nous transmettre une copie de votre pièce d'identité.\n\n"
        "Le 12 octobre 2026, support@example.com a écrit :\n"
        "> Bonjour, vos données ont été supprimées de nos bases."
    )
    parsed = mailer.parse_mail(_raw("Re: Votre demande", body))
    assert mailer.classify(parsed).kind == "id_request"


def test_classification_ignores_quoted_old_id_request_when_latest_reply_confirms_deletion():
    body = (
        "Bonjour, vos données ont été supprimées de nos bases.\n\n"
        "-----Original Message-----\n"
        "From: support@example.com\nSent: Monday\nTo: user@example.com\n"
        "Subject: Requête\nPlease send a copy of your ID."
    )
    parsed = mailer.parse_mail(_raw("Re: Votre demande", body))
    assert mailer.classify(parsed).kind == "completed_deleted"


def test_multipart_alternative_body_is_not_duplicated():
    msg = EmailMessage()
    msg["From"] = "dpo@acme.com"
    msg["Subject"] = "Réponse"
    msg.set_content("Merci, nous avons reçu votre demande.")
    msg.add_alternative("<p>Merci, nous avons reçu votre demande.</p>", subtype="html")
    parsed = mailer.parse_mail(bytes(msg))
    assert parsed.text.count("Merci, nous avons reçu votre demande.") == 1
