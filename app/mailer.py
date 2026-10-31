"""Envoi (SMTP), relève (IMAP) et analyse des réponses."""
from __future__ import annotations

import email
from collections import Counter
import html
import imaplib
import logging
import re
import smtplib

import tldextract
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import formataddr, getaddresses, make_msgid, formatdate
from urllib.parse import urlsplit

from .config import settings
from .net import create_pinned_socket

log = logging.getLogger(__name__)

# Snapshot PSL embarqué : aucune requête réseau au démarrage ni pendant le traitement.
# Les suffixes réservés servent uniquement aux adresses fictives des tests.
_TLD_EXTRACT = tldextract.TLDExtract(
    suffix_list_urls=(), include_psl_private_domains=True,
    extra_suffixes=("example", "test", "invalid"),
)


class SendError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


# ---------------------------------------------------------------- SMTP ----

class _PinnedSMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return create_pinned_socket(host, port, timeout, self.source_address, allow_private=True)


class _PinnedSMTP_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        sock = create_pinned_socket(host, port, timeout, self.source_address, allow_private=True)
        return self.context.wrap_socket(sock, server_hostname=host)


class _PinnedIMAP4(imaplib.IMAP4):
    def _create_socket(self, timeout):
        return create_pinned_socket(self.host, self.port, timeout, allow_private=True)


class _PinnedIMAP4_SSL(imaplib.IMAP4_SSL):
    def _create_socket(self, timeout):
        sock = create_pinned_socket(self.host, self.port, timeout, allow_private=True)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)

def build_message(to: str, subject: str, body: str, from_name: str, reply_to: str | None = None,
                  in_reply_to: str | None = None, from_addr: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    sender = from_addr or settings.smtp_from
    msg["From"] = formataddr((from_name, sender))
    msg["To"] = to
    if reply_to and reply_to.lower() != sender.lower():
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = sender.split("@")[-1] if "@" in sender else None
    msg["Message-ID"] = make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return msg


def send_message(msg: EmailMessage, force: bool = False) -> None:
    """Envoie le message. En mode simulation (sauf force=True), se contente de le journaliser."""
    recipients = [a for _, a in getaddresses([msg["To"]])]
    if settings.smtp_bcc_self:
        recipients.append(settings.smtp_from)
    if settings.dry_run and not force:
        log.info("[DRY_RUN] E-mail non envoyé à %s : %s", msg["To"], msg["Subject"])
        return
    from . import settings_store
    if settings_store.secret_unavailable("smtp_password") and not settings.smtp_password:
        raise SendError("Mot de passe SMTP chiffré illisible : restaurez ENCRYPTION_KEY ou saisissez à nouveau le mot de passe dans Réglages.")
    if not settings.smtp_configured:
        raise SendError("Messagerie non configurée : renseignez le serveur SMTP et l'adresse d'expédition dans Réglages")
    ctx = ssl.create_default_context()
    try:
        if settings.smtp_security == "ssl":
            server = _PinnedSMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx, timeout=60)
        else:
            server = _PinnedSMTP(settings.smtp_host, settings.smtp_port, timeout=60)
        with server:
            server.ehlo()
            if settings.smtp_security == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg, to_addrs=recipients)
    except smtplib.SMTPRecipientsRefused as e:
        raise SendError(f"Adresse refusée par le serveur : {e.recipients}", permanent=True) from e
    except smtplib.SMTPAuthenticationError as e:
        raise SendError(f"Authentification SMTP refusée : {e}") from e
    except (smtplib.SMTPException, OSError) as e:
        raise SendError(f"Erreur SMTP : {e}") from e


# ------------------------------------------------------- Analyse e-mail ----

@dataclass
class ParsedMail:
    uid: str
    from_addr: str
    from_name: str
    subject: str
    message_id: str
    in_reply_to: list[str] = field(default_factory=list)
    text: str = ""
    is_bounce: bool = False
    is_auto: bool = False
    date: str = ""
    html: str = ""
    reply_to: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (url, texte du lien)
    dmarc_pass: bool = False  # authentifié par le serveur IMAP de confiance configuré


_TAG_RE = re.compile(r"<[^>]+>")
_MSGID_RE = re.compile(r"<[^<>\s]+>")


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", s)
    s = html.unescape(_TAG_RE.sub(" ", s))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", s)).strip()


_HREF_RE = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_TEXT_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def extract_links(html_body: str, text: str) -> list[tuple[str, str]]:
    """Liens (url, texte) trouvés dans le message, sans doublon."""
    seen, out = set(), []
    for href, label in _HREF_RE.findall(html_body or ""):
        href = html.unescape(href.strip())
        if href.lower().startswith(("http://", "https://")) and href not in seen:
            seen.add(href)
            out.append((href, html_to_text(label)[:200]))
    for url in _TEXT_URL_RE.findall(text or ""):
        url = url.rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            out.append((url, ""))
    return out[:100]


