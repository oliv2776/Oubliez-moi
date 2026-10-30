"""Réglages modifiables depuis l'interface, conservés dans la base.

Les valeurs du fichier .env servent de valeurs par défaut ; celles enregistrées
depuis la page Réglages les remplacent.
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import delete, select

from . import crypto
from .config import EDITABLE, SECRET_KEYS, settings
from .db import KV, SessionLocal, kv_set

log = logging.getLogger(__name__)

PREFIX = "cfg:"
ENV_DEFAULTS = {k: getattr(settings, k) for k in EDITABLE}  # capturé avant toute surcharge
SECURITY_CHOICES = {"starttls", "ssl", "none"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _dump(key: str, value) -> str:
    """Valeur prête à stocker : les secrets sont chiffrés."""
    return json.dumps(crypto.encrypt(value) if key in SECRET_KEYS else value)


def apply_overrides() -> None:
    """Charge les réglages de l'interface ; chiffre au passage les secrets encore stockés en clair."""
    migrated = 0
    with SessionLocal() as db:
        rows = db.scalars(select(KV).where(KV.key.like(f"{PREFIX}%"))).all()
        for row in rows:
            key = row.key[len(PREFIX):]
            if key not in EDITABLE:
                continue
            try:
                value = json.loads(row.value)
            except ValueError:
                continue
            if key in SECRET_KEYS:
                if value and not crypto.is_encrypted(value):
                    row.value = _dump(key, value)  # ancienne valeur en clair : on la chiffre
                    migrated += 1
                try:
                    value = crypto.decrypt(value)
                except crypto.DecryptionError:
                    # Ne jamais remplacer un secret chiffré illisible par une chaîne vide.
                    # On conserve la valeur d'environnement et la valeur chiffrée en base.
                    log.exception("Réglage secret %s illisible : clé de chiffrement incorrecte", key)
                    continue
            setattr(settings, key, value)
        if migrated:
            db.commit()
    return migrated


def overridden_keys() -> set[str]:
    with SessionLocal() as db:
        return {k[len(PREFIX):] for k in db.scalars(select(KV.key).where(KV.key.like(f"{PREFIX}%")))}


def _coerce(key: str, raw: str):
    kind = EDITABLE[key]
    raw = (raw or "").strip()
    if kind is bool:
        return raw.lower() in {"1", "true", "on", "yes", "oui"}
    if kind is int:
        if not raw.isdigit():
            raise ValueError(f"« {key} » doit être un nombre entier")
        return int(raw)
    if kind is list:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def save_from_form(form: dict) -> list[str]:
    """Valide et enregistre les champs reçus. Renvoie la liste des erreurs (vide si OK)."""
    form = dict(form)
    # Choix du modèle d'IA : « Autre » → champ libre ; modèle connu → son type est imposé.
    if form.get("llm_model") == "__custom__":
        form["llm_model"] = form.get("llm_model_custom", "")
    if form.get("llm_model"):
        from .llm import catalog_kind
        kind = catalog_kind(form["llm_model"])
        if kind:
            form["llm_mode"] = kind
    values, errors = {}, []
    for key, kind in EDITABLE.items():
        if kind is bool:
            values[key] = _coerce(key, form.get(key, ""))  # case décochée = absente du formulaire
            continue
        if key not in form:
            continue
        if key in SECRET_KEYS and form.get(f"clear_{key}"):
            values[key] = ""
            continue
        if key in SECRET_KEYS and not str(form[key]).strip():
            continue  # champ mot de passe laissé vide : on conserve l'actuel
        try:
            values[key] = _coerce(key, form[key])
        except ValueError as e:
            errors.append(str(e))

    if "backup_schedule" in values and values["backup_schedule"] not in {"off", "daily", "weekly"}:
        errors.append("Fréquence de sauvegarde inconnue")
    if "backup_keep" in values and not 1 <= values["backup_keep"] <= 365:
        errors.append("Le nombre de sauvegardes conservées doit être compris entre 1 et 365")
    if "llm_mode" in values and values["llm_mode"] not in {"decision", "chat"}:
        errors.append("Type de modèle inconnu")
    if "imap_file_mode" in values and values["imap_file_mode"] not in {"off", "move", "label"}:
        errors.append("Mode de rangement inconnu")
    if values.get("imap_file_mode", settings.imap_file_mode) != "off" and not values.get("imap_file_folder", settings.imap_file_folder):
        errors.append("Indiquez le nom du dossier ou du libellé de rangement")
    for k in ("llm_url", "notify_ntfy_url", "notify_gotify_url", "app_url"):
        if values.get(k) and not re.match(r"^https?://", values[k]):
            errors.append(f"L'adresse « {values[k]} » doit commencer par http:// ou https://")
        if values.get(k):
            values[k] = values[k].rstrip("/")
    for k in ("smtp_security", "imap_security"):
        if k in values and values[k] not in SECURITY_CHOICES:
            errors.append(f"Sécurité inconnue : {values[k]}")
    if values.get("smtp_from") and not EMAIL_RE.match(values["smtp_from"]):
        errors.append("L'adresse d'expédition n'est pas une adresse e-mail valide")
    for k in ("smtp_port", "imap_port"):
        if k in values and not 0 < values[k] < 65536:
            errors.append(f"Port invalide : {values[k]}")
    if "send_per_hour" in values and not 1 <= values["send_per_hour"] <= 3600:
        errors.append("Le débit doit être compris entre 1 et 3600 e-mails par heure")
    if "imap_poll_minutes" in values and not 1 <= values["imap_poll_minutes"] <= 1440:
        errors.append("La fréquence de relève doit être comprise entre 1 et 1440 minutes")
    if values.get("imap_host") and not values.get("imap_folders", settings.imap_folders):
        values["imap_folders"] = ["INBOX"]
    if errors:
        return errors

    with SessionLocal() as db:
        for key, value in values.items():
            kv_set(db, PREFIX + key, _dump(key, value))
            setattr(settings, key, value)
        db.commit()
    return []


def reset_to_env() -> None:
    with SessionLocal() as db:
        db.execute(delete(KV).where(KV.key.like(f"{PREFIX}%")))
        db.commit()
    for key, value in ENV_DEFAULTS.items():
        setattr(settings, key, value)
