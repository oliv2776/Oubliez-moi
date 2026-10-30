"""Notifications : ntfy, Gotify et récapitulatif par e-mail."""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from .config import settings
from .net import safe_urlopen

log = logging.getLogger(__name__)


def _post(url: str, data: dict, headers: dict | None = None) -> None:
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json", **(headers or {})})
    with safe_urlopen(req, timeout=20, allow_private=True) as resp:
        resp.read()


def push(title: str, message: str, path: str = "/tasks", priority: int = 3) -> list[str]:
    """Envoie une notification sur les canaux configurés. Renvoie la liste des erreurs."""
    errors = []
    click = (settings.app_url.rstrip("/") + path) if settings.app_url else None
    if settings.notify_ntfy_url:
        # Publication JSON (gère les accents) : POST sur la racine du serveur avec le sujet dans le corps.
        parsed = urllib.parse.urlparse(settings.notify_ntfy_url)
        topic = parsed.path.strip("/")
        base = f"{parsed.scheme}://{parsed.netloc}"
        body = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": ["shield"]}
        if click:
            body["click"] = click
        headers = {"Authorization": f"Bearer {settings.notify_ntfy_token}"} if settings.notify_ntfy_token else {}
        try:
            _post(base, body, headers)
        except (OSError, ValueError) as e:
            errors.append(f"ntfy : {e}")
    if settings.notify_gotify_url and settings.notify_gotify_token:
        body = {"title": title, "message": message, "priority": 5 if priority >= 4 else 4}
        if click:
            body["extras"] = {"client::notification": {"click": {"url": click}}}
        url = settings.notify_gotify_url.rstrip("/") + "/message?token=" + urllib.parse.quote(settings.notify_gotify_token)
        try:
            _post(url, body)
        except (OSError, ValueError) as e:
            errors.append(f"Gotify : {e}")
    for e in errors:
        log.warning("Notification non envoyée — %s", e)
    return errors