def parse_mail(raw: bytes, uid: str = "") -> ParsedMail:
    msg = email.message_from_bytes(raw, policy=default_policy)
    addrs = getaddresses([str(msg.get("From", ""))])
    # Un nom d'affichage non entre guillemets contenant une virgule (« Acme, Inc. <dpo@acme.com> »)
    # est découpé en plusieurs morceaux : on retient le premier qui ressemble à une adresse.
    with_at = [a for a in addrs if "@" in a[1]]
    from_name, from_addr = (with_at or addrs or [("", "")])[0]
    if with_at and len(addrs) > 1:
        parts = [n or a for n, a in addrs if "@" not in a] + ([from_name] if from_name else [])
        from_name = ", ".join(x for x in parts if x)
    refs = " ".join(str(msg.get(h, "")) for h in ("In-Reply-To", "References"))
    texts = []
    # En multipart/alternative, ne traiter que la meilleure représentation du corps.
    # Concaténer texte + HTML du même message dupliquait les phrases et les décisions.
    try:
        body_part = msg.get_body(preferencelist=("plain", "html"))
        if body_part is not None:
            body_content = body_part.get_content()
            texts.append(body_content if body_part.get_content_type() == "text/plain" else html_to_text(body_content))
    except (LookupError, UnicodeError, KeyError) as e:
        log.debug("Corps du message illisible : %s", e)
    # Les rapports de non-distribution contiennent parfois des informations utiles
    # dans une partie dédiée, qui n'est pas le corps alternatif.
    for part in msg.walk():
        if part.get_content_type() not in ("text/rfc822-headers", "message/delivery-status"):
            continue
        try:
            payload = part.get_payload()
            if isinstance(payload, list):  # message/delivery-status : une liste de blocs d'en-têtes
                payload = "\n".join(p.as_string() if hasattr(p, "as_string") else str(p) for p in payload)
            texts.append(payload if isinstance(payload, str) else str(payload))
        except (LookupError, UnicodeError, KeyError) as e:
            log.debug("Partie de rapport illisible : %s", e)
    html_body = ""
    try:
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            html_body = part.get_content()
    except (LookupError, UnicodeError, KeyError):
        pass
    reply_to = ""
    rt = getaddresses([str(msg.get("Reply-To", ""))])
    rt = [a for _, a in rt if "@" in a]
    if rt:
        reply_to = rt[0].lower()
    ctype = msg.get_content_type()
    auto_sub = str(msg.get("Auto-Submitted", "no")).lower()
    sender = from_addr.lower()
    # Ne faire confiance qu'à Authentication-Results émis par l'identifiant
    # explicitement configuré ; un header fourni par l'expéditeur ne suffit pas.
    from .config import settings
    dmarc_pass = False
    trusted_authserv = settings.imap_authserv_id.strip().lower()
    if trusted_authserv:
        trusted_results = []
        for auth_result in msg.get_all("Authentication-Results", []):
            value = str(auth_result).strip()
            authserv = authserv_id(value)
            if authserv == trusted_authserv:
                trusted_results.append(value)
        # Plusieurs résultats du même authserv-id peuvent signaler un header forgé
        # conservé par le relais : dans le doute, ne pas authentifier.
        if len(trusted_results) == 1:
            # DMARC passe uniquement si l'identité authentifiée est alignée
            # avec le domaine visible dans From. Un SPF/DKIM pass sur une autre
            # identité (Return-Path ou signature tierce) ne suffit pas.
            clauses = [part.strip() for part in trusted_results[0].split(";")[1:]]
            from_domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
            dmarc_from = ""
            aligned_pass = False
            for clause in clauses:
                if re.match(r"dmarc\s*=\s*pass\b", clause, re.I):
                    hit = re.search(r"\bheader\.from\s*=\s*([^\s;()]+)", clause, re.I)
                    dmarc_from = hit.group(1).strip(".").lower() if hit else ""
                elif re.match(r"spf\s*=\s*pass\b", clause, re.I):
                    hit = re.search(r"\bsmtp\.mailfrom\s*=\s*\"?([^\s;()\"]+)", clause, re.I)
                    aligned_pass = aligned_pass or bool(
                        hit and "@" in hit.group(1)
                        and registered_domain(hit.group(1).rsplit("@", 1)[-1]) == registered_domain(from_domain)
                    )
                elif re.match(r"dkim\s*=\s*pass\b", clause, re.I):
                    # header.d=domaine (RFC 8601) ; Gmail n'indique que header.i=@domaine.
                    hit = (re.search(r"\bheader\.d\s*=\s*\"?([^\s;()\"@]+)", clause, re.I)
                           or re.search(r"\bheader\.i\s*=\s*\"?[^\s;()\"@]*@([^\s;()\"]+)", clause, re.I))
                    aligned_pass = aligned_pass or bool(
                        hit and registered_domain(hit.group(1).strip(".").lower()) == registered_domain(from_domain)
                    )
            dmarc_pass = bool(
                dmarc_from and registered_domain(dmarc_from) == registered_domain(from_domain) and aligned_pass
            )
    return ParsedMail(
        uid=uid,
        from_addr=sender,
        from_name=from_name,
        subject=str(msg.get("Subject", "")),
        message_id=str(msg.get("Message-ID", "")).strip(),
        in_reply_to=_MSGID_RE.findall(refs),
        text="\n\n".join(t for t in texts if t).strip(),
        is_bounce=(
            ctype == "multipart/report"
            or sender.startswith(("mailer-daemon@", "postmaster@"))
            or "mailer-daemon" in from_name.lower()
        ),
        is_auto=auto_sub not in ("", "no") or bool(msg.get("X-Autoreply")) or bool(msg.get("X-Autorespond")),
        date=str(msg.get("Date", "")),
        html=html_body,
        reply_to=reply_to,
        links=extract_links(html_body, "\n".join(texts)),
        dmarc_pass=dmarc_pass,
    )


# ------------------------------------------------ Classement automatique ---

_P = lambda *pats: re.compile("|".join(pats), re.I)  # noqa: E731

