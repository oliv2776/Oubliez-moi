"""Migration des comptes : quels services écrivent encore à vos anciennes adresses, et leur passage à un alias.

Principe :
- les boîtes « à quitter » (Gmail, Hotmail…) sont inventoriées : seuls les EN-TÊTES
  sont lus, et seuls les expéditeurs qui sont des services (pas des personnes
  utilisant une messagerie grand public) sont retenus, regroupés par domaine ;
- un service est « migré » dès qu'il écrit à un alias Proton : vu dans l'activité
  des alias SimpleLogin / Proton Pass (simplelogin.py) ou dans la boîte Proton
  lue via Proton Mail Bridge ;
- l'utilisateur garde la main : migré à la main, à abandonner, pas un compte.
"""
from __future__ import annotations

import email
import logging
import re
import threading
from datetime import timedelta
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime

from sqlalchemy import select

from .config import settings
from .db import AccountMigration, Broker, KV, Mailbox, Profile, Request, SessionLocal, kv_get, kv_set, utcnow
from .mailer import _quote_folder, registered_domain

log = logging.getLogger(__name__)

STARTED_KEY = "migration_started"
INVENTORY_MONTHS = 24          # premier inventaire : deux ans d'en-têtes
BATCH = 200
MAX_PER_RUN = 40_000
HEADER_FIELDS = ("FROM SUBJECT DATE TO DELIVERED-TO X-ORIGINAL-TO LIST-UNSUBSCRIBE LIST-ID PRECEDENCE "
                 "X-SIMPLELOGIN-ORIGINAL-FROM X-SIMPLELOGIN-ENVELOPE-TO")

# Messageries grand public : leurs expéditeurs sont des personnes, pas des comptes à migrer.
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "msn.com", "windowslive.com", "ymail.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "aol.fr", "proton.me", "pm.me", "passmail.net", "passmail.com", "passinbox.com", "passfwd.com",
    "simplelogin.com", "simplelogin.co", "simplelogin.fr", "aleeas.com", "slmail.me", "silomails.com", "slmails.com",
    "8shield.net", "dralias.com", "web.de", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "neuf.fr", "laposte.net",
    "bbox.fr", "numericable.fr", "club-internet.fr", "mail.ru", "tuta.io", "tuta.com", "zoho.com", "hey.com",
    "duck.com", "mailbox.org", "posteo.de", "posteo.net", "fastmail.com", "fastmail.fm", "skynet.be", "bluewin.ch",
}
PERSONAL_LABELS = {"hotmail", "outlook", "live", "yahoo", "gmx", "yandex", "protonmail", "tutanota", "libero"}
ACCOUNT_RE = re.compile(
    r"mot de passe|password|passwort|connexion|sign[- ]?in|log[- ]?in|identifiant|v[ée]rifi|verif(y|ication)|"
    r"confirm|votre compte|your account|ihr konto|s[ée]curit|security|commande|order|facture|invoice|re[çc]u|"
    r"receipt|abonnement|subscription|bienvenue|welcome|inscription|code d|one-time|2fa|authentification", re.I)
SKIP_FOLDER_RE = re.compile(r"spam|junk|ind[ée]sirable|trash|corbeille|bin|deleted|supprim|sent|envoy|draft|brouillon", re.I)
_AT_IN_NAME = re.compile(r"([\w.+-]+)\s+at\s+([\w-]+(?:\.[\w-]+)+)", re.I)

_lock = threading.Lock()
progress: dict[int, str] = {}  # mailbox_id -> avancement affiché dans la page


def role_of(mb: Mailbox) -> str:
    """old : adresse à quitter ; new : Proton ; none : ignorée."""
    if mb.migration_role in ("old", "new", "none"):
        return mb.migration_role
    return "new" if mb.provider == "proton" else "old"


def is_personal_domain(domain: str) -> bool:
    domain = (domain or "").lower()
    return domain in PERSONAL_DOMAINS or domain.split(".", 1)[0] in PERSONAL_LABELS


def _original_sender(msg) -> tuple[str, str]:
    """(adresse, nom) de l'expéditeur réel, y compris pour un message relayé par un alias SimpleLogin / Pass."""
    for header in ("X-SimpleLogin-Original-From", "From"):
        pairs = [p for p in getaddresses([str(msg.get(header, "") or "")]) if "@" in p[1]]
        if not pairs:
            continue
        name, addr = pairs[0]
        domain = addr.rsplit("@", 1)[-1].lower()
        if header == "From" and is_personal_domain(registered_domain(domain)):
            # Format des alias : « Amazon - noreply at amazon.fr <reverse@alias> » ou « x@y via SimpleLogin »
            hit = _AT_IN_NAME.search(name or "")
            if hit:
                return f"{hit.group(1)}@{hit.group(2)}".lower(), name.split(" - ")[0].strip()
            via = re.search(r"([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", name or "")
            if via:
                return via.group(1).lower(), ""
        return addr.lower(), name or ""
    return "", ""


