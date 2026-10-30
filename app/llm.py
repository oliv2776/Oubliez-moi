"""Classement des réponses ambiguës par un modèle de langage local (Ollama).

Utilisé seulement quand les règles par mots-clés ne savent pas trancher.
Les réponses des courtiers contiennent vos données : le modèle doit tourner
chez vous (Ollama), rien n'est envoyé à un service externe.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from .config import settings

log = logging.getLogger(__name__)

CATEGORIES = {
    "completed_deleted": "le courtier confirme avoir supprimé les données ou ajouté la personne à une liste d'opposition",
    "completed_no_data": "le courtier dit ne détenir aucune donnée sur la personne",
    "id_request": "le courtier exige une pièce d'identité ou un justificatif d'identité",
    "confirm_link": "le courtier demande de cliquer sur un lien pour confirmer la demande ou l'adresse e-mail",
    "needs_action": "le courtier demande une autre action : remplir un formulaire, utiliser un portail, fournir une information",
    "extension": "le courtier annonce qu'il prolonge le délai de réponse",
    "legit_refusal": "le courtier refuse en invoquant son intérêt légitime",
    "refusal": "le courtier refuse pour une autre raison",
    "residence_question": "le courtier demande seulement le pays de résidence",
    "ack": "simple accusé de réception, sans décision",
    "reply": "autre cas, ou incertain",
}

SYSTEM = (
    "Tu analyses la réponse d'une entreprise à une demande d'effacement de données personnelles (RGPD). "
    "Réponds UNIQUEMENT avec un objet JSON : "
    '{"category": "<catégorie>", "confidence": <nombre entre 0 et 1>, "summary": "<une phrase en français>", '
    '"ticket": "<numéro de dossier ou null>"}. Catégories possibles :\n'
    + "\n".join(f"- {k} : {v}" for k, v in CATEGORIES.items())
    + "\nIgnore le message cité (notre demande d'origine). En cas de doute, utilise « reply » avec une confiance basse."
)


def classify(subject: str, text: str, timeout: int = 300) -> dict | None:
    """Renvoie {"category", "confidence", "summary", "ticket"} ou None si le modèle est indisponible."""
    if not settings.llm_enabled:
        return None
    payload = {
        "model": settings.llm_model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Objet : {subject}\n\n{text[:6000]}"},
        ],
    }
    req = urllib.request.Request(
        settings.llm_url.rstrip("/") + "/api/chat", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = json.loads(resp.read())["message"]["content"]
        data = json.loads(content)
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("IA locale indisponible : %s", e)
        return None
    cat = data.get("category")
    if cat not in CATEGORIES:
        return None
    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    ticket = data.get("ticket")
    return {
        "category": cat,
        "confidence": max(0.0, min(1.0, conf)),
        "summary": str(data.get("summary") or "")[:400],
        "ticket": str(ticket)[:80] if ticket and str(ticket).lower() != "null" else None,
    }


def test() -> str:
    sample = "Hello, we have reviewed your request and removed your email address from our marketing database. Kind regards."
    res = classify("Re: your request", sample, timeout=300)
    if res is None:
        raise RuntimeError(f"pas de réponse exploitable de {settings.llm_url} (modèle « {settings.llm_model} » installé ?)")
    return f"Modèle opérationnel : « {res['category']} » (confiance {res['confidence']:.0%}) — {res['summary']}"