ID_RE = _P(
    r"cop(y|ies) of (your |a |an )?(valid )?(government[- ]issued )?(photo )?(id\b|identity|passport|driv)", r"proof of (your )?identity",
    r"photo id", r"identity document", r"pi[eè]ce d'identit[eé]", r"justificatif d'identit[eé]", r"copie de (votre |la )?(carte|passeport)",
    r"identit[äa]tsnachweis", r"ausweiskopie", r"kopie (ihres|des) (personal)?ausweis",
    r"identi(ty|fication) (verification )?form\b", r"formulaire (d'identification|de v[ée]rification d'identit[ée])", r"identit[äa]tsformular",
)
CONFIRM_RE = _P(
    r"confirm (your |the )?(e-?mail|request|address|identity) by clicking", r"(click|tap) (on )?(the|this) (following |below )?(link|button) to (confirm|verify|validate)",
    r"please (confirm|verify) (your |the )?(e-?mail|request|address)", r"verification link", r"confirmation link",
    r"cliquez sur le lien (ci-dessous |suivant )?pour (confirmer|valider|v[ée]rifier)", r"confirmer votre (demande|adresse)", r"valider votre (demande|adresse)",
    r"lien de (confirmation|validation|v[ée]rification)", r"best[äa]tigen sie (ihre|die) (anfrage|e-?mail)", r"best[äa]tigungslink",
    # Le lien est dans un autre message (« follow the prompt in the email you received from… »)
    r"follow(ing)? the (prompt|link|instructions|steps) (in|from) the (e-?mail|message) (you (have )?received|we (have )?sent)",
    r"(click|use|open) the (verification |confirmation )?link (in|from) the (e-?mail|message) (you (have )?received|we (have )?sent)",
    r"(en suivant|cliquez sur|ouvrez) le lien (re[çc]u|envoy[ée]|figurant) (dans|par) (l'|un |le )?(e-?mail|courriel|message)",
)
VERIFY_RE = _P(
    r"verify your identity", r"identity verification", r"v[ée]rifier votre identit[ée]", r"confirmer votre identit[ée]",
)
# Le courtier demande d'écrire depuis une autre adresse (celle dont on demande aussi l'effacement).
_EMAIL = r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+"
CONFIRM_FROM_RE = _P(
    r"(send|reply|write|respond|e-?mail us|confirm)[^.\n]{0,80}\bfrom (the |that |this |your )?(e-?mail )?(address )?(concerned|in question|" + _EMAIL + ")",
    r"(confirm|confirmation)[^.\n]{0,60}(directly )?from (the )?(e-?mail )?(address )?" + _EMAIL,
    r"(depuis|[àa] partir de) (l'adresse|cette adresse|l'adresse e-?mail|l'adresse [ée]lectronique)( e-?mail)? (concern[ée]e|en question|" + _EMAIL + ")",
    r"von (der|dieser|ihrer) (betreffenden |genannten )?e-?mail-?adresse (aus )?(schreiben|senden|best[äa]tigen)",
)
# Le courtier n'accepte que son formulaire / portail, ou cette adresse n'est pas lue.
WEBFORM_RE = _P(
    r"fill (in|out) (the|our|this) (web ?|online ?)?form", r"use our (web ?|online ?|privacy )?(form|portal)", r"submit (your request )?(via|through) our",
    r"(please|kindly) (visit|use|go to|complete|fill (in|out)|click|submit[^.\n]{0,40}(via|through|using|on))[^.\n]{0,90}"
    r"(portal|privacy (center|centre|hub|tool|page|request page|choices)|request (center|centre|page|form)|web ?form|online form|request form|\bform\b|rights request)",
    r"(privacy rights requests|requests?)[^.\n]{0,80}(must|should|need to|have to) be (submitted|made|sent|filed)",
    r"(requests?)[^.\n]{0,40}(made|filed|sent|submitted) (otherwise|through other channels|by e-?mail|via e-?mail)[^.\n]{0,30}(cannot|will not|won't|can't|are not)",
    r"(cannot|can't|will not|won't) be (made|submitted|processed|handled) (through|via|by) (e-?mail|this|privacy@)",
    r"(inbox|mailbox|e-?mail address) is (no longer|not) (actively |being )?(monitored|checked)",
    r"(inbox|mailbox) is (currently )?(nonfunctional|non-functional|not functional|unable to receive)",
    r"(will|do) not (receive|get) a response", r"(dedicated|secure) (online |web )?(form|portal)",
    r"follow the instructions[^.\n]{0,40}\n?[^.\n]{0,80}privacy (policy|notice)",
    r"to exercise (your|any|applicable|these|data subject|your applicable)[^.\n]{0,40}rights,?[^.\n]{0,20}(please )?(visit|use|click|go to|follow|submit)",
    r"(if you('re| are) looking to|if you would like to|if you wish to|if you want to) (make|submit|exercise)[^.\n]{0,80}(please )?(visit|click|use|go to)",
    r"submit (a|an|your) [^.\n]{0,30}request[^.\n]{0,40}(through|via|using|on) [^.\n]{0,50}(portal|form|privacy cent(er|re)|request page)",
    r"only way[^.\n]{0,60}(is|would be) (through|via|using) (our|the|a) (web ?form|form|portal)",
    r"(merci de|veuillez|nous vous invitons [àa]) (passer par|utiliser|remplir|compl[ée]ter)[^.\n]{0,40}(formulaire|portail|espace)",
    r"(remplir|utiliser) (le|notre) formulaire", r"via (notre|le) (formulaire|portail)",
    r"(nutzen|verwenden) sie (bitte )?(unser|das)[^.\n]{0,30}(formular|portal)", r"[üu]ber (unser|das) [^.\n]{0,30}(formular|portal)",
)
# Le courtier ne connaît que des identifiants publicitaires (cookie, appareil) et les réclame.
DEVICE_RE = _P(
    r"(cookie|device|advertising|online|mobile advertising) ?(id|identifier)s?\b[^.\n]{0,80}(is |are )?(required|needed|necessary)",
    r"(provide|send|share|give)( us)?( with)? (your|the) [^.\n]{0,50}(cookie ?id|device id|advertising id|identifier|idfa|aaid|maid)",
    r"(without|in the absence of) (such |these |further information such as your [^.\n]{0,40})?identifiers",
    r"(need|require) (you to provide|your)[^.\n]{0,40}(cookie|device|advertising) ?(id|identifier)",
    r"(identifiant|id) (cookie|publicitaire|de l'appareil)[^.\n]{0,60}(n[ée]cessaire|requis|indispensable)",
    r"(screenshots?|copy|value) of (the )?(relevant )?identifiers", r"onlinekennung", r"(cookie|werbe)-?id\b[^.\n]{0,60}(mit|senden|nennen)",
)
NO_DATA_RE = _P(
    r"no (personal )?(data|information|records?) (about|on|relating to|regarding|associated with|linked to) you",
    r"(do not|don't|does not|doesn't) (hold|have|store|process)( or (hold|have|store|process))? any (other )?(personal )?(data|information)",
    r"(could|were|did) not (find|locate|identify) any", r"unable to (find|locate|identify) (any|your)", r"no records? (were |was )?found",
    r"did not identify any", r"(could|were|did) not (find|locate|identify) (your|an?) (account|record|profile|data)", r"no (personal )?data to (be )?(erased|deleted)", r"(do not|does not|don't|doesn't) (maintain|keep) (any )?records",
    r"aucune donn[ée]e(?![^.\n]{0,60}(communication|communiqu|transmi|partag|c[ée]d[ée]|vendu))",
    r"ne (d[ée]tenons|disposons|poss[ée]dons|traitons) (d')?aucune", r"pas de donn[ée]es (vous )?concernant",
    r"n'avons pas (pu )?trouv(er|[ée]) (de |d'|aucun|un enregistrement)", r"n'avons pas enregistr[ée] de donn[ée]es", r"ne parvenons pas [àa] trouver",
    r"ne figurent? pas dans (notre|nos) (base|fichier)", r"not holding any (personal )?data",
    r"n'apparaissez pas", r"ne figurez pas", r"keine (personenbezogenen )?daten", r"nicht (in unserer datenbank )?gespeichert",
)
DELETED_RE = _P(
    r"(has|have|had) (now )?been (permanently |successfully )?(deleted|erased|removed|anonymi[sz]ed)",
    r"(we|i) (have )?(permanently |successfully )?(deleted|erased|removed) (all )?(your|the)",
    r"successfully (deleted|erased|removed)", r"(added|placed) (you |your [a-z ]+)?(to|on) (our|the) (suppression|opt-?out|do not contact|blocking) list",
    r"(ont|a) [ée]t[ée] (d[ée]finitivement )?(supprim|effac|anonymis)", r"nous avons (bien )?(proc[ée]d[ée] [àa] la suppression|supprim|effac)",
    r"suppression (a [ée]t[ée] )?effectu[ée]e", r"(liste|fichier) (d'opposition|repoussoir|de blocage)",
    r"have (now )?(checked [^.\n]{0,40} and )?(have )?(permanently |successfully )?(deleted|erased|removed) (all )?(of )?your (personal )?data",
    r"wurden? (vollst[äa]ndig |erfolgreich )?gel[öo]scht", r"haben (ihre daten )?gel[öo]scht", r"(sperrliste|werbesperre)",
)
EXTENSION_RE = _P(
    r"(extend|extension of) (the |our |this )?(response |time ?|deadline|period)", r"(additional|further|another) (two|2) months",
    r"deux mois suppl[ée]mentaires", r"prolong(er|ation) (le|du|de notre) d[ée]lai", r"d[ée]lai suppl[ée]mentaire",
    r"fristverl[äa]ngerung", r"weitere(n)? (zwei|2) monate",
)
LEGIT_RE = _P(r"legitimate interests?", r"int[ée]r[êe]ts? l[ée]gitimes?", r"berechtigte[sn]? interesse")
REFUSAL_RE = _P(
    r"unable to (comply|delete|fulfil|fulfill|honou?r)", r"(cannot|can't|will not|won't) (comply|delete|erase|fulfil)", r"not able to (delete|erase|comply)",
    r"(ne pouvons|ne sommes pas en mesure de) (pas )?(donner suite|supprimer|proc[ée]der|faire droit)", r"refusons", r"rejet[ée]e?",
    r"k[öo]nnen .{0,40}nicht (l[öo]schen|nachkommen|entsprechen)",
)
RESIDENCE_RE = _P(
    r"which country (did|do|are) you", r"what country (do|are|did) you", r"(country|state)( and state)?( or region)? in which you (are located|reside|live)",
    r"(country|state) of residence", r"which country (do )?you (live|reside)", r"where (do )?you (live|reside)", r"are you (a )?(resident|located) in",
    r"pays de r[ée]sidence", r"dans quel pays (r[ée]sidez|habitez)", r"wohnsitzland", r"in welchem land (wohnen|leben)",
)
ASK_RE = _P(
    r"(could|would|can) you (please )?(confirm|provide|send|tell us|indicate|specify|clarify|respond|reply)",
    r"please (provide|confirm|send us|indicate|specify|reply with|respond with)", r"(we )?(therefore )?ask you to (send|confirm|provide|indicate)",
    r"we (would |do |will )?(need|require) (you to|your|the following|additional|further|more|to confirm)",
    r"pourriez-vous", r"pouvez-vous (nous )?(indiquer|confirmer|pr[ée]ciser|communiquer|envoyer)",
    r"merci de (bien vouloir )?(nous )?(confirmer|indiquer|pr[ée]ciser|communiquer|transmettre|envoyer)",
    r"nous vous (remercions|prions|demandons) de (bien vouloir )?(nous )?(confirmer|indiquer|pr[ée]ciser|communiquer|transmettre|lancer|faire)",
    r"bitte (teilen|senden|best[äa]tigen|nennen) sie",
)
# Questions simples auxquelles une réponse type suffit (voir clarification_topic).
CLARIFY_TOPICS = (
    ("relationship", _P(
        r"relationship (with|to) [^.\n]{0,60}(employee|customer|consumer|client|vendor|candidate)",
        r"(are you|whether you are|if you are) (an? |our )?(current |former )?(employee|customer|client|consumer|candidate)",
        r"(êtes-vous|si vous êtes|[ée]tiez-vous) (un |une |notre )?(client|salari[ée]|employ[ée]|collaborat|candidat)",
        r"(votre )?(lien|relation) avec (notre|la) (soci[ée]t[ée]|entreprise|groupe)",
    )),
    ("unknown_details", _P(
        r"\b(user ?name|account (name|number|id)|customer (number|id)|membership number|order number)\b",
        r"\b(work|business|professional|corporate) e-?mail( address)?\b",
        r"nom d'utilisateur|identifiant de connexion|num[ée]ro (de )?(client|compte|commande|adh[ée]rent)|e-?mail professionnel|adresse professionnelle",
        r"\b(agence|campagne|campaign|advertis(ing|ement) campaign)\b[^.\n]{0,80}", r"p[ée]riode concern[ée]e|time ?frame concerned|period concerned",
        r"\bbenutzername|kundennummer|kundenkonto\b",
    )),
    ("which_email", _P(
        r"(confirm|indicate|provide|tell us|specify)[^.\n]{0,40}\b(e-?mail )?address(es)? (concerned|in question|you used|used|associated)",
        r"(confirmer|indiquer|pr[ée]ciser|communiquer)[^.\n]{0,40}l'adresse (e-?mail |[ée]lectronique )?(concern[ée]e|utilis[ée]e|en question)",
    )),
)


