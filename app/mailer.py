"""Envoi (SMTP), relève (IMAP) et analyse des réponses."""
from __future__ import annotations

import email
import html
import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import formataddr, getaddresses, make_msgid, formatdate

from .config import settings

log = logging.getLogger(__name__)


class SendError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


# ---------------------------------------------------------------- SMTP ----

def build_message(to: str, subject: str, body: str, from_name: str, reply_to: str | None = None,
                  in_reply_to: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, settings.smtp_from))
    msg["To"] = to
    if reply_to and reply_to.lower() != settings.smtp_from.lower():
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = settings.smtp_from.split("@")[-1] if "@" in settings.smtp_from else None
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
    if not settings.smtp_configured:
        raise SendError("Messagerie non configurée : renseignez le serveur SMTP et l'adresse d'expédition dans Réglages")
    ctx = ssl.create_default_context()
    try:
        if settings.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx, timeout=60)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60)
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
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.is_multipart() or part.get_content_disposition() == "attachment" and not ctype.startswith("message/"):
            continue
        try:
            if ctype == "text/plain":
                texts.append(part.get_content())
            elif ctype == "text/html":
                texts.append(html_to_text(part.get_content()))
            elif ctype in ("text/rfc822-headers", "message/delivery-status"):
                payload = part.get_payload()
                texts.append(payload if isinstance(payload, str) else str(payload))
        except (LookupError, UnicodeError, KeyError) as e:  # encodage exotique
            log.debug("Partie illisible : %s", e)
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
        for auth_result in msg.get_all("Authentication-Results", []):
            value = str(auth_result).strip()
            authserv = value.split(";", 1)[0].strip().split()[0].lower() if value else ""
            if authserv != trusted_authserv:
                continue
            has_dmarc = bool(re.search(r"(?:^|;)\s*dmarc\s*=\s*pass\b", value, re.I))
            has_aligned_mechanism = bool(re.search(r"(?:^|;)\s*(?:spf|dkim)\s*=\s*pass\b", value, re.I))
            dmarc_pass = has_dmarc and has_aligned_mechanism
            if dmarc_pass:
                break
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
)
CONFIRM_RE = _P(
    r"confirm (your |the )?(e-?mail|request|address|identity) by clicking", r"(click|tap) (on )?(the|this) (following |below )?(link|button) to (confirm|verify|validate)",
    r"please (confirm|verify) (your |the )?(e-?mail|request|address)", r"verification link", r"confirmation link",
    r"cliquez sur le lien (ci-dessous |suivant )?pour (confirmer|valider|v[ée]rifier)", r"confirmer votre (demande|adresse)", r"valider votre (demande|adresse)",
    r"lien de (confirmation|validation|v[ée]rification)", r"best[äa]tigen sie (ihre|die) (anfrage|e-?mail)", r"best[äa]tigungslink",
)
VERIFY_RE = _P(
    r"verify your identity", r"identity verification", r"v[ée]rifier votre identit[ée]", r"confirmer votre identit[ée]",
    r"fill (in|out) (the|our) (web ?|online ?)?form", r"use our (web ?|online ?|privacy )?(form|portal)", r"submit (your request )?(via|through) our",
    r"remplir (le|notre) formulaire", r"utiliser notre formulaire", r"via notre formulaire",
)
NO_DATA_RE = _P(
    r"no (personal )?(data|information|records?) (about|on|relating to|regarding|associated with|linked to) you",
    r"(do not|don't|does not|doesn't) (hold|have|store|process) any (personal )?(data|information)",
    r"(could|were) not (find|locate|identify) any", r"unable to (find|locate|identify) any", r"no records? (were |was )?found",
    r"aucune donn[ée]e", r"ne (d[ée]tenons|disposons|poss[ée]dons|traitons) (d')?aucune", r"pas de donn[ée]es (vous )?concernant",
    r"n'apparaissez pas", r"ne figurez pas", r"keine (personenbezogenen )?daten", r"nicht (in unserer datenbank )?gespeichert",
)
DELETED_RE = _P(
    r"(has|have|had) (now )?been (permanently |successfully )?(deleted|erased|removed|anonymi[sz]ed)",
    r"(we|i) (have )?(permanently |successfully )?(deleted|erased|removed) (all )?(your|the)",
    r"successfully (deleted|erased|removed)", r"(added|placed) (you |your [a-z ]+)?(to|on) (our|the) (suppression|opt-?out|do not contact|blocking) list",
    r"(ont|a) [ée]t[ée] (d[ée]finitivement )?(supprim|effac|anonymis)", r"nous avons (bien )?(proc[ée]d[ée] [àa] la suppression|supprim|effac)",
    r"suppression (a [ée]t[ée] )?effectu[ée]e", r"(liste|fichier) (d'opposition|repoussoir|de blocage)",
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
    r"(country|state) of residence", r"which country (do )?you (live|reside)", r"where (do )?you (live|reside)", r"are you (a )?(resident|located) in",
    r"pays de r[ée]sidence", r"dans quel pays (r[ée]sidez|habitez)", r"wohnsitzland", r"in welchem land (wohnen|leben)",
)
ACK_RE = _P(
    r"accus[ée] de r[ée]ception", r"accusons r[ée]ception", r"automatic reply", r"auto(matic)?[- ]?reply", r"out of (the )?office", r"r[ée]ponse automatique",
    r"we (have )?received your (request|message|e-?mail)", r"nous avons (bien )?re[çc]u votre", r"thank you for (contacting|your (request|message|e-?mail))",
    r"ticket (number|#|no)", r"request (number|#|id)", r"eingangsbest[äa]tigung", r"automatische antwort", r"abwesenheit",
)

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
    "reply": "réponse à examiner",
}


@dataclass
class Classification:
    kind: str
    reason: str = ""


def classify(m: ParsedMail) -> Classification:
    text = f"{m.subject}\n{m.text}"[:20000]
    if m.is_bounce:
        return Classification("bounce", "message de non-distribution")
    # Ordre important : les confirmations de suppression priment sur le reste.
    for kind, rx in (
        ("completed_no_data", NO_DATA_RE),
        ("completed_deleted", DELETED_RE),
        ("id_request", ID_RE),
        ("confirm_link", CONFIRM_RE),
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
    hit = ACK_RE.search(text)
    if hit or m.is_auto:
        return Classification("ack", hit.group(0) if hit else "en-tête de réponse automatique")
    return Classification("reply", "")


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
    """Approximation du domaine enregistré (exemple.co.uk -> exemple.co.uk, mail.exemple.fr -> exemple.fr)."""
    host = (host or "").lower().strip().strip(".")
    host = re.sub(r"^https?://", "", host).split("/")[0].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac", "gouv"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# ---------------------------------------------------------------- IMAP ----

def _imap_connect() -> imaplib.IMAP4:
    if settings.imap_security == "ssl":
        conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, ssl_context=ssl.create_default_context(), timeout=60)
    else:
        conn = imaplib.IMAP4(settings.imap_host, settings.imap_port, timeout=60)
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


def test_imap() -> str:
    conn = _imap_connect()
    try:
        typ, data = conn.list()
        folders = [d.decode(errors="replace").split(' "/" ')[-1] for d in (data or []) if d]
        return f"Connexion IMAP réussie. Dossiers : {', '.join(folders[:15])}"
    finally:
        conn.logout()
