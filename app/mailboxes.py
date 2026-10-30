"""Analyse de vos boîtes mail : qui vous écrit en masse, désinscription, rangement.

Indépendant des demandes RGPD aux courtiers. Pour chaque boîte :
- seuls les EN-TÊTES des messages sont lus (expéditeur, objet, date, liens de
  désinscription), jamais le contenu, et rien n'est marqué comme lu ;
- un message compte comme envoi en masse s'il porte un en-tête List-Unsubscribe,
  List-Id ou Precedence: bulk ;
- l'analyse est incrémentale : après la première, seuls les nouveaux messages
  sont lus.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
import smtplib
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime

from sqlalchemy import func, select

from . import confirm, crypto, mailer, oauth_ms, settings_store
from .config import settings
from .db import KV, Broker, Mailbox, Sender, SessionLocal, kv_get, kv_set, utcnow
from .mailer import _quote_folder, registered_domain
from .net import create_pinned_socket, safe_urlopen

log = logging.getLogger(__name__)

PROVIDERS = {
    "gmail": {"label": "Gmail", "host": "imap.gmail.com", "port": 993, "security": "ssl", "verify_tls": True,
              "auth": "password", "folders": ["INBOX", "[Gmail]/Spam"],
              "help": "Mot de passe d'application Google (myaccount.google.com/apppasswords), pas votre mot de passe habituel."},
    "outlook": {"label": "Hotmail / Outlook.com", "host": "outlook.office365.com", "port": 993, "security": "ssl", "verify_tls": True,
                "auth": "oauth_ms", "folders": ["INBOX", "Junk"],
                "help": "Pas de mot de passe : après l'ajout, cliquez sur « Connecter le compte Microsoft » et saisissez le code affiché."},
    "proton": {"label": "Proton Mail (via Proton Mail Bridge)", "host": "protonmail-bridge", "port": 143, "security": "starttls",
               "verify_tls": False, "auth": "password", "folders": ["INBOX", "Spam"],
               "help": "Service « protonmail-bridge » du docker-compose (profil proton) : serveur protonmail-bridge, port 143. "
                       "Identifiant et mot de passe : ceux affichés par la commande « info » du Bridge, pas votre mot de passe Proton."},
    "icloud": {"label": "iCloud", "host": "imap.mail.me.com", "port": 993, "security": "ssl", "verify_tls": True,
               "auth": "password", "folders": ["INBOX", "Junk"],
               "help": "Mot de passe pour app créé sur account.apple.com."},
    "imap": {"label": "Autre (IMAP)", "host": "", "port": 993, "security": "ssl", "verify_tls": True,
             "auth": "password", "folders": ["INBOX"], "help": ""},
}

HEADER_FIELDS = "FROM SUBJECT DATE LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST LIST-ID PRECEDENCE"
BATCH = 200
MAX_PER_RUN = 30_000
_ANGLE_RE = re.compile(r"<([^>]+)>")
_UID_RE = re.compile(rb"UID (\d+)")
_scan_lock = threading.Lock()
jobs: dict[str, dict] = {}  # travaux de fond (désinscriptions groupées…) affichés dans la page


class MailboxError(Exception):
    pass


class _PinnedIMAP4(imaplib.IMAP4):
    def _create_socket(self, timeout):
        return create_pinned_socket(self.host, self.port, timeout, allow_private=True)


class _PinnedIMAP4_SSL(imaplib.IMAP4_SSL):
    def _create_socket(self, timeout):
        sock = create_pinned_socket(self.host, self.port, timeout, allow_private=True)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


# ------------------------------------------------- Boîte des réglages ----
# La boîte configurée dans Réglages › Relève des réponses apparaît aussi dans « Boîtes mail ».
# Ses identifiants ne sont pas copiés : la connexion est relue dans les réglages à chaque fois.

SETTINGS_DISMISSED = "mailbox_settings_dismissed"  # adresse retirée volontairement de la liste


def settings_address() -> str:
    """Adresse de la boîte des réglages ("" si la relève n'est pas configurée)."""
    if not settings.imap_configured:
        return ""
    user, _ = settings.imap_login
    return (user if "@" in (user or "") else settings.smtp_from or "").strip().lower()


def _provider_for(host: str) -> str:
    host = (host or "").lower()
    for key, preset in PROVIDERS.items():
        if preset["host"] and host == preset["host"]:
            return key
    return "gmail" if host.endswith(("gmail.com", "googlemail.com")) else "imap"


def sync_settings_mailbox(db) -> Mailbox | None:
    """Crée ou met à jour la boîte des réglages dans la liste des boîtes analysées.

    Rien n'est lu tant que l'utilisateur ne lance pas l'analyse (auto_scan désactivé
    à la création). Pas de doublon si la même adresse a déjà été ajoutée à la main.
    """
    linked = db.scalar(select(Mailbox).where(Mailbox.linked_settings.is_(True)))
    address = settings_address()
    if not address:
        return linked
    provider = _provider_for(settings.imap_host)
    if linked is None:
        if kv_get(db, SETTINGS_DISMISSED) == address:
            return None
        if db.scalar(select(Mailbox.id).where(func.lower(Mailbox.address) == address)):
            return None
        preset = PROVIDERS[provider]
        linked = Mailbox(
            label="Boîte des demandes", address=address, provider=provider, linked_settings=True,
            host=settings.imap_host, port=settings.imap_port, security=settings.imap_security, verify_tls=True,
            auth="password", username="", secret_enc="", state={}, auto_scan=False,
            folders=list(preset["folders"]) if provider != "imap" else list(settings.imap_folders or ["INBOX"]),
        )
        db.add(linked)
        db.commit()
        return linked
    if (linked.address, linked.host) != (address, settings.imap_host):
        linked.state = {}  # autre compte : l'analyse repart de zéro
    linked.address, linked.provider = address, provider
    linked.host, linked.port, linked.security = settings.imap_host, settings.imap_port, settings.imap_security
    if db.is_modified(linked):
        db.commit()
    return linked


def dismiss_settings_mailbox(db, mb: Mailbox) -> None:
    kv_set(db, SETTINGS_DISMISSED, mb.address)


def restore_settings_mailbox(db) -> Mailbox | None:
    db.query(KV).filter(KV.key == SETTINGS_DISMISSED).delete()
    db.commit()
    return sync_settings_mailbox(db)


# ------------------------------------------------------------- Connexion ----

def _ssl_context(verify_tls: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify_tls:  # Proton Mail Bridge : certificat auto-signé sur le réseau local
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _account(mb: Mailbox) -> dict:
    """Paramètres de connexion effectifs : ceux de la boîte, ou ceux des réglages pour la boîte liée."""
    if mb.linked_settings:
        if not settings.imap_configured:
            raise MailboxError("La relève n'est plus configurée : renseignez-la dans Réglages › Relève des réponses.")
        user, password = settings.imap_login
        key = "imap_password" if settings.imap_password else "smtp_password"
        if settings_store.secret_unavailable(key):
            raise MailboxError("Mot de passe des réglages illisible : saisissez-le de nouveau dans Réglages.")
        return {"host": settings.imap_host, "port": settings.imap_port, "security": settings.imap_security,
                "verify_tls": True, "auth": "password", "user": user, "secret": lambda: password}
    return {"host": mb.host, "port": mb.port, "security": mb.security, "verify_tls": mb.verify_tls,
            "auth": mb.auth, "user": mb.username or mb.address, "secret": lambda: _secret(mb)}


def _secret(mb: Mailbox) -> str:
    try:
        return crypto.decrypt(mb.secret_enc)
    except crypto.DecryptionError as exc:
        raise MailboxError(
            "Secret de cette boîte illisible : restaurez la clé ENCRYPTION_KEY d'origine avant de reconnecter la boîte."
        ) from exc


def connect(mb: Mailbox, db=None) -> imaplib.IMAP4:
    acc = _account(mb)
    host, port = acc["host"], acc["port"]
    if not host:
        raise MailboxError("Serveur IMAP non renseigné")
    try:
        if acc["security"] == "ssl":
            conn = _PinnedIMAP4_SSL(host, port, ssl_context=_ssl_context(acc["verify_tls"]), timeout=60)
        else:
            conn = _PinnedIMAP4(host, port, timeout=60)
            if acc["security"] == "starttls":
                conn.starttls(ssl_context=_ssl_context(acc["verify_tls"]))
    except (OSError, imaplib.IMAP4.error) as e:
        raise MailboxError(f"Connexion à {host}:{port} impossible : {e}") from e
    try:
        user = acc["user"]
        if acc["auth"] == "oauth_ms":
            token, new_refresh = oauth_ms.access_token(mb.id, oauth_ms.client_id_for(mb.oauth_client_id), mb.oauth_tenant, _secret(mb))
            if new_refresh and db is not None:
                mb.secret_enc = crypto.encrypt(new_refresh)  # Microsoft fait tourner les jetons
                db.commit()
            conn.authenticate("XOAUTH2", lambda _: oauth_ms.xoauth2(user, token))
        else:
            conn.login(user, acc["secret"]())
    except oauth_ms.OAuthError as e:
        conn.logout()
        raise MailboxError(str(e)) from e
    except imaplib.IMAP4.error as e:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
        raise MailboxError(f"Identifiants refusés par {host} : {e}") from e
    return conn


# Serveurs d'envoi des fournisseurs courants (hôte, port, sécurité). Proton : le Bridge envoie sur le port 25.
SMTP_SERVERS = {
    "gmail": ("smtp.gmail.com", 465, "ssl"),
    "outlook": ("smtp-mail.outlook.com", 587, "starttls"),
    "icloud": ("smtp.mail.me.com", 587, "starttls"),
}


def for_address(db, address: str) -> Mailbox | None:
    """Boîte enregistrée pour cette adresse (Boîtes mail), s'il y en a une."""
    a = (address or "").strip().lower()
    return next((mb for mb in db.scalars(select(Mailbox)) if (mb.address or "").strip().lower() == a), None)


def smtp_server(mb: Mailbox) -> tuple[str, int, str, bool]:
    """(hôte, port, sécurité, vérifier le certificat) pour envoyer depuis cette boîte."""
    if mb.auth == "oauth_ms":
        return ("smtp-mail.outlook.com", 587, "starttls", True)
    if mb.provider in SMTP_SERVERS:
        return (*SMTP_SERVERS[mb.provider], True)
    if mb.provider == "proton":
        return (mb.host, 25, "starttls", mb.verify_tls)
    host = re.sub(r"^imap\.", "smtp.", mb.host or "")
    return (host, 587, "starttls", mb.verify_tls)


def send_from(mb: Mailbox, msg, db=None) -> None:
    """Envoie le message depuis cette boîte (mêmes identifiants que pour la lire). Respecte le mode simulation."""
    if settings.dry_run:
        log.info("[DRY_RUN] E-mail non envoyé depuis %s à %s : %s", mb.address, msg["To"], msg["Subject"])
        return
    if mb.linked_settings:
        mailer.send_message(msg)  # boîte des réglages : serveur d'envoi des réglages
        return
    host, port, security, verify = smtp_server(mb)
    if not host:
        raise MailboxError("Serveur d'envoi inconnu pour cette boîte")
    user = mb.username or mb.address
    ctx = _ssl_context(verify)
    try:
        server = (mailer._PinnedSMTP_SSL(host, port, context=ctx, timeout=60) if security == "ssl"
                  else mailer._PinnedSMTP(host, port, timeout=60))
        with server:
            server.ehlo()
            if security == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            if mb.auth == "oauth_ms":
                token, new_refresh = oauth_ms.smtp_access_token(oauth_ms.client_id_for(mb.oauth_client_id), mb.oauth_tenant, _secret(mb))
                if new_refresh and db is not None:
                    mb.secret_enc = crypto.encrypt(new_refresh)
                server.auth("XOAUTH2", lambda challenge=None: oauth_ms.xoauth2(user, token).decode(), initial_response_ok=True)
            else:
                server.login(user, _secret(mb))
            server.send_message(msg, to_addrs=[a for _, a in getaddresses([msg["To"]])])
    except oauth_ms.OAuthError as e:
        raise MailboxError(str(e)) from e
    except smtplib.SMTPAuthenticationError as e:
        hint = (" Microsoft : reconnectez la boîte une fois pour autoriser l'envoi." if mb.auth == "oauth_ms"
                else " Gmail : il faut un mot de passe d'application." if mb.provider == "gmail" else "")
        raise MailboxError(f"Envoi refusé par {host} : {e.smtp_error.decode(errors='replace') if isinstance(e.smtp_error, bytes) else e}.{hint}") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailboxError(f"Envoi depuis {mb.address} impossible ({host}:{port}) : {e}") from e


def test_connection(mb: Mailbox, db=None) -> str:
    conn = connect(mb, db)
    try:
        typ, data = conn.list()
        names = []
        for raw in data or []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            names.append(line.rsplit(' "/" ', 1)[-1].strip('"'))
        return f"Connexion réussie. Dossiers : {', '.join(names[:12])}{'…' if len(names) > 12 else ''}"
    finally:
        conn.logout()


# ---------------------------------------------------------------- Analyse ----

def parse_list_unsubscribe(value: str) -> tuple[str | None, str | None]:
    """Renvoie (url https, adresse mailto) d'un en-tête List-Unsubscribe."""
    url = mailto = None
    for item in _ANGLE_RE.findall(value or "") or [v.strip() for v in (value or "").split(",") if v.strip()]:
        item = item.strip()
        low = item.lower()
        if low.startswith("https://") and not url:
            url = item
        elif low.startswith("http://") and not url:
            url = item
        elif low.startswith("mailto:") and not mailto:
            mailto = item
    return url, mailto


def _parse_headers(raw: bytes) -> dict | None:
    msg = email.message_from_bytes(raw, policy=default_policy)
    lu = str(msg.get("List-Unsubscribe", "") or "")
    bulk = bool(lu) or bool(msg.get("List-Id")) or str(msg.get("Precedence", "")).lower() in ("bulk", "list", "junk")
    if not bulk:
        return None
    addrs = [a for a in getaddresses([str(msg.get("From", ""))]) if "@" in a[1]]
    if not addrs:
        return None
    name, addr = addrs[0]
    try:
        date = parsedate_to_datetime(str(msg.get("Date"))).astimezone(tz=None).replace(tzinfo=None) if msg.get("Date") else None
    except (TypeError, ValueError, IndexError):
        date = None
    url, mailto = parse_list_unsubscribe(lu)
    one_click = "one-click" in str(msg.get("List-Unsubscribe-Post", "")).lower().replace(" ", "")
    return {"address": addr.lower(), "name": name, "subject": str(msg.get("Subject", ""))[:500], "date": date,
            "url": url, "mailto": mailto, "one_click": one_click and bool(url and url.lower().startswith("https://"))}


def _upsert(db, mb: Mailbox, cache: dict, h: dict) -> None:
    s = cache.get(h["address"])
    if s is None:
        s = db.scalars(select(Sender).where(Sender.mailbox_id == mb.id, Sender.address == h["address"])).first()
        if s is None:
            s = Sender(mailbox_id=mb.id, address=h["address"], domain=registered_domain(h["address"].split("@")[-1]),
                       name=h["name"] or "", count=0, status="new")
            db.add(s)
        cache[h["address"]] = s
    s.count = (s.count or 0) + 1
    if h["name"] and not s.name:
        s.name = h["name"]
    d = h["date"]
    if d and (s.first_seen is None or d < s.first_seen):
        s.first_seen = d
    if d and (s.last_seen is None or d >= s.last_seen):
        s.last_seen = d
        s.last_subject = h["subject"]
        if h["url"]:
            s.unsub_url, s.unsub_one_click = h["url"], h["one_click"]
        if h["mailto"]:
            s.unsub_mailto = h["mailto"]
    else:
        s.unsub_url = s.unsub_url or h["url"]
        s.unsub_mailto = s.unsub_mailto or h["mailto"]
        s.unsub_one_click = s.unsub_one_click or h["one_click"]
    if s.status == "unsubscribed" and d and s.status_at and d > s.status_at + timedelta(days=3):
        s.seen_after_unsub = (s.seen_after_unsub or 0) + 1  # continue d'écrire malgré la désinscription


def scan(mailbox_id: int) -> int:
    """Analyse (incrémentale) une boîte. Renvoie le nombre de messages en masse comptés."""
    counted = 0
    with SessionLocal() as db:
        mb = db.get(Mailbox, mailbox_id)
        if mb is None:
            return 0
        mb.scanning, mb.progress, mb.last_error = True, "connexion…", None
        db.commit()
        try:
            conn = connect(mb, db)
            try:
                state = dict(mb.state or {})
                read = 0
                for folder in mb.folders or ["INBOX"]:
                    typ, _ = conn.select(_quote_folder(folder), readonly=True)
                    if typ != "OK":
                        log.info("Dossier %s absent de %s", folder, mb.label)
                        continue
                    validity = (conn.untagged_responses.get("UIDVALIDITY") or [b"0"])[0]
                    validity = validity.decode() if isinstance(validity, bytes) else str(validity)
                    prev = (state.get(folder) or ":0").split(":")
                    last_uid = int(prev[1] or 0) if prev[0] == validity else 0
                    if last_uid:
                        typ, data = conn.uid("SEARCH", None, "UID", f"{last_uid + 1}:*")
                    else:
                        since = (utcnow() - timedelta(days=30 * max(1, mb.months))).strftime("%d-%b-%Y")
                        typ, data = conn.uid("SEARCH", None, "SINCE", since)
                    uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last_uid) if typ == "OK" else []
                    cache: dict = {}
                    for i in range(0, len(uids), BATCH):
                        if read >= MAX_PER_RUN:
                            break  # la suite sera lue à la prochaine analyse
                        chunk = uids[i:i + BATCH]
                        typ, data = conn.uid("FETCH", ",".join(map(str, chunk)), f"(UID BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
                        for part in data or []:
                            if isinstance(part, tuple):
                                h = _parse_headers(part[1])
                                if h:
                                    _upsert(db, mb, cache, h)
                                    counted += 1
                        read += len(chunk)
                        state[folder] = f"{validity}:{chunk[-1]}"
                        mb.state = dict(state)
                        mb.progress = f"{folder} : {min(i + BATCH, len(uids))} / {len(uids)} messages lus"
                        db.commit()
                    if not uids:
                        state[folder] = f"{validity}:{last_uid}"
                mb.state = dict(state)
            finally:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001
                    pass
            mb.last_scan_at = utcnow()
        except MailboxError as e:
            mb.last_error = str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("Analyse de %s", mb.label)
            mb.last_error = f"Erreur pendant l'analyse : {e}"
        finally:
            mb.scanning, mb.progress = False, None
            db.commit()
    return counted


def scan_in_background(mailbox_id: int) -> None:
    def run():
        with _scan_lock:
            scan(mailbox_id)
    threading.Thread(target=run, daemon=True).start()


def scan_all() -> str | None:
    with SessionLocal() as db:
        ids = list(db.scalars(select(Mailbox.id).where(Mailbox.auto_scan.is_(True))))
    total = 0
    with _scan_lock:
        for mid in ids:
            total += scan(mid)
    return f"{total} message(s) en masse analysé(s)" if ids else None


# ---------------------------------------------------------- Désinscription ----

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Ne suit jamais les redirections d'un endpoint List-Unsubscribe non fiable."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Un endpoint public peut rediriger vers localhost ou un service interne.
        # La désinscription reste alors manuelle plutôt que de suivre la redirection.
        return None


def _post_one_click(url: str) -> int:
    """POST RFC 8058 sans cookie, JavaScript ni suivi de redirection."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or not confirm._is_public(parsed.hostname):
        raise urllib.error.URLError("adresse de désinscription non publique ou non HTTPS")
    req = urllib.request.Request(url, data=b"List-Unsubscribe=One-Click", method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Oubliez-moi (RFC 8058)"})
    with safe_urlopen(req, timeout=20, allow_private=False) as resp:
        return resp.status


def unsubscribe(s: Sender) -> tuple[str, str]:
    """Tente une désinscription. Renvoie (nouveau statut, détail)."""
    if s.unsub_one_click and s.unsub_url:
        parsed = urllib.parse.urlparse(s.unsub_url)
        if parsed.scheme != "https" or not parsed.hostname or not confirm._is_public(parsed.hostname):
            return "failed", "adresse de désinscription refusée (non publique)"
        try:
            status = _post_one_click(s.unsub_url)
            if 200 <= status < 300:
                return "unsubscribed", f"désinscription en un clic acceptée (HTTP {status})"
            return "failed", f"le site a répondu {status}"
        except urllib.error.HTTPError as e:
            if s.unsub_url or s.unsub_mailto:
                return "manual", f"désinscription en un clic refusée (HTTP {e.code}) : terminez avec le lien"
            return "failed", f"le site a répondu {e.code}"
        except (OSError, ValueError) as e:
            return "failed", f"site injoignable : {getattr(e, 'reason', e)}"
    if s.unsub_url:
        return "manual", "ce site demande d'ouvrir sa page de désinscription"
    if s.unsub_mailto:
        return "manual", "ce site demande un e-mail de désinscription, à envoyer depuis la boîte concernée"
    return "failed", "aucun moyen de désinscription fourni par l'expéditeur"


def apply_unsubscribe(db, s: Sender) -> str:
    status, detail = unsubscribe(s)
    s.status, s.detail, s.status_at = status, detail, utcnow()
    db.commit()
    return status


# ---------------------------------------------------------------- Rangement ----

def file_sender_messages(db, mb: Mailbox, senders: list[Sender]) -> int:
    """Déplace les e-mails des expéditeurs donnés vers le dossier de rangement de la boîte."""
    if not senders:
        return 0
    target = mb.file_folder or "Newsletters"
    conn = connect(mb, db)
    moved = 0
    try:
        caps = {c.upper() for c in (conn.capabilities or ())}
        conn.create(_quote_folder(target))  # sans effet s'il existe déjà
        for folder in mb.folders or ["INBOX"]:
            if folder == target:
                continue
            typ, _ = conn.select(_quote_folder(folder))
            if typ != "OK":
                continue
            for s in senders:
                typ, data = conn.uid("SEARCH", None, "FROM", f'"{s.address}"')
                uids = (data[0] or b"").split() if typ == "OK" else []
                if not uids:
                    continue
                uid_set = b",".join(uids).decode()
                if "MOVE" in caps:
                    typ, _ = conn.uid("MOVE", uid_set, _quote_folder(target))
                else:
                    typ, _ = conn.uid("COPY", uid_set, _quote_folder(target))
                    if typ == "OK":
                        conn.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
                        if "UIDPLUS" in caps:
                            conn.uid("EXPUNGE", uid_set)
                if typ == "OK":
                    moved += len(uids)
                    s.filed = (s.filed or 0) + len(uids)
        db.commit()
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return moved


# ----------------------------------------------------------- Travaux de fond ----

def run_job(name: str, mailbox_ids_senders: dict[int, list[int]], action: str) -> None:
    """Désinscriptions ou rangements groupés, en arrière-plan avec progression."""
    total = sum(len(v) for v in mailbox_ids_senders.values())
    job = jobs[name] = {"action": action, "done": 0, "total": total, "ok": 0, "manual": 0, "failed": 0, "finished": False, "error": None}

    def run():
        try:
            with SessionLocal() as db:
                for mid, sids in mailbox_ids_senders.items():
                    mb = db.get(Mailbox, mid)
                    senders = [db.get(Sender, sid) for sid in sids]
                    senders = [s for s in senders if s is not None]
                    if action == "unsubscribe":
                        for s in senders:
                            st = apply_unsubscribe(db, s)
                            job["ok" if st == "unsubscribed" else st if st in ("manual", "failed") else "failed"] += 1
                            job["done"] += 1
                    elif action == "file":
                        try:
                            job["ok"] += file_sender_messages(db, mb, senders)
                        except MailboxError as e:
                            job["error"] = str(e)
                        job["done"] += len(senders)
        except Exception as e:  # noqa: BLE001
            log.exception("Travail %s", name)
            job["error"] = str(e)
        finally:
            job["finished"] = True

    threading.Thread(target=run, daemon=True).start()


def catalog_domains(db) -> dict[str, str]:
    """Domaines des courtiers du catalogue, pour signaler un expéditeur qui en est un."""
    out = {}
    for b in db.scalars(select(Broker)):
        for d in (registered_domain((b.email or "").split("@")[-1]), registered_domain(b.web or "")):
            if d:
                out.setdefault(d, b.name)
    return out