def normalize_reply(text: str) -> str:
    """Apostrophes typographiques et lignes coupées à 72 caractères recollées (les paragraphes restent séparés)."""
    text = (text or "").replace("\u2019", "'")
    return re.sub(r"[ \t]+", " ", re.sub(r"(?<!\n)[ \t]*\n[ \t]*(?!\n)", " ", text))


def clarification_topic(text: str) -> str | None:
    """Sujet de la question simple posée par le courtier : relationship | unknown_details | which_email."""
    text = normalize_reply(text)
    for topic, rx in CLARIFY_TOPICS:
        if rx.search(text):
            return topic
    return None


# Ce que dit le courtier juste après « aucune donnée » quand il attend encore quelque chose.
_BUT_RE = re.compile(r"\b(however|but|nevertheless|cependant|toutefois|n[ée]anmoins|jedoch|allerdings)\b", re.I)
# Délais et incidents de distribution temporaires, ou limités à un membre d'une liste de diffusion.
DELIVERY_ISSUE_RE = _P(
    r"\bdelay(ed)?\b", r"temporar(y|ily)", r"will (be )?retr(y|ied)", r"(d'autres|nouvelles) tentatives", r"probl[èe]me temporaire",
    r"vor[üu]bergehend", r"verz[öo]gert", r"mailbox (is )?full", r"quota", r"\b5\.2\.2\b", r"bo[îi]te (aux lettres |de r[ée]ception )?pleine",
)
# Phrases de nos propres courriers : si elles apparaissent, c'est notre message cité.
OWN_LETTER_RE = re.compile(
    r"(?is)(pursuant to article 17 of the general data protection regulation \(gdpr, regulation \(eu\) 2016/679\), i request"
    r"|conform[ée]ment [àa] l'article 17 du r[èe]glement g[ée]n[ée]ral sur la protection des donn[ée]es \(rgpd, r[èe]glement \(ue\) 2016/679\), je vous demande"
    r"|please confirm in writing that my data has been erased|je vous remercie de me confirmer par [ée]crit l'effacement de mes donn[ée]es"
    r"|on [^\n]{1,40}, i sent you a request for the erasure of my personal data|le [^\n]{1,40}, je vous ai adress[ée] une demande d'effacement"
    r"|you have asked me for proof of identity in order to process my erasure request|vous me demandez un justificatif d'identit[ée] pour traiter"
    r"|you have declined my request of [^\n]{1,40} on the basis of your legitimate|vous refusez de donner suite [àa] ma demande du"
    r"|in answer to your question: i reside in|en r[ée]ponse [àa] votre question : je r[ée]side)"
    .replace(" ", r"\s+")
)
# Formules qui annoncent une vérification éventuelle, sans rien demander.
# (ce qui arrivera plus tard, ou ce que le courtier n'exige pas, n'est pas une demande)
_MAYBE_LATER_RE = re.compile(
    r"(?i)(we )?may need to (confirm|verify) your identity[^.\n]*|(vous allez recevoir|vous recevrez|you will (then )?receive|you'll receive)[^.\n]{0,120}"
    r"|(do not|don't|does not|will not|won't|no) (require|need|ask for)[^.\n]{0,30}(identity document|proof of identity|copy of (your )?id|photo id|pi[èe]ce d'identit[ée])"
)