def parse(raw: bytes) -> dict | None:
    msg = email.message_from_bytes(raw, policy=default_policy)
    sender, name = _original_sender(msg)
    if not sender or sender.split("@")[0] in ("mailer-daemon", "postmaster"):
        return None
    recipients = []
    for header in ("X-SimpleLogin-Envelope-To", "Delivered-To", "X-Original-To", "To"):
        for _, addr in getaddresses([str(v) for v in msg.get_all(header, []) or []]):
            addr = addr.lower()
            if "@" in addr and addr not in recipients:
                recipients.append(addr)
    try:
        date = parsedate_to_datetime(str(msg.get("Date"))).astimezone(tz=None).replace(tzinfo=None) if msg.get("Date") else None
    except (TypeError, ValueError, IndexError):
        date = None
    subject = str(msg.get("Subject", "") or "")
    bulk = bool(msg.get("List-Unsubscribe") or msg.get("List-Id")) or \
        str(msg.get("Precedence", "")).lower() in ("bulk", "list", "junk")
    return {"sender": sender, "name": name, "domain": registered_domain(sender.rsplit("@", 1)[-1]), "date": date,
            "subject": subject[:500], "recipients": recipients, "bulk": bulk, "account": bool(ACCOUNT_RE.search(subject))}


def own_addresses(db) -> set[str]:
    out = {(a or "").lower() for a in (settings.smtp_from, settings.imap_user, settings.smtp_user) if a and "@" in a}
    for p in db.scalars(select(Profile)):
        out |= {e.lower() for e in (p.emails or []) if "@" in e}
    out |= {m.address.lower() for m in db.scalars(select(Mailbox)) if m.address}
    return out


def excluded_domains(db) -> set[str]:
    """Vos propres domaines et ceux des courtiers à qui une demande RGPD a été envoyée."""
    out = {registered_domain(a.rsplit("@", 1)[-1]) for a in own_addresses(db)}
    for email_addr, web in db.execute(select(Broker.email, Broker.web).join(Request, Request.broker_id == Broker.id).distinct()):
        for value in (email_addr, web):
            if value:
                out.add(registered_domain(value.rsplit("@", 1)[-1].split("//")[-1].split("/")[0]))
    return {d for d in out if d}


def _account(db, cache: dict, domain: str) -> AccountMigration:
    acc = cache.get(domain)
    if acc is None:
        acc = db.scalar(select(AccountMigration).where(AccountMigration.domain == domain))
        if acc is None:
            acc = AccountMigration(domain=domain, name="", sample_sender="", old_count=0, old_account_count=0,
                                   new_count=0, status="todo")
            db.add(acc)
        cache[domain] = acc
    return acc


def record(db, cache: dict, mb: Mailbox, role: str, h: dict, mine: set[str], excluded: set[str]) -> bool:
    """Ajoute un message à l'inventaire. Renvoie False s'il ne concerne pas un service."""
    domain = h["domain"]
    if not domain or is_personal_domain(domain) or domain in excluded or h["sender"] in mine:
        return False
    acc = _account(db, cache, domain)
    if h["name"] and not acc.name:
        acc.name = h["name"][:300]
    if not acc.sample_sender or h["account"]:
        acc.sample_sender = h["sender"][:300]
    d = h["date"]
    if role == "old":
        acc.old_count = (acc.old_count or 0) + 1
        if h["account"]:
            acc.old_account_count = (acc.old_account_count or 0) + 1
        if d and (acc.old_first_seen is None or d < acc.old_first_seen):
            acc.old_first_seen = d
        if d and (acc.old_last_seen is None or d >= acc.old_last_seen):
            acc.old_last_seen = d
            acc.old_address = next((r for r in h["recipients"] if r in mine), mb.address or "")[:300]
            if h["account"] or not acc.last_subject:
                acc.last_subject = h["subject"]
    else:
        acc.new_count = (acc.new_count or 0) + 1
        if d and (acc.new_first_seen is None or d < acc.new_first_seen):
            acc.new_first_seen = d
        if d and (acc.new_last_seen is None or d >= acc.new_last_seen):
            acc.new_last_seen = d
        alias = next((r for r in h["recipients"] if r != (mb.address or "").lower()), None)
        if alias and not acc.alias:
            acc.alias, acc.alias_source = alias[:300], "matched"
    acc.refresh_status()
    return True


