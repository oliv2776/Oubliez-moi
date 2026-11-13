"""Qui a décidé quoi dans l'historique d'une demande : colonnes `actor` et `category` des événements.

Les textes de l'historique sont faits pour être lus ; le code, lui, s'appuie sur ces deux colonnes.
`infer` ne sert qu'à deux choses : remplir les colonnes des événements créés avant leur existence
(migration au démarrage), et servir de filet quand un événement est créé sans les préciser.
"""
from __future__ import annotations

# Auteurs d'un événement
RULES, AI, REANALYSIS, REANALYSIS_AI, MANUAL, SYSTEM = "rules", "ai", "reanalysis", "reanalysis-ai", "manual", "system"
AUTOMATIC = {RULES, AI, REANALYSIS, REANALYSIS_AI}   # classement automatique, à la réception ou à la réanalyse
AT_RECEPTION = {RULES, AI}
BY_AI = {AI, REANALYSIS_AI}
# Catégories des événements qui ne sont pas un classement de réponse
SUMMARY, ADVICE, UNVERIFIED = "summary", "advice", "unverified"
STATUS_PREFIX = "status:"    # anciens historiques : « Classement automatique : Terminée » (libellé de statut)

_MANUAL = "Statut changé manuellement : "
_AUTO = ("Classement automatique : ", "Réanalyse : ")


def classifier(by_ai: bool, reanalysis: bool = False) -> str:
    if reanalysis:
        return REANALYSIS_AI if by_ai else REANALYSIS
    return AI if by_ai else RULES


def infer(kind: str | None, summary: str | None) -> tuple[str | None, str | None]:
    """(auteur, catégorie) déduits du texte d'un événement, pour les historiques antérieurs aux colonnes."""
    from .db import STATUS_LABELS
    from .mailer import KIND_LABELS
    s = summary or ""
    if kind == "status":
        if s.startswith(_MANUAL):
            label = s[len(_MANUAL):].partition(" — ")[0].strip()
            return MANUAL, next((code for code, text in STATUS_LABELS.items() if text == label), None)
        for prefix in _AUTO:
            if s.startswith(prefix):
                rest = s[len(prefix):]
                actor = classifier("IA, probabilité" in s, reanalysis=prefix == _AUTO[1])
                for k, label in sorted(KIND_LABELS.items(), key=lambda kv: -len(kv[1])):
                    if rest.startswith(label):
                        return actor, k
                for code, label in STATUS_LABELS.items():
                    if rest.startswith(label):
                        return actor, STATUS_PREFIX + code
                return actor, None
        return SYSTEM, None
    if kind == "info":
        if s.startswith("Analyse IA : "):
            return AI, SUMMARY
        if s.startswith("Conseil IA : "):
            return AI, ADVICE
        if s.startswith("Accusé de réception détecté"):
            return RULES, "ack"
    if kind == "warning" and s.startswith("Réponse non vérifiée"):
        return SYSTEM, UNVERIFIED
    return None, None


def last_status(req):
    return next((e for e in reversed(req.events) if e.kind == "status"), None)
