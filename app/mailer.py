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


# Les règles de classement des réponses sont dans rules.py.


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