def inventory(mailbox_id: int, months: int = INVENTORY_MONTHS) -> int:
    """Inventaire (incrémental) d'une boîte. Renvoie le nombre de messages de services comptés."""
    from . import mailboxes  # import tardif : mailboxes importe déjà beaucoup de modules
    counted = 0
    with SessionLocal() as db:
        mb = db.get(Mailbox, mailbox_id)
        if mb is None or role_of(mb) == "none":
            return 0
        role, mine, excluded = role_of(mb), own_addresses(db), excluded_domains(db)
        progress[mailbox_id] = f"{mb.label} : connexion…"
        try:
            conn = mailboxes.connect(mb, db)
        except mailboxes.MailboxError as e:
            progress.pop(mailbox_id, None)
            mb.last_error = str(e)
            db.commit()
            return 0
        cache: dict = {}
        read = 0
        try:
            # Position de lecture rangée à part (KV) : l'analyse des expéditeurs garde la sienne.
            for folder in mb.folders or ["INBOX"]:
                if SKIP_FOLDER_RE.search(folder) and folder.upper() != "INBOX":
                    continue
                typ, _ = conn.select(_quote_folder(folder), readonly=True)
                if typ != "OK":
                    continue
                validity = (conn.untagged_responses.get("UIDVALIDITY") or [b"0"])[0]
                validity = validity.decode() if isinstance(validity, bytes) else str(validity)
                key = f"inv:{mb.id}:{folder}"
                prev = (kv_get(db, key) or ":0").split(":")
                last_uid = int(prev[1] or 0) if prev[0] == validity else 0
                if last_uid:
                    typ, data = conn.uid("SEARCH", None, "UID", f"{last_uid + 1}:*")
                else:
                    since = (utcnow() - timedelta(days=30 * months)).strftime("%d-%b-%Y")
                    typ, data = conn.uid("SEARCH", None, "SINCE", since)
                uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last_uid) if typ == "OK" else []
                for i in range(0, len(uids), BATCH):
                    if read >= MAX_PER_RUN:
                        break  # la suite sera lue au prochain passage
                    chunk = uids[i:i + BATCH]
                    typ, data = conn.uid("FETCH", ",".join(map(str, chunk)), f"(UID BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
                    for part in data or []:
                        if isinstance(part, tuple):
                            h = parse(part[1])
                            if h and record(db, cache, mb, role, h, mine, excluded):
                                counted += 1
                    read += len(chunk)
                    kv_set(db, key, f"{validity}:{chunk[-1]}")
                    progress[mailbox_id] = f"{mb.label} : {min(i + BATCH, len(uids))} / {len(uids)} messages lus ({folder})"
                    db.commit()
                if not uids:
                    kv_set(db, key, f"{validity}:{last_uid}")
            db.commit()
        finally:
            progress.pop(mailbox_id, None)
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
    return counted


def inventory_all() -> str | None:
    """Passage de nuit : nouveaux messages des boîtes à quitter et de la boîte Proton, puis alias."""
    from . import simplelogin
    with SessionLocal() as db:
        if not kv_get(db, STARTED_KEY):
            return None
        ids = [m.id for m in db.scalars(select(Mailbox)) if role_of(m) != "none"]
    total = 0
    with _lock:
        for mid in ids:
            total += inventory(mid)
        sync = simplelogin.sync() if simplelogin.configured() else None
    return f"{total} message(s) de services inventorié(s)" + (f" ; alias : {sync}" if sync else "")


def start_in_background(mailbox_ids: list[int]) -> None:
    with SessionLocal() as db:
        kv_set(db, STARTED_KEY, utcnow().isoformat())
        db.commit()

    def run():
        from . import simplelogin
        with _lock:
            for mid in mailbox_ids:
                try:
                    inventory(mid)
                except Exception:  # noqa: BLE001
                    log.exception("Inventaire de la boîte %s", mid)
            if simplelogin.configured():
                try:
                    simplelogin.sync()
                except simplelogin.SimpleLoginError as e:
                    log.warning("Synchronisation des alias : %s", e)
    threading.Thread(target=run, daemon=True).start()


def running() -> bool:
    return _lock.locked()


def reset(db) -> None:
    """Oublie l'inventaire (pas les alias eux-mêmes) pour repartir de zéro."""
    db.query(AccountMigration).delete()
    db.query(KV).filter((KV.key == STARTED_KEY) | KV.key.like("inv:%")).delete(synchronize_session=False)
    db.commit()