ACK_RE = _P(
    r"accus[ée] de r[ée]ception", r"accusons r[ée]ception", r"automatic reply", r"auto(matic)?[- ]?reply", r"out of (the )?office", r"r[ée]ponse automatique",
    r"we (have )?received your (request|message|e-?mail)", r"nous avons (bien )?re[çc]u votre",
    r"ticket (number|#|no)", r"request (number|#|id)", r"eingangsbest[äa]tigung",
    r"(request|message|e-?mail|inquiry) (has been|was|have been) (successfully )?(received|submitted|logged)", r"confirm (the )?receipt",
    r"(has been|was) forwarded to", r"automatische antwort", r"abwesenheit",
    r"(passed|forwarded|transferred|escalated) (your request|it|this|your message) to", r"will (get back to|be in touch with|contact|respond to|reply to|email) you",
    r"(are|is|will be) (usually |normally |generally )?processed within", r"we will (process|handle|review) (your|the|this) request",
    r"nous reviendrons vers vous", r"wir (melden uns|werden uns)[^.\n]{0,40}(melden|zur[üu]ckmelden)",
)
# Formules de politesse : un accusé seulement si le message s'arrête là (sinon, il dit autre chose).
ACK_WEAK_RE = _P(r"thank you for (contacting|your (request|message|e-?mail)|submitting|reaching out)", r"merci (de|pour) votre (message|demande)")
ACK_WEAK_MAX = 700

