"""Classement des réponses ambiguës par un modèle local (Ollama).

Utilisé seulement quand les règles par mots-clés ne savent pas trancher.
Les réponses des courtiers contiennent vos données : le modèle doit tourner
chez vous (Ollama), rien n'est envoyé à un service externe.

Deux types de modèles sont pris en charge :

- « decision » (par défaut) : un modèle de décision (Tev1, Nimble…) interrogé
  via l'API System One d'Ollama (POST /v1/systemone). Il choisit une catégorie
  dans une liste et donne une probabilité pour chacune, en une seule passe :
  plus rapide et plus fiable pour ce travail, mais il n'écrit pas de résumé ;
- « chat » : un modèle conversationnel (qwen2.5, llama3.1…) via /api/chat,
  qui renvoie un JSON avec catégorie, confiance, résumé et numéro de dossier.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import settings
from .net import safe_urlopen

log = logging.getLogger(__name__)

# Description des catégories pour un modèle conversationnel (consignes en français).
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

# Même liste pour un modèle de décision. Ces modèles sont entraînés surtout en anglais :
# les descriptions sont en anglais, le message analysé peut être dans n'importe quelle langue.
DECISION_CRITERIA = {
    "completed_deleted": "The company confirms it has deleted or erased the person's data, anonymised it, "
                         "or added the person to a suppression / opt-out / do-not-contact list.",
    "completed_no_data": "The company states it holds no personal data about the person, or could not find any record.",
    "id_request": "The company requires a copy of an ID card, passport or other identity document before acting.",
    "confirm_link": "The company asks the person to click a link to confirm or verify the request or the email address.",
    "needs_action": "The company asks for another action: fill in a web form, use a privacy portal, "
                    "or provide additional information (other than an ID document or the country of residence).",
    "extension": "The company announces it needs more time and extends the response deadline.",
    "legit_refusal": "The company refuses the request, invoking its legitimate interests.",
    "refusal": "The company refuses or declines the request for another reason.",
    "residence_question": "The company only asks in which country the person lives or resides.",
    "ack": "A simple acknowledgement of receipt or automatic reply, with no decision yet.",
    "reply": "None of the above, or the message is unclear.",
}
DECISION_INSTRUCTIONS = (
    "This is a company's reply to a GDPR request asking it to erase a person's personal data "
    "and to stop using it for direct marketing. What does the reply say? Ignore any quoted copy of the original request."
)

# Modèles proposés dans l'interface. « memory » : mémoire vive approximative en usage.
MODELS = [
    {"name": "tev1:4b", "label": "Tev1 4B — Together AI", "kind": "decision", "memory": "~3 Go",
     "note": "recommandé : précis (73 % sur les tests publics) et léger"},
    {"name": "nimble", "label": "Nimble 9B — Bespoke Labs", "kind": "decision", "memory": "~7 Go",
     "note": "le plus précis (76 % sur les tests publics), plus lent sur processeur"},
    {"name": "tev1:0.8b", "label": "Tev1 0.8B — Together AI", "kind": "decision", "memory": "~1 Go",
     "note": "très léger, nettement moins fiable (64 %)"},
    {"name": "nimble:9b-bf16", "label": "Nimble 9B pleine précision", "kind": "decision", "memory": "~18 Go",
     "note": "très lourd, sans intérêt sans carte graphique"},
    {"name": "qwen2.5:3b", "label": "Qwen2.5 3B", "kind": "chat", "memory": "~2,5 Go",
     "note": "conversationnel : classe et résume en une phrase"},
    {"name": "qwen2.5:7b", "label": "Qwen2.5 7B", "kind": "chat", "memory": "~5 Go",
     "note": "conversationnel, plus fiable que le 3B"},
    {"name": "qwen3:4b", "label": "Qwen3 4B", "kind": "chat", "memory": "~3 Go", "note": "conversationnel, plus récent"},
    {"name": "qwen3:8b", "label": "Qwen3 8B", "kind": "chat", "memory": "~6 Go", "note": "conversationnel, plus récent"},
]
MODELS_BY_NAME = {m["name"]: m for m in MODELS}


def normalize(name: str) -> str:
    """« nimble » et « nimble:latest » désignent le même modèle."""
    name = (name or "").strip()
    return name if ":" in name else f"{name}:latest"


def catalog_kind(name: str) -> str | None:
    for m in MODELS:
        if normalize(m["name"]) == normalize(name):
            return m["kind"]
    return None


SYSTEM = (
    "Tu aides une personne qui a envoyé à une entreprise (courtier en données, régie publicitaire…) une demande RGPD "
    "d'effacement de ses données (article 17) et d'opposition à la prospection (article 21). Tu lis la réponse de "
    "l'entreprise et tu dis à la personne, en français, ce qu'elle dit et ce qu'il faut faire.\n"
    "Réponds UNIQUEMENT avec un objet JSON :\n"
    '{"category": "<catégorie>", "confidence": <nombre entre 0 et 1>, '
    '"summary": "<une ou deux phrases : ce qui est déjà fait, ce qui reste en attente, et pourquoi>", '
    '"action": "<ce que la personne doit faire maintenant : une à trois phrases concrètes, à l\'impératif>", '
    '"ticket": "<numéro de dossier attribué par l\'entreprise, ou null>"}\n'
    "Pour « action » : sois précis et appuie-toi sur le message. Cite l'expéditeur ou l'adresse d'où viendra un "
    "autre e-mail (par exemple « cherchez dans votre boîte l'e-mail de privacy-requests@exemple.com »), le formulaire "
    "ou la page à utiliser, le délai annoncé. Si une partie de la demande est déjà traitée, dis-le. Si l'entreprise "
    "renvoie vers d'autres organismes, dis lesquels contacter. N'invente aucun lien ni aucune adresse. "
    "Si rien n'est à faire, mets null.\n"
    "« ticket » : uniquement un numéro attribué par l'entreprise ; jamais la référence de la demande qui commence par "
    "OUBLI- ni aucune autre référence que la personne a elle-même envoyée.\n"
    "Catégories possibles :\n"
    + "\n".join(f"- {k} : {v}" for k, v in CATEGORIES.items())
    + "\nIgnore le message cité (notre demande d'origine) et la signature. En cas de doute, utilise « reply » avec "
    "une confiance basse."
)


# Requête en cours vers le modèle, affichée dans l'interface (« le modèle réfléchit depuis 23 s »).
activity: dict = {}


def _post(path: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        settings.llm_url.rstrip("/") + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    activity.update(model=payload.get("model", settings.llm_model), since=time.time())
    try:
        with safe_urlopen(req, timeout=timeout, allow_private=True) as resp:
            return json.loads(resp.read())
    finally:
        activity.clear()


def loaded_models() -> list[dict] | None:
    """Modèles chargés en mémoire par Ollama (/api/ps) ; None si Ollama ne répond pas."""
    if not settings.llm_url:
        return None
    try:
        with safe_urlopen(settings.llm_url.rstrip("/") + "/api/ps", timeout=3, allow_private=True) as resp:
            models = json.loads(resp.read()).get("models", [])
    except (OSError, ValueError):
        return None
    return [{"name": m.get("name", ""), "size_gb": round(int(m.get("size") or 0) / 1e9, 1)} for m in models]


def _classify_decision(subject: str, text: str, timeout: int) -> dict | None:
    data = _post("/v1/systemone", {
        "model": settings.llm_model,
        "state": {"subject": subject, "message": clean_for_model(text)[:6000]},
        "questions": {"category": {"type": "choice", "instructions": DECISION_INSTRUCTIONS, "criteria": DECISION_CRITERIA}},
        "keep_alive": "5m",
    }, timeout)
    ans = data["answers"]["category"]
    cat = ans.get("choice")
    if cat not in CATEGORIES:
        return None
    probs = ans.get("probabilities") or {}
    # Probabilité de l'option retenue : c'est elle qu'on compare au seuil d'action.
    conf = probs.get(cat, ans.get("confidence", 0))
    return {"category": cat, "confidence": float(conf or 0), "summary": "", "ticket": None, "mode": "decision"}


_LINK = re.compile(r"<?https?://[^\s>]+>?;?")


def clean_for_model(text: str) -> str:
    """Retire le bruit qui égare le modèle : liens de pistage interminables, lignes vides en série."""
    def short(m):
        domain = re.sub(r"^<?https?://", "", m.group(0)).split("/", 1)[0].rstrip(">;")
        return f"[lien {domain}]"
    text = _LINK.sub(short, text or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _their_ticket(value) -> str | None:
    """Numéro de dossier de l'entreprise ; jamais notre propre référence (OUBLI-…)."""
    value = str(value or "").strip()
    if not value or value.lower() in ("null", "none") or value.upper().startswith(settings.reference_prefix.upper()):
        return None
    return value[:80]


