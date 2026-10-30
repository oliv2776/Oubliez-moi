"""Chiffrement des secrets stockés (mots de passe de messagerie, jetons…).

Les secrets sont chiffrés avec Fernet (AES-128-CBC + HMAC-SHA256) avant d'être
écrits dans la base. La clé ne se trouve jamais dans la base :

- variable d'environnement ENCRYPTION_KEY (recommandé : dans le fichier .env,
  hors du volume de données et donc hors des sauvegardes) ;
- à défaut, fichier /data/.encryption_key généré au premier démarrage
  (droits 600), qui n'est pas inclus dans les sauvegardes.

Une valeur chiffrée commence par « enc:v1: ». Les anciennes valeurs en clair
restent lisibles et sont chiffrées dès qu'elles sont relues.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

log = logging.getLogger(__name__)
PREFIX = "enc:v1:"
KEY_FILE = settings.data_dir / ".encryption_key"

_fernet: Fernet | None = None
key_source = ""          # "env" ou "fichier"
decrypt_errors = 0       # > 0 : la clé ne correspond pas à certaines données (clé changée ?)


class DecryptionError(RuntimeError):
    """Un secret chiffré existe mais ne peut pas être déchiffré avec la clé active."""


def _from_passphrase(value: str) -> bytes:
    """Accepte une clé Fernet ou n'importe quelle phrase secrète (dérivée en clé de 32 octets)."""
    try:
        raw = base64.urlsafe_b64decode(value.encode())
        if len(raw) == 32:
            return value.encode()
    except (ValueError, TypeError):
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest())


def _load() -> Fernet:
    global _fernet, key_source
    if _fernet is not None:
        return _fernet
    env_key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if env_key:
        key, key_source = _from_passphrase(env_key), "env"
    else:
        if KEY_FILE.exists():
            key = KEY_FILE.read_text().strip().encode()
        else:
            key = Fernet.generate_key()
            KEY_FILE.write_text(key.decode())
            KEY_FILE.chmod(0o600)
            log.info("Clé de chiffrement générée dans %s", KEY_FILE)
        key_source = "fichier"
    _fernet = Fernet(key)
    return _fernet


def fernet() -> Fernet:
    return _load()


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)


def encrypt(value: str | None) -> str:
    """Chiffre une chaîne. Une chaîne vide reste vide (rien à protéger)."""
    if not value:
        return ""
    if is_encrypted(value):
        return value
    return PREFIX + _load().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str:
    """Déchiffre une valeur ; une valeur en clair (ancienne version) est renvoyée telle quelle."""
    global decrypt_errors
    if not value:
        return ""
    if not is_encrypted(value):
        return value
    try:
        return _load().decrypt(value[len(PREFIX):].encode()).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        decrypt_errors += 1
        log.error("Impossible de déchiffrer un secret : la clé de chiffrement a-t-elle changé ?")
        raise DecryptionError("Secret chiffré illisible : vérifiez ENCRYPTION_KEY et restaurez la clé d'origine") from exc


def encrypt_bytes(data: bytes) -> bytes:
    return _load().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    global decrypt_errors
    try:
        return _load().decrypt(data)
    except InvalidToken as exc:
        decrypt_errors += 1
        raise DecryptionError("Données chiffrées illisibles : vérifiez la clé de chiffrement") from exc


def export_key() -> str:
    """Clé à conserver hors du serveur (gestionnaire de mots de passe) pour pouvoir restaurer une sauvegarde."""
    _load()
    if key_source == "env":
        return ""
    return KEY_FILE.read_text().strip()