KIND_LABELS = {
    "bounce": "adresse invalide",
    "id_request": "demande de pièce d'identité",
    "confirm_link": "lien de confirmation à cliquer",
    "needs_action": "action demandée",
    "completed_no_data": "aucune donnée détenue",
    "completed_deleted": "données supprimées",
    "extension": "prolongation du délai",
    "legit_refusal": "refus au nom de l'intérêt légitime",
    "refusal": "refus probable",
    "residence_question": "question sur le pays de résidence",
    "ack": "accusé de réception",
    "webform": "formulaire ou portail exigé",
    "confirm_from_address": "confirmation depuis une autre adresse demandée",
    "clarification": "question simple sur votre demande",
    "device_id": "identifiant publicitaire demandé",
    "delivery_issue": "incident de distribution temporaire",
    "reply": "réponse à examiner",
}


@dataclass
class Classification:
    kind: str
    reason: str = ""


_QUOTE_START_RE = re.compile(
    r"(?im)^\s*(?:[-_]{2,}\s*(?:original message|message d'origine|message initial|forwarded message|urspr[üu]ngliche nachricht|"
    r"original nachricht|oorspronkelijk bericht|messaggio originale|mensaje original|weitergeleitete nachricht)\s*[-_]*|"
    r"(?:original message|urspr[üu]ngliche nachricht|message d'origine)\s*:?|"
    r"begin forwarded message:|on .{1,240} wrote:|le .{1,240} a [ée]crit\s*:|.{0,240}\b(?:wrote|schrieb|a [ée]crit|schreef|skrev|escribi[óo]|ha scritto)\s*:|"
    r"\*{2,}\s*(?:original message|message d'origine)\s*\*{2,})\s*$"
)
# En-têtes « De / Envoyé / À / Objet » d'Outlook et équivalents, dans les langues courantes.
_HEADER_FROM_RE = re.compile(r"(?i)^\s*\**(?:from|de|von|van|fr[åa]n|da|fra|od|desde)\s*:")
_HEADER_NEXT_RE = re.compile(
    r"(?i)\b(?:sent|envoy[ée]|to|[àa]|subject|objet|date|datum|gesendet|an|betreff|verzonden|aan|onderwerp|skickat|till|[äa]mne|"
    r"inviato|oggetto|enviado|para|asunto|cc)\s*:"
)


def latest_reply_text(body: str) -> str:
    """Écarte le bloc cité pour ne classer que la réponse la plus récente."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    own = OWN_LETTER_RE.search(text)
    if own:  # notre propre courrier, cité sans marque reconnaissable (ticket, client mail exotique)
        text = text[:text.rfind("\n", 0, own.start()) + 1]
    lines = text.split("\n")
    kept = []
    for index, line in enumerate(lines):
        if not line.strip():
            kept.append(line)
            continue
        if line.lstrip().startswith(">") or _QUOTE_START_RE.match(line):
            break
        # Outlook et certains clients introduisent la citation par un bloc d'en-têtes.
        if _HEADER_FROM_RE.match(line):
            following = " ".join(lines[index + 1:index + 5])
            if _HEADER_NEXT_RE.search(following):
                break
        kept.append(line)
    return "\n".join(kept).strip()


def classify(m: ParsedMail) -> Classification:
    current_reply = normalize_reply(latest_reply_text(m.text))
    text = f"{m.subject}\n{current_reply}"[:20000]
    if m.is_bounce:
        hit = DELIVERY_ISSUE_RE.search(f"{m.subject}\n{current_reply[:1500]}")
        if hit:
            return Classification("delivery_issue", hit.group(0))
        return Classification("bounce", "message de non-distribution")
    text = _MAYBE_LATER_RE.sub(" ", text)
    # « Aucune donnée » ne clôt que si le courtier n'attend rien d'autre (« however… », question).
    vetoed = None
    for kind, rx in (("completed_no_data", NO_DATA_RE), ("completed_deleted", DELETED_RE)):
        hit = rx.search(text)
        if hit:
            after = text[hit.end():hit.end() + 400]
            ask = _BUT_RE.search(after) or ASK_RE.search(text) or DEVICE_RE.search(text) or WEBFORM_RE.search(text)
            if not ask:
                return Classification(kind, hit.group(0))
            vetoed = vetoed or hit.group(0)
    for kind, rx in (
        ("confirm_from_address", CONFIRM_FROM_RE),
        ("id_request", ID_RE),
        ("confirm_link", CONFIRM_RE),
        ("device_id", DEVICE_RE),
        ("webform", WEBFORM_RE),
        ("needs_action", VERIFY_RE),
        ("extension", EXTENSION_RE),
    ):
        hit = rx.search(text)
        if hit:
            return Classification(kind, hit.group(0))
    refusal = REFUSAL_RE.search(text)
    legit = LEGIT_RE.search(text)
    if legit and refusal:
        return Classification("legit_refusal", legit.group(0))
    if refusal:
        return Classification("refusal", refusal.group(0))
    hit = RESIDENCE_RE.search(text)
    if hit:
        return Classification("residence_question", hit.group(0))
    hit = ASK_RE.search(text)
    if hit or vetoed:
        # Une question à laquelle une réponse type suffit (lien avec l'entreprise, adresse concernée, compte…)
        for _topic, rx in CLARIFY_TOPICS:
            q = rx.search(text)
            if q:
                return Classification("clarification", q.group(0))
        return Classification("needs_action", hit.group(0) if hit else f"{vetoed}, mais…")
    hit = ACK_RE.search(text) or (ACK_WEAK_RE.search(text) if len(current_reply) <= ACK_WEAK_MAX else None)
    if hit or m.is_auto:
        return Classification("ack", hit.group(0) if hit else "en-tête de réponse automatique")
    return Classification("reply", "")


_BOUNCED_TO_RE = re.compile(
    r"(?i)(?:failed to these recipients or groups:\s*|your message to |n'est pas parvenu [àa] |could not be delivered to |"
    r"wasn't delivered to |konnte nicht an |delivery to the following recipients? failed[^\n]*\s*)<?([\w.+'-]+@[\w.-]+\w)"
)


def emails_in(text: str) -> list[str]:
    """Adresses e-mail citées dans un texte, en minuscules, dans l'ordre et sans doublon."""
    out: list[str] = []
    for a in re.findall(_EMAIL, text or ""):
        a = a.lower().strip(".")
        if a not in out:
            out.append(a)
    return out


