"""Alias Proton Pass / SimpleLogin via l'API SimpleLogin (les alias de Proton Pass y sont synchronisés).

Utilisé par la migration des comptes pour :
- lister vos alias et repérer, grâce à leur activité, quel service écrit déjà à quel alias ;
- créer un alias par service (préfixe = nom du service), visible ensuite dans Proton Pass.

La clé d'API se crée sur app.simplelogin.io › Paramètres › Clé d'API ; elle est
conservée chiffrée. Documentation de l'API : github.com/simple-login/app/blob/master/docs/api.md
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from sqlalchemy import select

from . import settings_store
from .config import settings
from .db import AccountMigration, SessionLocal
from .mailer import registered_domain
from .net import safe_urlopen

ALLOW_PRIVATE = False  # uniquement pour les tests (serveur local)
MAX_PAGES = 100        # 20 alias par page
MAX_ACTIVITY_CALLS = 60


class SimpleLoginError(Exception):
    pass


def configured() -> bool:
    return bool(settings.simplelogin_api_key)


def _call(method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
    if settings_store.secret_unavailable("simplelogin_api_key"):
        raise SimpleLoginError("Clé d'API SimpleLogin illisible : saisissez-la de nouveau.")
    if not settings.simplelogin_api_key:
        raise SimpleLoginError("Aucune clé d'API SimpleLogin enregistrée.")
    url = (settings.simplelogin_url or "https://app.simplelogin.io").rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Authentication": settings.simplelogin_api_key, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with safe_urlopen(req, timeout=30, allow_private=ALLOW_PRIVATE) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error", "")
        except ValueError:
            detail = ""
        if e.code == 401:
            raise SimpleLoginError("Clé d'API SimpleLogin refusée : vérifiez-la ou créez-en une nouvelle.") from e
        raise SimpleLoginError(f"SimpleLogin a répondu {e.code}{f' : {detail}' if detail else ''}") from e
    except (OSError, ValueError) as e:
        raise SimpleLoginError(f"SimpleLogin injoignable : {getattr(e, 'reason', e)}") from e


def user_info() -> dict:
    return _call("GET", "/api/user_info")


def list_aliases() -> list[dict]:
    out = []
    for page in range(MAX_PAGES):
        batch = _call("GET", "/api/v2/aliases", {"page_id": page}).get("aliases", [])
        out += batch
        if len(batch) < 20:
            break
    return out


def activities(alias_id: int) -> list[dict]:
    return _call("GET", f"/api/aliases/{alias_id}/activities", {"page_id": 0}).get("activities", [])


def _ts(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def _seen(acc: AccountMigration, when: datetime | None, count: int = 1) -> None:
    if when is None:
        return
    acc.new_count = max(acc.new_count or 0, count)
    if acc.new_first_seen is None or when < acc.new_first_seen:
        acc.new_first_seen = when
    if acc.new_last_seen is None or when > acc.new_last_seen:
        acc.new_last_seen = when


def _label(domain: str) -> str:
    """Nom court du service pour un préfixe d'alias : « amazon » pour amazon.fr."""
    return re.sub(r"[^a-z0-9]", "", domain.split(".", 1)[0].lower())[:30] or "compte"


def _link(acc: AccountMigration, alias: dict, source: str) -> None:
    if acc.alias and acc.alias.lower() != alias["email"].lower() and acc.alias_source in ("created", "manual"):
        return  # ne remplace jamais un alias choisi ou créé exprès
    acc.alias, acc.alias_id = alias["email"], alias["id"]
    if not acc.alias_source or acc.alias_source == "guessed" or source != "guessed":
        acc.alias_source = source


