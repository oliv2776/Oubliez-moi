"""Modèles de courriers RGPD (français / anglais)."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from . import phones
from .db import Broker, Profile, Request


def letter_language(profile: Profile, broker: Broker) -> str:
    if profile.language in ("fr", "en"):
        return profile.language
    return "fr" if "fr" in (broker.countries or []) else "en"


def without_accents(text: str) -> str:
    """« Deschênes » -> « Deschenes » (beaucoup de bases ne stockent pas les accents)."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))


def name_variants(profile: Profile) -> list[str]:
    """Autres noms, plus l'orthographe sans accents du nom et des autres noms quand elle diffère."""
    names = [n for n in (profile.other_names or []) if n and n.strip()]
    known = {n.strip().lower() for n in [profile.full_name, *names]}
    for n in [profile.full_name, *names]:
        plain = without_accents(n).strip()
        if plain and plain.lower() not in known:
            names.append(plain)
            known.add(plain.lower())
    return names


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

    add(labels[1], name_variants(profile))
    add(labels[2], profile.emails)
    add(labels[3], [phones.parse(t).for_letter for t in (profile.phones or [])])
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
    profile, ref = req.profile, req.reference
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


def _re_subject(their_subject: str | None, fallback: str) -> str:
    subj = (their_subject or "").strip()
    if not subj:
        return fallback
    return subj if subj.lower().startswith(("re:", "re :", "aw:", "tr:")) else f"Re: {subj}"


def auto_reply_letter(req: Request, kind: str, their_subject: str | None = None) -> tuple[str, str]:
    """Réponses automatiques aux situations courantes. kind : id_request | legit_refusal | residence_question."""
    from .config import settings

    p, ref = req.profile, req.reference
    sent = req.sent_at.strftime("%d/%m/%Y") if req.sent_at else "?"
    fr = req.language == "fr"
    sig = _signature(p)
    if kind == "id_request":
        if fr:
            subject = _re_subject(their_subject, f"Demande d'effacement – justificatif d'identité – Réf. {ref}")
            body = f"""Madame, Monsieur,

Vous me demandez un justificatif d'identité pour traiter ma demande d'effacement du {sent} (réf. {ref}).

L'article 12, paragraphe 6, du RGPD ne vous permet de demander des informations supplémentaires que si vous avez des doutes raisonnables sur mon identité. Le Comité européen de la protection des données rappelle qu'une copie de pièce d'identité ne doit pas être exigée de manière systématique et qu'elle est disproportionnée lorsque l'identité peut être vérifiée par d'autres moyens (lignes directrices 01/2022, points 70 à 78) ; la CNIL ne l'admet qu'en cas de doute raisonnable.

Je vous écris depuis une adresse e-mail et avec des informations qui figurent dans vos fichiers : c'est précisément ce qui vous permet de retrouver mes données. Si vous estimez malgré tout avoir un doute raisonnable, merci de m'en indiquer le motif ; vous pouvez par exemple m'envoyer un lien ou un code de confirmation à l'adresse concernée.

Je vous remercie de traiter ma demande sans autre délai. Le délai d'un mois prévu par l'article 12, paragraphe 3, continue de courir.

Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.

{sig}
"""
        else:
            subject = _re_subject(their_subject, f"Erasure request – proof of identity – Ref. {ref}")
            body = f"""Dear Sir or Madam,

You have asked me for proof of identity in order to process my erasure request of {sent} (ref. {ref}).

Under Article 12(6) GDPR, you may only request additional information where you have reasonable doubts about my identity. The European Data Protection Board stresses that a copy of an identity document should not be required systematically and is disproportionate where identity can be verified by other means (Guidelines 01/2022, paragraphs 70 to 78).

I am writing from an email address, and with details, that appear in your records: this is precisely what allows you to locate my data. Should you nevertheless have a reasonable doubt, please explain its grounds; you may for instance send a confirmation link or code to the address concerned.

Please process my request without further delay. The one-month deadline set by Article 12(3) GDPR continues to run.

Yours faithfully,

{sig}
"""
    elif kind == "legit_refusal":
        if fr:
            subject = _re_subject(their_subject, f"Contestation de votre refus – Réf. {ref}")
            body = f"""Madame, Monsieur,

Vous refusez de donner suite à ma demande du {sent} (réf. {ref}) en invoquant votre intérêt légitime.

Je vous rappelle que je me suis opposé au traitement de mes données à des fins de prospection. En application de l'article 21, paragraphes 2 et 3, du RGPD, cette opposition est absolue : l'intérêt légitime ne peut pas lui être opposé, et mes données ne doivent plus être traitées à ces fins. L'article 17, paragraphe 1, point c), impose alors leur effacement.

Je maintiens donc ma demande d'effacement et vous demande de me confirmer son exécution dans un délai d'un mois. À défaut, je saisirai la CNIL en joignant nos échanges.

Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.

{sig}
"""
        else:
            subject = _re_subject(their_subject, f"Objection to your refusal – Ref. {ref}")
            body = f"""Dear Sir or Madam,

You have declined my request of {sent} (ref. {ref}) on the basis of your legitimate interests.

I would remind you that I objected to the processing of my personal data for direct marketing purposes. Under Article 21(2) and (3) GDPR this objection is absolute: legitimate interests cannot override it, and my data must no longer be processed for such purposes. Article 17(1)(c) GDPR then requires its erasure.

I therefore maintain my erasure request and ask you to confirm that it has been carried out within one month. Failing that, I will lodge a complaint with the supervisory authority, enclosing our correspondence.

Yours faithfully,

{sig}
"""
    elif kind == "residence_question":
        country = settings.residence_country or "France"
        if fr:
            subject = _re_subject(their_subject, f"Pays de résidence – Réf. {ref}")
            body = f"""Madame, Monsieur,

En réponse à votre question : je réside en {country}, dans l'Union européenne. Le RGPD s'applique donc à ma demande d'effacement du {sent} (réf. {ref}), que je vous remercie de traiter dans le délai prévu par son article 12, paragraphe 3.

Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.

{sig}
"""
        else:
            subject = _re_subject(their_subject, f"Country of residence – Ref. {ref}")
            body = f"""Dear Sir or Madam,

In answer to your question: I reside in {country}, in the European Union. The GDPR therefore applies to my erasure request of {sent} (ref. {ref}), which I ask you to process within the deadline set by Article 12(3).

Yours faithfully,

{sig}
"""
    else:
        raise ValueError(kind)
    return subject, body