def bounced_recipients(text: str) -> set[str]:
    """Adresses que le rapport de non-distribution déclare en échec (vide si on ne sait pas)."""
    return {a.lower() for a in _BOUNCED_TO_RE.findall(text or "")}


_TICKET_RE = re.compile(
    r"(?:ticket|case|request|reference|ref\.?|dossier|demande|requ[êe]te|vorgang|anfrage|aktenzeichen)"
    r"\s*(?:number|no\.?|nr\.?|n[°ºo]|#|id|num[ée]ro|nummer)?\s*[:#]?\s*([A-Z0-9][A-Z0-9_-]{3,24})",
    re.I,
)


def extract_ticket(text: str) -> str | None:
    """Numéro de ticket ou de dossier du courtier (pas notre propre référence)."""
    prefix = settings.reference_prefix.upper() + "-"
    for m in _TICKET_RE.finditer(text or ""):
        value = m.group(1)
        if any(ch.isdigit() for ch in value) and not value.upper().startswith(prefix):
            return value
    return None


def find_references(m: ParsedMail) -> set[str]:
    prefix = re.escape(settings.reference_prefix)
    return set(re.findall(rf"\b{prefix}-[A-Z0-9]{{6}}\b", f"{m.subject}\n{m.text}"))


# Domaines grand public : on ne rattache jamais une réponse sur la seule base de ces domaines.
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "outlook.fr", "hotmail.com", "hotmail.fr", "live.com", "live.fr",
    "msn.com", "yahoo.com", "yahoo.fr", "icloud.com", "me.com", "aol.com", "gmx.com", "gmx.de", "gmx.fr", "gmx.net",
    "web.de", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "laposte.net", "proton.me", "protonmail.com", "pm.me",
    "tutanota.com", "zoho.com", "mail.com", "yandex.com", "t-online.de",
}


def registered_domain(host: str) -> str:
    """Renvoie le domaine enregistrable selon la Public Suffix List, ou vide si inconnu.

    La liste privée est incluse pour éviter qu'un hébergement mutualisé (ex. github.io)
    ne permette à deux locataires distincts de partager une identité de domaine.
    """
    value = (host or "").strip().lower().strip(".")
    if not value:
        return ""
    # Accepte un nom d'hôte nu ou une URL, sans confondre port et partie du nom.
    parsed = urlsplit(value if "://" in value else "//" + value)
    hostname = (parsed.hostname or "").rstrip(".")
    if not hostname:
        return ""
    result = _TLD_EXTRACT(hostname)
    return result.top_domain_under_public_suffix.lower() if result.suffix else ""


# ---------------------------------------------------------------- IMAP ----

def _imap_connect() -> imaplib.IMAP4:
    from . import settings_store
    if settings_store.secret_unavailable("imap_password"):
        raise RuntimeError("Mot de passe IMAP chiffré illisible : restaurez ENCRYPTION_KEY ou saisissez à nouveau le mot de passe dans Réglages.")
    if settings.imap_security == "ssl":
        conn = _PinnedIMAP4_SSL(settings.imap_host, settings.imap_port, ssl_context=ssl.create_default_context(), timeout=60)
    else:
        conn = _PinnedIMAP4(settings.imap_host, settings.imap_port, timeout=60)
        if settings.imap_security == "starttls":
            conn.starttls(ssl_context=ssl.create_default_context())
    conn.login(*settings.imap_login)
    return conn


