"""Modèles de courriers RGPD (français / anglais)."""
from __future__ import annotations

from datetime import datetime

from .db import Broker, Profile, Request


def letter_language(profile: Profile, broker: Broker) -> str:
    if profile.language in ("fr", "en"):
        return profile.language
    return "fr" if "fr" in (broker.countries or []) else "en"


def _identity_block(profile: Profile, lang: str) -> str:
    labels = {
        "fr": ("Nom", "Autre(s) nom(s)", "Adresse(s) e-mail", "Téléphone(s)", "Adresse(s) postale(s)", "Date de naissance"),
        "en": ("Name", "Other name(s)", "Email address(es)", "Phone number(s)", "Postal address(es)", "Date of birth"),
    }[lang]
    lines = [f"- {labels[0]} : {profile.full_name}" if lang == "fr" else f"- {labels[0]}: {profile.full_name}"]
    sep = " : " if lang == "fr" else ": "

    def add(label, values):
        values = [v.replace("\n", ", ").strip() for v in (values or []) if v and v.strip()]
        if values:
            lines.append(f"- {label}{sep}" + " ; ".join(values))

    add(labels[1], profile.other_names)
    add(labels[2], profile.emails)
    add(labels[3], profile.phones)
    add(labels[4], profile.addresses)
    if profile.birth_date:
        lines.append(f"- {labels[5]}{sep}{profile.birth_date}")
    return "\n".join(lines)


def _signature(profile: Profile) -> str:
    sig = profile.full_name
    if profile.primary_email:
        sig += f"\n{profile.primary_email}"
    return sig


def erasure_letter(profile: Profile, broker: Broker, reference: str, lang: str) -> tuple[str, str]:
    ident = _identity_block(profile, lang)
    if lang == "fr":
        subject = f"Demande d'effacement de données personnelles (article 17 du RGPD) – Réf. {reference}"
        body = f"""Madame, Monsieur,

Conformément à l'article 17 du Règlement général sur la protection des données (RGPD, règlement (UE) 2016/679), je vous demande d'effacer l'ensemble des données à caractère personnel me concernant que {broker.name} détient, traite ou communique à des tiers.

Je m'oppose par ailleurs, en application de l'article 21, paragraphes 2 et 3, du RGPD, à tout traitement de mes données à des fins de prospection, y compris au profilage lié à cette prospection. Cette opposition est de droit et n'a pas à être motivée. Si une exception à l'effacement devait s'appliquer à certaines données, je vous demande d'en limiter le traitement conformément à l'article 18 et de m'en indiquer le fondement.

Conformément à l'article 19 du RGPD, je vous demande également de notifier cet effacement à chacun des destinataires auxquels mes données ont été communiquées, et de me communiquer l'identité de ces destinataires.

Afin de vous permettre de me retrouver dans vos fichiers, voici les informations me concernant. Elles ne doivent être utilisées qu'à cette seule fin :
{ident}

En l'absence de doute raisonnable sur mon identité (article 12, paragraphe 6, du RGPD), aucun justificatif supplémentaire ne devrait être nécessaire.

Je vous remercie de me confirmer par écrit l'effacement de mes données, ou l'absence de données me concernant, dans les meilleurs délais et au plus tard dans un délai d'un mois à compter de la réception de la présente demande (article 12, paragraphe 3, du RGPD). Merci de rappeler la référence {reference} dans votre réponse.

À défaut de réponse dans ce délai, je me réserve la possibilité de saisir la Commission nationale de l'informatique et des libertés (CNIL).

Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.

{_signature(profile)}
"""
    else:
        subject = f"Request for erasure of personal data (Article 17 GDPR) – Ref. {reference}"
        body = f"""Dear Sir or Madam,

Pursuant to Article 17 of the General Data Protection Regulation (GDPR, Regulation (EU) 2016/679), I request that {broker.name} erase all personal data concerning me that you hold, process or disclose to third parties.

In addition, pursuant to Article 21(2) and (3) GDPR, I object to any processing of my personal data for direct marketing purposes, including profiling related to such marketing. This objection is absolute and requires no justification. Should an exemption from erasure apply to any of my data, I request that you restrict its processing in accordance with Article 18 and inform me of the legal basis relied upon.

In accordance with Article 19 GDPR, I also ask you to communicate this erasure to every recipient to whom my personal data have been disclosed, and to inform me of the identity of those recipients.

To enable you to locate me in your records, please find my identifying information below. It may be used for this purpose only:
{ident}

As there is no reasonable doubt as to my identity (Article 12(6) GDPR), no additional documentation should be required.

Please confirm in writing that my data has been erased, or that you hold no data concerning me, without undue delay and in any event within one month of receipt of this request (Article 12(3) GDPR). Kindly quote the reference {reference} in your reply.

If I do not receive a response within this period, I reserve the right to lodge a complaint with the competent supervisory authority, the French Commission nationale de l'informatique et des libertés (CNIL).

Yours faithfully,

{_signature(profile)}
"""
    return subject, body


