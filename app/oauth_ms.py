"""Connexion OAuth 2.0 à Microsoft (Hotmail, Outlook.com, Live, Microsoft 365) pour lire une boîte en IMAP.

Microsoft n'accepte plus de mot de passe (ni de mot de passe d'application) en
IMAP depuis septembre 2024. On utilise le flux « code d'appareil » : Oubliez-moi
affiche un code, vous l'entrez sur microsoft.com/devicelogin, et Microsoft
délivre un jeton de rafraîchissement, conservé chiffré.

Microsoft n'accepte que les applications déclarées chez lui, chacune avec son
identifiant (client ID) — comme Apple Mail ou Thunderbird. Oubliez-moi a le sien,
déclaré une fois pour tout le projet (PROJECT_CLIENT_ID) : l'utilisateur n'a
alors qu'à cliquer sur « Connecter » et saisir le code. MS_CLIENT_ID (.env) ou
l'identifiant saisi pour une boîte le remplacent si besoin. Un identifiant
d'application public n'est pas un secret : il n'autorise rien à lui seul.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .net import safe_urlopen

AUTHORITY = os.environ.get("MS_AUTHORITY", "https://login.microsoftonline.com")
# Identifiant de l'application « Oubliez-moi » déclarée dans Microsoft Entra (comptes
# personnels et professionnels, flux clients publics autorisés, sans secret client).
# Valeur publique par nature, comme celle d'Apple Mail ou de Thunderbird.
PROJECT_CLIENT_ID = "40870361-82c7-42f9-8e99-594cc906acf8"
ALLOW_PRIVATE = False  # surchargeable uniquement dans les tests de flux OAuth local
SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

_access_cache: dict[int, tuple[str, float]] = {}  # mailbox_id -> (jeton d'accès, expiration)
pending: dict[int, dict] = {}                      # mailbox_id -> flux en cours


class OAuthError(Exception):
    pass


def _post(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with safe_urlopen(req, timeout=30, allow_private=ALLOW_PRIVATE) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())  # Microsoft renvoie les erreurs du flux en JSON (400)
        except ValueError:
            raise OAuthError(f"Microsoft a répondu {e.code}") from e
    except OSError as e:
        raise OAuthError(f"Microsoft injoignable : {getattr(e, 'reason', e)}") from e


def default_client_id() -> str:
    """Identifiant utilisé quand une boîte n'en précise pas : .env, sinon celui du projet."""
    return os.environ.get("MS_CLIENT_ID", "").strip() or PROJECT_CLIENT_ID


def client_id_for(own: str | None) -> str:
    return (own or "").strip() or default_client_id()


def _endpoint(tenant: str, path: str) -> str:
    return f"{AUTHORITY.rstrip('/')}/{tenant or 'consumers'}/oauth2/v2.0/{path}"


def start(mailbox_id: int, client_id: str, tenant: str) -> dict:
    """Démarre la connexion : renvoie le code à saisir et l'adresse où le saisir."""
    if not client_id:
        raise OAuthError("Aucun identifiant d'application Microsoft : indiquez-en un dans « Utiliser votre propre identifiant »")
    data = _post(_endpoint(tenant, "devicecode"), {"client_id": client_id, "scope": SCOPE})
    if "device_code" not in data:
        raise OAuthError(data.get("error_description") or data.get("error") or "réponse inattendue de Microsoft")
    flow = {
        "device_code": data["device_code"], "user_code": data["user_code"],
        "uri": data.get("verification_uri", "https://microsoft.com/devicelogin"),
        "interval": int(data.get("interval", 5)), "expires": time.time() + int(data.get("expires_in", 900)),
        "client_id": client_id, "tenant": tenant, "next_poll": 0.0,
    }
    pending[mailbox_id] = flow
    return flow


def poll(mailbox_id: int) -> tuple[str, str | None]:
    """Interroge Microsoft. Renvoie (état, jeton de rafraîchissement) ; état : pending | ok | expired | denied | error:<msg>."""
    flow = pending.get(mailbox_id)
    if not flow:
        return "expired", None
    if time.time() > flow["expires"]:
        pending.pop(mailbox_id, None)
        return "expired", None
    if time.time() < flow["next_poll"]:
        return "pending", None
    flow["next_poll"] = time.time() + flow["interval"]
    data = _post(_endpoint(flow["tenant"], "token"),
                 {"grant_type": DEVICE_GRANT, "client_id": flow["client_id"], "device_code": flow["device_code"]})
    if "refresh_token" in data:
        pending.pop(mailbox_id, None)
        if "access_token" in data:
            _access_cache[mailbox_id] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 120)
        return "ok", data["refresh_token"]
    err = data.get("error", "")
    if err == "authorization_pending":
        return "pending", None
    if err == "slow_down":
        flow["interval"] += 5
        return "pending", None
    pending.pop(mailbox_id, None)
    if err == "expired_token":
        return "expired", None
    if err in ("access_denied", "authorization_declined"):
        return "denied", None
    return f"error:{data.get('error_description') or err or 'erreur inconnue'}", None


def access_token(mailbox_id: int, client_id: str, tenant: str, refresh_token: str) -> tuple[str, str | None]:
    """Jeton d'accès valide (renouvelé si besoin). Renvoie (jeton, nouveau jeton de rafraîchissement éventuel)."""
    cached = _access_cache.get(mailbox_id)
    if cached and cached[1] > time.time():
        return cached[0], None
    if not refresh_token:
        raise OAuthError("Compte Microsoft non connecté : cliquez sur « Connecter le compte Microsoft »")
    data = _post(_endpoint(tenant, "token"), {
        "grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token, "scope": SCOPE,
    })
    if "access_token" not in data:
        raise OAuthError("Connexion Microsoft expirée ou révoquée : reconnectez le compte "
                         f"({data.get('error_description') or data.get('error') or 'refus'})")
    _access_cache[mailbox_id] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 120)
    return data["access_token"], data.get("refresh_token")


def forget(mailbox_id: int) -> None:
    _access_cache.pop(mailbox_id, None)
    pending.pop(mailbox_id, None)


def xoauth2(user: str, token: str) -> bytes:
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()