def _quote_folder(name: str) -> str:
    """Nom de dossier prêt pour une commande IMAP : encodé en UTF-7 modifié (accents) puis entre guillemets."""
    name = imap_utf7(name)
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fetch_new_messages(folder: str, last_uidvalidity: str | None, last_uid: int, since: str | None):
    """Renvoie (uidvalidity, [(uid, raw_bytes)]) des nouveaux messages, sans les marquer comme lus."""
    conn = _imap_connect()
    try:
        typ, _ = conn.select(_quote_folder(folder), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Dossier IMAP introuvable : {folder}")
        uidvalidity = (conn.untagged_responses.get("UIDVALIDITY") or [b"0"])[0]
        uidvalidity = uidvalidity.decode() if isinstance(uidvalidity, bytes) else str(uidvalidity)
        if last_uidvalidity != uidvalidity or last_uid <= 0:
            criteria = ("SINCE", since) if since else ("ALL",)
            last_uid = 0
        else:
            criteria = ("UID", f"{last_uid + 1}:*")
        typ, data = conn.uid("SEARCH", None, *criteria)
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last_uid) if typ == "OK" else []
        out = []
        for uid in uids[:500]:
            typ, msg_data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ == "OK":
                raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
                if raw:
                    out.append((uid, raw))
        return uidvalidity, out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def fetch_by_message_id(message_ids: list[str]) -> dict[str, bytes]:
    """Retrouve des messages déjà relevés grâce à leur Message-ID (lecture seule).

    Cherche dans les dossiers surveillés et dans le dossier de rangement, où les
    réponses des courtiers ont pu être déplacées. Renvoie {Message-ID: message brut}.
    """
    wanted = [m for m in dict.fromkeys(message_ids) if m and re.fullmatch(r"<[^<>\s\"]+>", m)]
    found: dict[str, bytes] = {}
    if not wanted:
        return found
    folders = list(settings.imap_folders or ["INBOX"])
    if settings.imap_file_mode == "move" and settings.imap_file_folder and settings.imap_file_folder not in folders:
        folders.append(settings.imap_file_folder)
    conn = _imap_connect()
    try:
        for folder in folders:
            typ, _ = conn.select(_quote_folder(folder), readonly=True)
            if typ != "OK":
                continue
            for mid in wanted:
                if mid in found:
                    continue
                typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", f'"{mid}"')
                uids = (data[0] or b"").split() if typ == "OK" and data else []
                if not uids:
                    continue
                typ, msg_data = conn.uid("FETCH", uids[-1].decode(), "(BODY.PEEK[])")
                raw = next((part[1] for part in msg_data or [] if isinstance(part, tuple)), None) if typ == "OK" else None
                if raw:
                    found[mid] = raw
            if len(found) == len(wanted):
                break
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return found


def imap_utf7(name: str) -> str:
    """Encode un nom de dossier en UTF-7 modifié (RFC 3501), ex. « Désinscriptions »."""
    out, buf = [], []

    def flush():
        if buf:
            import base64
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append("&" + b + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def file_messages(folder: str, uids: list[int], mode: str, target: str, mark_read: bool) -> str:
    """Range des messages : déplacement vers un dossier, ou libellé Gmail (le message reste dans la boîte)."""
    if not uids or mode == "off":
        return ""
    conn = _imap_connect()
    try:
        typ, _ = conn.select(_quote_folder(folder))  # lecture-écriture
        if typ != "OK":
            raise RuntimeError(f"Dossier IMAP introuvable : {folder}")
        caps = {c.upper() for c in (conn.capabilities or ())}
        gmail = "X-GM-EXT-1" in caps
        box = _quote_folder(target)
        conn.create(box)  # échoue sans conséquence si le dossier existe déjà
        uid_set = ",".join(str(u) for u in uids)
        if mark_read:
            conn.uid("STORE", uid_set, "+FLAGS", "(\\Seen)")
        if mode == "label":
            if gmail:
                typ, data = conn.uid("STORE", uid_set, "+X-GM-LABELS", f"({box})")
                return f"{len(uids)} message(s) libellé(s) « {target} »" if typ == "OK" else f"libellé refusé : {data}"
            mode = "move"  # les libellés n'existent que chez Gmail : on déplace
        if "MOVE" in caps:
            typ, data = conn.uid("MOVE", uid_set, box)
        else:
            typ, data = conn.uid("COPY", uid_set, box)
            if typ == "OK":
                conn.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
                if "UIDPLUS" in caps:
                    conn.uid("EXPUNGE", uid_set)
        if typ != "OK":
            raise RuntimeError(f"Déplacement refusé par le serveur : {data}")
        return f"{len(uids)} message(s) rangé(s) dans « {target} »"
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def authserv_id(header_value: str) -> str:
    """Identifiant du serveur (authserv-id) en tête d'un en-tête Authentication-Results.

    Renvoie "" quand l'en-tête n'en contient pas (Outlook/Hotmail écrivent
    directement « spf=pass … », sans identifiant).
    """
    first = str(header_value or "").strip().split(";", 1)[0].split()
    token = first[0].lower() if first else ""
    return "" if "=" in token else token


def detect_authserv_ids(sample: int = 40) -> dict:
    """Identifiants vus dans les Authentication-Results des derniers messages de la boîte de réception.

    Celui qui figure dans presque tous les messages est celui du serveur de réception :
    un expéditeur peut ajouter un faux en-tête à son propre message, pas à tous les autres.
    """
    conn = _imap_connect()
    counts: Counter[str] = Counter()
    scanned = without_id = 0
    try:
        typ, _ = conn.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError("impossible d'ouvrir la boîte de réception")
        typ, data = conn.uid("search", None, "ALL")
        uids = (data[0] or b"").split()[-sample:] if typ == "OK" and data else []
        if uids:
            typ, data = conn.uid("fetch", b",".join(uids), "(BODY.PEEK[HEADER.FIELDS (AUTHENTICATION-RESULTS)])")
            for part in data or []:
                if not isinstance(part, tuple):
                    continue
                scanned += 1
                headers = email.message_from_bytes(part[1]).get_all("Authentication-Results", [])
                ids = {authserv_id(v) for v in headers}
                if "" in ids:
                    without_id += 1
                counts.update(i for i in ids if i)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    best = counts.most_common(1)
    suggestion = best[0][0] if best and best[0][1] >= 3 and best[0][1] >= 0.6 * scanned else ""
    return {"scanned": scanned, "ids": counts.most_common(5), "without_id": without_id, "suggestion": suggestion}


def test_imap() -> str:
    conn = _imap_connect()
    try:
        typ, data = conn.list()
        folders = [d.decode(errors="replace").split(' "/" ')[-1] for d in (data or []) if d]
        return f"Connexion IMAP réussie. Dossiers : {', '.join(folders[:15])}"
    finally:
        conn.logout()