def sync() -> str:
    """Relie vos alias aux comptes à migrer et repère les services qui leur écrivent déjà."""
    aliases = list_aliases()
    linked = migrated = added = 0
    with SessionLocal() as db:
        accounts = {a.domain: a for a in db.scalars(select(AccountMigration))}
        by_alias = {a.alias.lower(): a for a in accounts.values() if a.alias}
        by_id = {a.alias_id: a for a in accounts.values() if a.alias_id}
        for alias in aliases:
            email_addr = (alias.get("email") or "").lower()
            act = alias.get("latest_activity") or {}
            contact = ((act.get("contact") or {}).get("email") or "").lower()
            sender_domain = registered_domain(contact.rsplit("@", 1)[-1]) if "@" in contact else ""
            acc = by_id.get(alias.get("id")) or by_alias.get(email_addr)
            if acc is None and act.get("action") == "forward" and sender_domain:
                acc = accounts.get(sender_domain)
                if acc is None:
                    from .migration import is_personal_domain
                    if not is_personal_domain(sender_domain):
                        # Service qui n'écrit qu'à l'alias : déjà migré, on l'ajoute pour avoir la vue complète.
                        acc = AccountMigration(domain=sender_domain, name="", sample_sender=contact, old_count=0,
                                               old_account_count=0, new_count=0, status="migrated")
                        db.add(acc)
                        accounts[sender_domain] = acc
                        added += 1
            if acc is None:
                note = f"{alias.get('note') or ''} {alias.get('name') or ''}".lower()
                acc = next((a for a in accounts.values() if not a.alias and a.domain in note), None)
                if acc is None:  # préfixe proposé par Proton Pass : nom du site (amazon.x1y2@passmail.net)
                    local = email_addr.split("@")[0]
                    acc = next((a for a in accounts.values()
                                if not a.alias and a.alias_source != "rejected" and len(_label(a.domain)) >= 4
                                and local.startswith(_label(a.domain))), None)
                    if acc is not None:
                        _link(acc, alias, "guessed")
                        linked += 1
                        acc.refresh_status()
                        continue
                if acc is None:
                    continue
            was_migrated = acc.status == "migrated"
            if acc.alias_id != alias.get("id"):
                _link(acc, alias, "matched")
                linked += 1
            if act.get("action") == "forward" and sender_domain == acc.domain:
                _seen(acc, _ts(act.get("timestamp")), alias.get("nb_forward") or 1)
            acc.refresh_status()
            migrated += int(acc.status == "migrated" and not was_migrated)
        # Alias reliés mais sans preuve d'envoi du service : on regarde leur historique.
        calls = 0
        for acc in accounts.values():
            if not acc.alias_id or acc.new_last_seen or acc.user_status or calls >= MAX_ACTIVITY_CALLS:
                continue
            calls += 1
            try:
                history = activities(acc.alias_id)
            except SimpleLoginError:
                continue  # alias supprimé entre-temps, erreur passagère : on réessaiera la nuit suivante
            for item in history:
                sender = (item.get("from") or "").lower()
                if item.get("action") == "forward" and "@" in sender and registered_domain(sender.rsplit("@", 1)[-1]) == acc.domain:
                    _seen(acc, _ts(item.get("timestamp")))
            if acc.refresh_status() == "migrated":
                migrated += 1
        db.commit()
    return f"{len(aliases)} alias lus, {linked} relié(s), {migrated} compte(s) migré(s), {added} ajouté(s)"


def create_alias(acc: AccountMigration) -> str:
    """Crée « <service>.xxx@domaine-d-alias » pour ce compte et le relie. Renvoie l'adresse."""
    options = _call("GET", "/api/v5/alias/options", {"hostname": acc.domain})
    if not options.get("can_create", True):
        raise SimpleLoginError("Limite d'alias atteinte sur votre offre (Proton Pass gratuit : 10 alias).")
    suffixes = options.get("suffixes") or []
    if not suffixes:
        raise SimpleLoginError("SimpleLogin ne propose aucun domaine d'alias pour ce compte.")
    # Domaines de Proton Pass d'abord, puis le premier domaine utilisable.
    suffix = next((s for s in suffixes if "passmail" in s.get("suffix", "") or "passinbox" in s.get("suffix", "")), None) \
        or next((s for s in suffixes if not s.get("is_premium")), suffixes[0])
    mailboxes = _call("GET", "/api/v2/mailboxes").get("mailboxes", [])
    mailbox = next((m for m in mailboxes if m.get("default")), mailboxes[0] if mailboxes else None)
    if mailbox is None:
        raise SimpleLoginError("Aucune boîte de réception vérifiée dans SimpleLogin.")
    prefix = re.sub(r"[^a-z0-9._-]", "", (options.get("prefix_suggestion") or _label(acc.domain)).lower())[:40] or _label(acc.domain)
    alias = _call("POST", "/api/v3/alias/custom/new", {"hostname": acc.domain}, {
        "alias_prefix": prefix, "signed_suffix": suffix["signed_suffix"], "mailbox_ids": [mailbox["id"]],
        "note": f"Créé par Oubliez-moi pour {acc.domain}",
    })
    if not alias.get("email"):
        raise SimpleLoginError("Réponse inattendue de SimpleLogin à la création de l'alias.")
    acc.alias, acc.alias_id, acc.alias_source = alias["email"], alias.get("id"), "created"
    acc.refresh_status()
    return alias["email"]