def formal_notice_letter(req: Request) -> tuple[str, str]:
    """Mise en demeure, après la relance restée sans réponse."""
    from .config import settings

    p, b, ref = req.profile, req.broker, req.reference
    sent = req.sent_at.strftime("%d/%m/%Y") if req.sent_at else "?"
    rem = req.last_reminder_at.strftime("%d/%m/%Y") if req.last_reminder_at else "?"
    days = settings.formal_notice_wait_days
    _, original = erasure_letter(p, b, ref, req.language)
    quoted = "\n".join("> " + line for line in original.splitlines())
    if req.language == "fr":
        subject = f"Mise en demeure – Demande d'effacement (article 17 du RGPD) – Réf. {ref}"
        body = f"""Madame, Monsieur,

Le {sent}, je vous ai adressé une demande d'effacement de mes données personnelles, assortie d'une opposition à la prospection (articles 17 et 21 du RGPD). Je vous ai relancé le {rem}. Je n'ai reçu à ce jour aucune réponse, en méconnaissance de l'article 12, paragraphe 3, du RGPD.

Par la présente, je vous mets en demeure de procéder à l'effacement de l'ensemble de mes données, de cesser tout traitement à des fins de prospection et de m'en confirmer l'exécution par écrit dans un délai de {days} jours.

À défaut, je déposerai sans autre avis une réclamation auprès de la Commission nationale de l'informatique et des libertés (CNIL), en joignant l'ensemble de nos échanges.

Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.

{_signature(p)}

--- Demande initiale du {sent} (réf. {ref}) ---
{quoted}
"""
    else:
        subject = f"Formal notice – Request for erasure (Article 17 GDPR) – Ref. {ref}"
        body = f"""Dear Sir or Madam,

On {sent}, I sent you a request for the erasure of my personal data, together with an objection to direct marketing (Articles 17 and 21 GDPR). I sent you a reminder on {rem}. To date I have received no response, in breach of Article 12(3) GDPR.

I hereby give you formal notice to erase all of my personal data, to cease any processing for direct marketing purposes, and to confirm this in writing within {days} days.

Failing that, I will without further notice lodge a complaint with the supervisory authority, the French Commission nationale de l'informatique et des libertés (CNIL), enclosing all of our correspondence.

Yours faithfully,

{_signature(p)}

--- Original request of {sent} (ref. {ref}) ---
{quoted}
"""
    return subject, body


def supplement_letter(req: Request, missing: list[tuple[str, str]]) -> tuple[str, str]:
    """Courrier complémentaire : nouveaux identifiants à inclure dans la demande initiale."""
    labels = {"emails": ("Adresse e-mail", "Email address"), "phones": ("Téléphone", "Phone number"),
              "addresses": ("Adresse postale", "Postal address"), "other_names": ("Autre nom", "Other name")}
    fr = req.language == "fr"
    sep = " : " if fr else ": "
    lines = []
    for field, value in missing:
        if field == "phones":
            value = phones.parse(value).for_letter
        lines.append(f"- {labels[field][0 if fr else 1]}{sep}{value}")
    items = "\n".join(lines)
    sent = req.sent_at.strftime("%d/%m/%Y") if req.sent_at else "?"
    ref, profile, broker = req.reference, req.profile, req.broker
    if fr:
        subject = f"Complément à ma demande d'effacement (article 17 du RGPD) – Réf. {ref}"
        body = f"""Madame, Monsieur,

Le {sent}, je vous ai adressé une demande d'effacement de mes données personnelles, assortie d'une opposition à la prospection (articles 17 et 21 du RGPD), sous la référence {ref}.

Je vous demande d'étendre cette demande à toutes les données que {broker.name} détiendrait en lien avec les éléments suivants, qui me concernent également :
{items}

Ces informations ne doivent être utilisées que pour retrouver mes données. Les termes de ma demande initiale s'appliquent intégralement, y compris la notification aux destinataires (article 19) et le délai de réponse d'un mois (article 12, paragraphe 3). Merci de rappeler la référence {ref} dans votre réponse.

Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.

{_signature(profile)}
"""
    else:
        subject = f"Addendum to my request for erasure (Article 17 GDPR) – Ref. {ref}"
        body = f"""Dear Sir or Madam,

On {sent}, I sent you a request for the erasure of my personal data, together with an objection to direct marketing (Articles 17 and 21 GDPR), under reference {ref}.

Please extend that request to any personal data {broker.name} may hold in connection with the following identifiers, which also relate to me:
{items}

This information may be used for the sole purpose of locating my data. All terms of my original request apply, including notification of recipients (Article 19) and the one-month response deadline (Article 12(3)). Kindly quote the reference {ref} in your reply.

Yours faithfully,

{_signature(profile)}
"""
    return subject, body