def _classify_chat(subject: str, text: str, timeout: int) -> dict | None:
    data = _post("/api/chat", {
        "model": settings.llm_model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "keep_alive": "5m",  # déchargé après 5 min sans analyse (prime sur un OLLAMA_KEEP_ALIVE permanent)
        **({"think": False} if settings.llm_model.startswith("qwen3") else {}),
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Objet : {subject}\n\n{clean_for_model(text)[:6000]}"},
        ],
    }, timeout)
    content = json.loads(data["message"]["content"])
    cat = content.get("category")
    if cat not in CATEGORIES:
        return None
    try:
        conf = float(content.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    ticket = content.get("ticket")
    action = content.get("action")
    return {
        "category": cat,
        "confidence": conf,
        "summary": str(content.get("summary") or "")[:400],
        "action": str(action)[:400] if action and str(action).lower() not in ("null", "none", "") else None,
        "ticket": _their_ticket(ticket),
        "mode": "chat",
    }


def classify(subject: str, text: str, timeout: int = 300) -> dict | None:
    """Renvoie {"category", "confidence", "summary", "ticket", "mode"} ou None si le modèle est indisponible."""
    if not settings.llm_enabled:
        return None
    try:
        if settings.llm_mode == "chat":
            res = _classify_chat(subject, text, timeout)
        else:
            res = _classify_decision(subject, text, timeout)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("IA locale indisponible : %s", e)
        return None
    if res:
        res["confidence"] = max(0.0, min(1.0, res["confidence"]))
    return res


def unreachable_hint(url: str) -> str:
    """Explication concrète quand Ollama ne répond pas (cas fréquent : installé sur l'hôte)."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return ("dans le conteneur, « localhost » désigne Oubliez-moi lui-même : pour un Ollama installé sur le serveur, "
                "utilisez http://host.docker.internal:11434")
    return ("s'il est installé directement sur le serveur, il n'écoute que sur 127.0.0.1 : ajoutez "
            "OLLAMA_HOST=0.0.0.0:11434 au service (sudo systemctl edit ollama) puis redémarrez-le — voir le README")


def test() -> str:
    try:
        with safe_urlopen(settings.llm_url.rstrip("/") + "/api/tags", timeout=5, allow_private=True) as resp:
            resp.read()
    except (OSError, ValueError) as e:
        raise RuntimeError(f"Ollama injoignable à {settings.llm_url} ({getattr(e, 'reason', e)}) : {unreachable_hint(settings.llm_url)}") from e
    sample = "Hello, we have reviewed your request and removed your email address from our marketing database. Kind regards."
    res = classify("Re: your request", sample, timeout=300)
    if res is None:
        hint = "Ollama 0.35 ou plus récent et un modèle de décision (tev1:4b, nimble) installés ?" if settings.llm_mode != "chat" \
            else "modèle installé avec ollama pull ?"
        raise RuntimeError(f"pas de réponse exploitable de {settings.llm_url} pour « {settings.llm_model} » ({hint})")
    from .mailer import KIND_LABELS
    detail = f" — {res['summary']}" if res["summary"] else ""
    return (f"Modèle opérationnel : « {KIND_LABELS.get(res['category'], res['category'])} » "
            f"(probabilité {round(res['confidence'] * 100)} %){detail}")


# ------------------------------------------------------ Gestion des modèles ----
import re as _re
import threading as _threading
import time as _time

_MODEL_NAME_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*(:[a-zA-Z0-9._-]+)?$")
_installed_cache: dict = {"at": 0.0, "url": "", "names": None, "sizes": {}}
PULLS: dict[str, dict] = {}  # téléchargements en cours ou terminés, par nom normalisé


def installed_models(max_age: float = 20.0) -> list[str] | None:
    """Modèles présents dans Ollama (None si Ollama est injoignable ou non configuré)."""
    if not settings.llm_url:
        return None
    now = _time.monotonic()
    if _installed_cache["names"] is not None and _installed_cache["url"] == settings.llm_url and now - _installed_cache["at"] < max_age:
        return _installed_cache["names"]
    sizes = {}
    try:
        with safe_urlopen(settings.llm_url.rstrip("/") + "/api/tags", timeout=3, allow_private=True) as resp:
            models = json.loads(resp.read()).get("models", [])
        names = sorted(m["name"] for m in models)
        sizes = {m["name"]: int(m.get("size") or 0) for m in models}
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.info("Liste des modèles Ollama indisponible : %s", e)
        names = None
    _installed_cache.update(at=now, url=settings.llm_url, names=names, sizes=sizes)
    return names


def installed_details() -> list[dict] | None:
    """Modèles installés avec leur taille sur le disque, du plus gros au plus petit."""
    names = installed_models()
    if names is None:
        return None
    sizes = _installed_cache["sizes"]
    current = normalize(settings.llm_model)
    out = [{"name": n, "size": sizes.get(n, 0), "gb": f"{sizes.get(n, 0) / 1e9:.1f}".replace(".", ","),
            "current": normalize(n) == current} for n in names]
    return sorted(out, key=lambda m: -m["size"])


def delete_model(name: str) -> None:
    """Supprime un modèle d'Ollama pour libérer de la place sur le disque."""
    if not settings.llm_url:
        raise ValueError("Indiquez d'abord l'adresse d'Ollama")
    if not _MODEL_NAME_RE.match(name or ""):
        raise ValueError("Nom de modèle invalide")
    pull = PULLS.get(normalize(name))
    if pull and not pull["done"]:
        raise ValueError("Ce modèle est en cours de téléchargement")
    # « model » pour les versions récentes d'Ollama, « name » pour les anciennes
    req = urllib.request.Request(settings.llm_url.rstrip("/") + "/api/delete", method="DELETE",
                                 data=json.dumps({"model": name, "name": name}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with safe_urlopen(req, timeout=60, allow_private=True) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise ValueError("Ollama ne connaît pas ce modèle" if e.code == 404 else f"Ollama a répondu {e.code}") from e
    except OSError as e:
        raise ValueError(f"Ollama injoignable : {getattr(e, 'reason', e)}") from e
    finally:
        _installed_cache["names"] = None
    PULLS.pop(normalize(name), None)


def is_installed(name: str) -> bool:
    names = installed_models() or []
    return normalize(name) in {normalize(n) for n in names}


def _pull_worker(name: str) -> None:
    key = normalize(name)
    state = PULLS[key]
    req = urllib.request.Request(settings.llm_url.rstrip("/") + "/api/pull",
                                 data=json.dumps({"model": name, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with safe_urlopen(req, timeout=60 * 60, allow_private=True) as resp:
            for raw in resp:
                line = json.loads(raw.decode() or "{}")
                if line.get("error"):
                    raise RuntimeError(line["error"])
                state["status"] = line.get("status", state["status"])
                total, done = line.get("total"), line.get("completed")
                if total and done is not None and total > 10_000_000:  # la grosse couche : les poids du modèle
                    state["percent"] = int(100 * done / total)
                if line.get("status") == "success":
                    state.update(done=True, percent=100, status="installé")
        if not state["done"]:
            raise RuntimeError("téléchargement interrompu")
    except Exception as e:  # noqa: BLE001
        state.update(done=True, error=str(getattr(e, "reason", e)))
        log.warning("Téléchargement du modèle %s impossible : %s", name, e)
    finally:
        _installed_cache["names"] = None  # forcer la relecture de la liste


def start_pull(name: str) -> dict:
    if not settings.llm_url:
        raise ValueError("Indiquez d'abord l'adresse d'Ollama")
    if not _MODEL_NAME_RE.match(name or ""):
        raise ValueError("Nom de modèle invalide")
    key = normalize(name)
    current = PULLS.get(key)
    if current and not current["done"]:
        return current
    PULLS[key] = {"model": name, "status": "démarrage", "percent": None, "done": False, "error": None}
    _threading.Thread(target=_pull_worker, args=(name,), daemon=True).start()
    return PULLS[key]


def pull_status(name: str) -> dict:
    return PULLS.get(normalize(name)) or {}
