"""Ce que l'utilisateur doit faire pour une demande en « Action requise », en catégories précises.

Le classement est refait avec les règles actuelles sur la dernière réponse : une demande classée
« action demandée » par une ancienne version apparaît ainsi dans la bonne catégorie.
"""
from __future__ import annotations

from . import mailer
from .db import NEEDS_ACTION, Request

# (clé, libellé de la catégorie, libellé court pour la liste, catégories de réponse)
ACTION_TYPES = [
    ("form", "Formulaire à remplir", "Formulaire", {"webform"}),
    ("link", "Lien à cliquer", "Lien", {"confirm_link"}),
    ("answer", "Réponse prête à envoyer", "Réponse prête", {"clarification", "residence_question", "legit_refusal"}),
    ("identity", "Pièce d'identité demandée", "Pièce d'identité", {"id_request"}),
    ("other_address", "Confirmer depuis une autre adresse", "Autre adresse", {"confirm_from_address"}),
    ("device", "Identifiant publicitaire", "Identifiant pub.", {"device_id"}),
    ("other", "Autre demande", "À lire", set()),
]
LABELS = {key: label for key, label, _, _ in ACTION_TYPES}
SHORT = {key: short for key, _, short, _ in ACTION_TYPES}
_BY_KIND = {kind: key for key, _, _, kinds in ACTION_TYPES for kind in kinds}


# (id, empreinte du texte) -> classement. Les règles ne changent pas tant que l'appli tourne ; l'empreinte
# couvre le cas d'un identifiant réutilisé après une suppression.
_kind_cache: dict[tuple, str] = {}
_CACHE_MAX = 20000


def kind_of_reply(ev) -> str:
    """Classement d'une réponse enregistrée par les règles actuelles, calculé une seule fois par réponse."""
    from .reanalysis import _from_event
    key = (ev.id, hash(ev.body))
    if key in _kind_cache:
        return _kind_cache[key]
    kind = mailer.classify(_from_event(ev)).kind
    if len(_kind_cache) >= _CACHE_MAX:
        _kind_cache.clear()
    _kind_cache[key] = kind
    return kind


def current_kind(req: Request) -> str | None:
    """Classement de la dernière réponse par les règles actuelles (None s'il n'y a pas de réponse)."""
    replies = [e for e in req.events if e.kind == "reply"]
    return kind_of_reply(replies[-1]) if replies else None


def action_type(req: Request, kind: str | None) -> str | None:
    """Catégorie d'action d'une demande en « Action requise » (None pour les autres statuts)."""
    if req.status != NEEDS_ACTION:
        return None
    if req.channel == "webform" and not req.sent_at:
        return "form"  # courtier sans adresse e-mail : seul son formulaire en ligne existe
    return _BY_KIND.get(kind or "", "other")