def reminder_letter(req: Request, original_body: str) -> tuple[str, str]:
    profile, broker, ref = req.profile, req.broker, req.reference
    sent = req.sent_at.strftime("%d/%m/%Y") if req.sent_at else "?"
    quoted = "\n".join("> " + line for line in original_body.splitlines())
    if req.language == "fr":
        subject = f"Relance – Demande d'effacement (article 17 du RGPD) – Réf. {ref}"
        body = f"""Madame, Monsieur,

Le {sent}, je vous ai adressé une demande d'effacement de mes données personnelles (rappelée ci-dessous). Le délai d'un mois prévu par l'article 12, paragraphe 3, du RGPD est aujourd'hui expiré sans que j'aie reçu de réponse.

Je vous demande de bien vouloir traiter ma demande sans délai et de m'en confirmer l'exécution par écrit. À défaut de réponse sous quinze jours, je déposerai une réclamation auprès de la CNIL.

Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.

{_signature(profile)}

--- Demande initiale du {sent} ---
{quoted}
"""
    else:
        subject = f"Reminder – Request for erasure (Article 17 GDPR) – Ref. {ref}"
        body = f"""Dear Sir or Madam,

On {sent}, I sent you a request for the erasure of my personal data (reproduced below). The one-month deadline set by Article 12(3) GDPR has now expired and I have not received any response.

Please process my request without further delay and confirm in writing that it has been carried out. If I do not receive a response within fifteen days, I will lodge a complaint with the CNIL.

Yours faithfully,

{_signature(profile)}

--- Original request of {sent} ---
{quoted}
"""
    return subject, body


def cnil_complaint_text(req: Request) -> str:
    """Texte prêt à coller dans le formulaire de plainte en ligne de la CNIL."""
    b, p = req.broker, req.profile
    sent = req.sent_at.strftime("%d/%m/%Y") if req.sent_at else "?"
    rem = req.last_reminder_at.strftime("%d/%m/%Y") if req.last_reminder_at else None
    lines = [
        f"Organisme mis en cause : {b.name}",
        f"Contact protection des données : {b.email or b.webform or '-'}",
    ]
    if b.postal_address:
        lines.append("Adresse : " + b.postal_address.replace("\n", ", "))
    lines += [
        "",
        f"Le {sent}, j'ai adressé à cet organisme, par courrier électronique, une demande d'effacement de mes "
        f"données personnelles (article 17 du RGPD) assortie d'une opposition à la prospection (article 21), "
        f"sous la référence {req.reference}.",
    ]
    if rem:
        lines.append(f"Je l'ai relancé le {rem}.")
    if req.status == "refused":
        lines.append("L'organisme a refusé de donner suite à ma demande (réponse jointe).")
    else:
        lines.append(
            f"À ce jour ({datetime.now().strftime('%d/%m/%Y')}), je n'ai reçu aucune réponse satisfaisante, "
            "en méconnaissance de l'article 12, paragraphe 3, du RGPD."
        )
    lines += [
        "",
        "Je vous demande d'intervenir afin que cet organisme procède à l'effacement de mes données et m'en informe.",
        "",
        p.full_name,
    ]
    return "\n".join(lines)
