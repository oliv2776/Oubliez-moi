"""« En bref » d'une demande : ce que dit la dernière réponse, qui l'a classée, et quoi faire.

Tout est déduit de l'historique (classement, note et conseil de l'IA, avertissements),
donc fonctionne aussi pour les réponses reçues avant cette fonctionnalité.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import mailer, mailview
from .db import Request

# Que faire, selon ce que dit la réponse (utilisé quand l'IA n'a pas donné de conseil).
ACTIONS = {
    "id_request": ("Le courtier demande une preuve d'identité. Votre adresse e-mail suffit souvent à vous identifier : "
                   "répondez-lui en ce sens, ou envoyez une copie de pièce d'identité en masquant tout ce qui n'est pas "
                   "nécessaire (photo, numéro de document). Puis cliquez « C'est fait, j'attends leur réponse ».",
                   "Ne transmettez jamais une pièce complète à une adresse que vous n'avez pas vérifiée."),
    "confirm_link": ("Ouvrez le lien de confirmation après avoir vérifié qu'il mène bien au site du courtier, "
                     "puis cliquez « C'est fait, j'attends leur réponse ».",
                     "Ignorez tout lien de connexion ou de désinscription qui ne correspond pas à votre demande."),
    "needs_action": ("Le courtier attend quelque chose de vous (formulaire, précision, confirmation) : faites-le si c'est "
                     "raisonnable, puis cliquez « C'est fait, j'attends leur réponse ».",
                     "N'envoyez pas plus de données personnelles que nécessaire."),
    "residence_question": ("Répondez simplement que vous résidez en France : votre adresse complète n'est pas nécessaire. "
                           "Puis cliquez « C'est fait, j'attends leur réponse ».", ""),
    "legit_refusal": ("Le courtier invoque son « intérêt légitime ». Pour la prospection, le droit d'opposition est sans "
                      "condition (RGPD art. 21.3) : rappelez-le-lui. S'il maintient son refus, cliquez « Refus » pour "
                      "préparer une plainte à la CNIL.", ""),
    "refusal": ("Le courtier semble refuser. Demandez-lui le motif précis ; s'il n'est pas justifié, cliquez « Refus » "
                "pour préparer une plainte à la CNIL.", ""),
    "completed_deleted": ("Si la réponse confirme bien la suppression de vos données, cliquez « Données supprimées ».", ""),
    "completed_no_data": ("Si la réponse confirme qu'aucune donnée ne vous concerne, cliquez « Données supprimées ».", ""),
    "ack": ("Simple accusé de réception : cliquez « Simple accusé, j'attends ». Le délai légal continue de courir.", ""),
    "extension": ("Le courtier prolonge le délai (deux mois de plus au maximum) : rien à faire, cliquez « Simple accusé, "
                  "j'attends ».", ""),
    "bounce": ("L'adresse e-mail du courtier ne fonctionne pas : cherchez un autre contact (formulaire, site) depuis la "
               "fiche du courtier.", ""),
    "reply": ("Lisez la réponse ci-dessous et indiquez ce qu'elle dit avec les boutons.", ""),
}
_LABEL_TO_KIND = {label: kind for kind, label in mailer.KIND_LABELS.items()}
_CLASSIFIED = re.compile(r"^(?:Classement automatique|Réanalyse) : (.+?)(?: \(« (.*) »\))?(?: — .*)?$")
_PROBA = re.compile(r"IA, probabilité (\d+) %")
_QUOTE_INTRO = re.compile(r"^\s*(on|le|am)\b.{0,120}\b(wrote|a écrit|schrieb)\s*:\s*$", re.I)


@dataclass
class Insight:
    kind: str = "reply"
    label: str = ""
    by_ai: bool = False
    confidence: int | None = None
    summary: str = ""
    ai_summary: bool = False
    action: str = ""
    ai_action: bool = False
    caution: str = ""
    ticket: str | None = None
    unverified: bool = False


def _first_sentences(text: str, limit: int = 260) -> str:
    lines = []
    for ln in mailview.strip_quoted(text).splitlines():
        if ln.startswith(">") or _QUOTE_INTRO.match(ln):
            break  # début du message cité
        ln = ln.strip()
        if ln and not re.match(r"^(hi|hello|hey|dear|bonjour|bonsoir|madame|monsieur|hallo)\b[^\n]{0,60}$", ln, re.I):
            lines.append(ln)
    out = " ".join(lines)
    return out[:limit].rstrip() + ("…" if len(out) > limit else "")


def for_request(r: Request) -> Insight | None:
    """Résumé de la dernière réponse d'une demande (None s'il n'y a pas de réponse)."""
    events = list(r.events)
    idx = max((i for i, e in enumerate(events) if e.kind == "reply"), default=None)
    if idx is None:
        return None
    reply, after = events[idx], events[idx + 1:]
    ins = Insight()
    for e in after:
        if e.kind == "status":
            hit = _CLASSIFIED.match(e.summary or "")
            if hit:
                ins.label = hit.group(1)
                ins.kind = _LABEL_TO_KIND.get(hit.group(1), ins.kind)
                proba = _PROBA.search(hit.group(2) or "")
                if proba:
                    ins.by_ai, ins.confidence = True, int(proba.group(1))
        elif e.kind == "info" and (e.summary or "").startswith("Analyse IA : "):
            text = e.summary[len("Analyse IA : "):]
            ins.by_ai = ins.by_ai or True
            # Modèle de décision : « catégorie (probabilité 88 %) », pas un résumé
            if not re.search(r"\(probabilité \d+ %\)", text):
                ins.summary, ins.ai_summary = text, True
            proba = re.search(r"probabilité (\d+) %", text)
            if proba and ins.confidence is None:
                ins.confidence = int(proba.group(1))
            ticket = re.search(r"\(dossier n° ([^)]+)\)", text)
            if ticket:
                ins.ticket = ticket.group(1)
        elif e.kind == "info" and (e.summary or "").startswith("Conseil IA : "):
            ins.action, ins.ai_action = e.summary[len("Conseil IA : "):], True
        elif e.kind == "info" and (e.summary or "").startswith("Accusé de réception détecté"):
            ins.kind = "ack"
        elif e.kind == "warning" and (e.summary or "").startswith("Réponse non vérifiée"):
            ins.unverified = True
    if not ins.label:
        ins.label = mailer.KIND_LABELS.get(ins.kind, ins.kind)
    text = mailview.split_headers(reply.body or "")[1]
    if not ins.summary:
        ins.summary = _first_sentences(text)
    ins.ticket = ins.ticket or mailer.extract_ticket(mailview.strip_quoted(text))
    fallback, caution = ACTIONS.get(ins.kind, ACTIONS["reply"])
    if not ins.action:
        ins.action = fallback
    ins.caution = caution
    if ins.unverified:
        ins.caution = ("L'expéditeur n'a pas pu être authentifié : vérifiez que ce message vient bien du courtier avant "
                       "de suivre ses instructions. " + ins.caution).strip()
    return ins
