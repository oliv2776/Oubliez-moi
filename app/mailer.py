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

def build_message(to: str, subject: str, body: str, from_name: str, reply_to: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, settings.smtp_from))
    msg["To"] = to
    if reply_to and reply_to.lower() != settings.smtp_from.lower():
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = settings.smtp_from.split("@")[-1] if "@" in settings.smtp_from else None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(body)
    return msg


def send_message(msg: EmailMessage) -> None:
    """Envoie le message. En mode DRY_RUN, se contente de le journaliser."""
    recipients = [a for _, a in getaddresses([msg["To"]])]
    if settings.smtp_bcc_self:
        recipients.append(settings.smtp_from)
    if settings.dry_run:
        log.info("[DRY_RUN] E-mail non envoyé à %s : %s", msg["To"], msg["Subject"])
        return
    if not settings.smtp_configured:
        raise SendError("SMTP non configuré (SMTP_HOST / SMTP_FROM)")
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
            if settings.smtp_user:
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


_TAG_RE = re.compile(r"<[^>]+>")
_MSGID_RE = re.compile(r"<[^<>\s]+>")


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", s)
    s = html.unescape(_TAG_RE.sub(" ", s))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", s)).strip()


def parse_mail(raw: bytes, uid: str = "") -> ParsedMail:
    msg = email.message_from_bytes(raw, policy=default_policy)
    addrs = getaddresses([str(msg.get("From", ""))])
    from_name, from_addr = addrs[0] if addrs else ("", "")
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
    ctype = msg.get_content_type()
    auto_sub = str(msg.get("Auto-Submitted", "no")).lower()
    sender = from_addr.lower()
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
    )


# ------------------------------------------------ Classement automatique ---

_P = lambda *pats: re.compile("|".join(pats), re.I)  # noqa: E731

VERIFY_RE = _P(
    r"verify your identity", r"identity verification", r"proof of (your )?identity", r"copy of (your |a )?(id\b|passport|identity)",
    r"pi[eè]ce d'identit[eé]", r"justificatif d'identit[eé]", r"v[eé]rifier votre identit[eé]", r"confirmer votre identit[eé]",
    r"confirm your (e-?mail|request|identity)", r"click (on )?the (following |below )?link", r"cliquez sur le lien",
    r"confirmer votre demande", r"fill (in|out) (the|our) (web ?)?form", r"remplir (le|notre) formulaire",
    r"identit[äa]tsnachweis", r"ausweiskopie", r"personalausweis",
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
ACK_RE = _P(
    r"accus[ée] de r[ée]ception", r"automatic reply", r"auto(matic)?[- ]?reply", r"out of (the )?office", r"r[ée]ponse automatique",
    r"we (have )?received your (request|message|e-?mail)", r"nous avons (bien )?re[çc]u votre", r"thank you for (contacting|your (request|message|e-?mail))",
    r"ticket (number|#|no)", r"request (number|#|id)", r"eingangsbest[äa]tigung", r"automatische antwort", r"abwesenheit",
)


@dataclass
class Classification:
    kind: str  # bounce, needs_action, completed_no_data, completed_deleted, ack, reply
    reason: str = ""


def classify(m: ParsedMail) -> Classification:
    text = f"{m.subject}\n{m.text}"[:20000]
    if m.is_bounce:
        return Classification("bounce", "message de non-distribution")
    for kind, rx in (
        ("needs_action", VERIFY_RE),
        ("completed_no_data", NO_DATA_RE),
        ("completed_deleted", DELETED_RE),
    ):
        hit = rx.search(text)
        if hit:
            return Classification(kind, hit.group(0))
    hit = ACK_RE.search(text)
    if hit or m.is_auto:
        return Classification("ack", hit.group(0) if hit else "en-tête de réponse automatique")
    return Classification("reply", "")


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
    conn.login(settings.imap_user, settings.imap_password)
    return conn


def _quote_folder(name: str) -> str:
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


def test_imap() -> str:
    conn = _imap_connect()
    try:
        typ, data = conn.list()
        folders = [d.decode(errors="replace").split(' "/" ')[-1] for d in (data or []) if d]
        return f"Connexion IMAP réussie. Dossiers : {', '.join(folders[:15])}"
    finally:
        conn.logout()