_POSTCODE_RE = re.compile(r"^(?P<street>.*?)[,\s]+(?P<zip>\d{4,5})\s+(?P<city>[^\d,][^,]*)$")


def form_fields(req: Request) -> list[tuple[str, str]]:
    """Valeurs prêtes à copier dans un formulaire en ligne, dans l'ordre habituel des champs."""
    p = req.profile
    out: list[tuple[str, str]] = [("Nom complet", p.full_name)]
    parts = p.full_name.split()
    if len(parts) >= 2:
        out += [("Prénom", parts[0]), ("Nom de famille", " ".join(parts[1:]))]
    for name in name_variants(p):
        out.append(("Autre nom", name))
    for i, email in enumerate(p.emails or []):
        out.append(("E-mail" if i == 0 else "Autre e-mail", email))
    for tel in p.phones or []:
        ph = phones.parse(tel)
        if ph.valid:
            out += [("Téléphone (national)", ph.national), ("Téléphone (international)", ph.e164)]
        else:
            out.append(("Téléphone", tel))
    for i, addr in enumerate(p.addresses or []):
        label = "Adresse" if i == 0 else "Ancienne adresse"
        out.append((label, addr))
        m = _POSTCODE_RE.match(addr.strip())
        if m:
            out += [(f"{label} – rue", m["street"].strip(" ,")), (f"{label} – code postal", m["zip"]), (f"{label} – ville", m["city"].strip())]
    if p.birth_date:
        out.append(("Date de naissance", p.birth_date))
    out.append(("Référence de la demande", req.reference))
    return out


def form_message(req: Request, lang: str) -> str:
    """Texte court pour le champ « message » d'un formulaire."""
    p = req.profile
    ids = [*(p.emails or []), *(phones.parse(t).for_letter for t in (p.phones or []))]
    if lang == "fr":
        return (
            "Je demande l'effacement de toutes les données personnelles me concernant (article 17 du RGPD) et je m'oppose "
            "à leur traitement à des fins de prospection, y compris au profilage (article 21, paragraphes 2 et 3). "
            "Merci de notifier cet effacement aux destinataires de mes données (article 19) et de me confirmer son exécution "
            "dans un délai d'un mois (article 12, paragraphe 3). "
            f"Identifiants : {' ; '.join(ids)}. Référence : {req.reference}."
        )
    return (
        "I request the erasure of all personal data concerning me (Article 17 GDPR) and object to its processing for direct "
        "marketing purposes, including profiling (Article 21(2) and (3) GDPR). Please notify all recipients of my data of this "
        "erasure (Article 19) and confirm it has been carried out within one month (Article 12(3)). "
        f"Identifiers: {' ; '.join(ids)}. Reference: {req.reference}."
    )


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


def confirm_from_letter(req: Request, address: str, their_subject: str | None = None) -> tuple[str, str]:
    """Courte confirmation, envoyée depuis une autre adresse du profil, que cette adresse est bien la vôtre."""
    ref, profile = req.reference, req.profile
    if req.language == "fr":
        subject = _re_subject(their_subject, f"Confirmation – Réf. {ref}")
        body = f"""Madame, Monsieur,

Je confirme que l'adresse e-mail {address} m'appartient et que je souhaite exercer, pour cette adresse également, mes droits d'effacement (article 17 du RGPD) et d'opposition (article 21 du RGPD), conformément à ma demande réf. {ref}.

Je vous écris depuis cette adresse, comme vous me l'avez demandé.

Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.

{_signature(profile)}
"""
    else:
        subject = _re_subject(their_subject, f"Confirmation – Ref. {ref}")
        body = f"""Dear Sir or Madam,

I confirm that the email address {address} belongs to me and that I wish to exercise my rights to erasure (Article 17 GDPR) and to object (Article 21 GDPR) in relation to this address as well, as set out in my request ref. {ref}.

As you asked, I am writing to you from this address.

Yours faithfully,

{_signature(profile)}
"""
    return subject, body
