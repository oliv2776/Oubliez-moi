"""Catalogue des courtiers en données.

La source principale est la base ouverte de Datenanfragen.de
(https://github.com/datenanfragen/data, licence CC0), qui recense les contacts
« protection des données » de milliers d'organismes. On n'en garde que les
organismes qui font commerce de données personnelles : courtiers d'adresses,
acteurs publicitaires / DMP, bureaux de crédit, bases de prospection B2B…
"""
from __future__ import annotations

import io
import json
import logging
import re
import urllib.request

from .net import safe_urlopen
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

DATENANFRAGEN_ZIP_URL = "https://github.com/datenanfragen/data/archive/refs/heads/master.zip"

# Catégories Datenanfragen qui correspondent à des courtiers en données.
BROKER_CATEGORIES = {"addresses", "ads"}

# Organismes à inclure même si leur catégorie ne correspond pas
# (bureaux de crédit, bases de prospection, courtiers connus).
EXTRA_SLUGS = re.compile(
    r"^(experian|equifax|transunion|schufa|crif|infoscore|arvato-infoscore|bisnode|"
    r"liveramp|acxiom|oracle|nielsen|kantar|epsilon|zoominfo|lusha|kaspr|apollo-io|"
    r"rocketreach|contactout|clearview-ai|pipl|spokeo|mylife|data-axle|dealfront)"
)

# Grandes plateformes classées « ads » mais qui ne sont pas des courtiers :
# une demande d'effacement chez elles supprimerait le compte de l'utilisateur.
EXCLUDED_SLUGS = re.compile(
    r"^(google|facebook|meta|instagram|whatsapp|microsoft|linkedin|amazon|apple|twitter|x-corp|"
    r"tiktok|snapchat|pinterest|reddit|yahoo|verizon-media|ebay|paypal|spotify|netflix|"
    r"deezer|twitch|discord|telegram|zoom|yandex|duckduckgo|epicgames|oculuscom|avast|dailymotion|"
    r"justwatch|roku|trustpilot|gameforge|miles-and-more|gartentraum|hd-toner|condenast|"
    r"burda-forward|ziff-davis|iab|digitrust|arkoselabs|helpshift|mailchimp|sendinblue|cleverreach|"
    r"onesignal|optimizely|mixpanel|amplitude-com|leanplum|unity3d|vivalu)(-|$)"
)


# Organismes du catalogue chez qui une personne peut AUSSI avoir un compte qu'elle utilise.
# Une demande d'effacement y supprimerait ce compte : on le signale avant l'envoi.
ACCOUNT_RISKS = [
    (re.compile(r"^(gfk|nielsen|kantar)(-|$)"),
     "Institut d'études : si vous êtes panéliste, votre inscription au panel sera supprimée."),
    (re.compile(r"^(zoominfo|lusha|kaspr|apollo-io|rocketreach|contactout|dealfront|sales-rocks|growbots|amazinghiring|similarweb)(-|$)"),
     "Outil professionnel : si vous l'utilisez pour votre travail, votre compte utilisateur pourrait être supprimé."),
    (re.compile(r"^(awin|tradedoubler|rakutenmarketing|skimlinks|sovendus|aklamio|kupona|planet49)(-|$)"),
     "Affiliation, cashback ou jeux-concours : un compte de gains, de parrainage ou d'éditeur pourrait être supprimé."),
    (re.compile(r"^(bazaarvoice)(-|$)"),
     "Plateforme d'avis clients : les avis que vous avez publiés pourraient être supprimés."),
    (re.compile(r"^(we-conect|meinestadt-de)(-|$)"),
     "Service avec comptes utilisateurs : si vous y êtes inscrit, votre compte pourrait être supprimé."),
]


def account_risk(slug: str) -> str | None:
    for rx, message in ACCOUNT_RISKS:
        if rx.match(slug or ""):
            return message
    return None


def normalize_record(rec: dict) -> dict | None:
    """Convertit un enregistrement Datenanfragen en courtier, ou None s'il n'en est pas un."""
    slug = (rec.get("slug") or "").strip()
    name = (rec.get("name") or "").strip()
    if not slug or not name or EXCLUDED_SLUGS.match(slug):
        return None
    cats = set(rec.get("categories") or [])
    if not (cats & BROKER_CATEGORIES or EXTRA_SLUGS.match(slug)):
        return None
    email = (rec.get("email") or "").strip() or None
    webform = (rec.get("webform") or "").strip() or None
    if not email and not webform:
        return None
    return {
        "slug": slug,
        "name": name,
        "email": email,
        "webform": webform,
        "web": (rec.get("web") or "").strip() or None,
        "postal_address": rec.get("address") or None,
        "countries": rec.get("relevant-countries") or ["all"],
        "categories": sorted(cats),
        "needs_id": bool(rec.get("needs-id-document")),
        "quality": rec.get("quality") or None,
        "source": "datenanfragen",
    }


def _collect(records) -> list[dict]:
    out = {}
    for rec in records:
        b = normalize_record(rec)
        if b:
            out[b["slug"]] = b
    return sorted(out.values(), key=lambda b: b["name"].lower())


def parse_directory(path: str | Path) -> list[dict]:
    """Lit un dossier `companies/` cloné localement."""
    recs = []
    for f in sorted(Path(path).glob("*.json")):
        try:
            recs.append(json.loads(f.read_text(encoding="utf-8")))
        except (ValueError, OSError) as e:
            log.warning("Fichier ignoré %s : %s", f, e)
    return _collect(recs)


def parse_zip(data: bytes) -> list[dict]:
    """Lit l'archive zip du dépôt Datenanfragen sans rien extraire sur disque."""
    pattern = re.compile(r"^[^/]+/companies/[^/]+\.json$")
    recs = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if pattern.match(name):
                try:
                    recs.append(json.loads(zf.read(name).decode("utf-8")))
                except ValueError as e:
                    log.warning("Entrée ignorée %s : %s", name, e)
    return _collect(recs)


def fetch_remote(url: str = DATENANFRAGEN_ZIP_URL, timeout: int = 120) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "oubliez-moi-selfhosted"})
    with safe_urlopen(req, timeout=timeout, allow_private=False) as resp:
        data = resp.read()
    return parse_zip(data)


def load_seed(path: str | Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def is_default_enabled(broker: dict, countries: set[str]) -> bool:
    """Un courtier est activé par défaut s'il concerne un des pays configurés."""
    return bool(set(broker.get("countries") or ["all"]) & countries)


if __name__ == "__main__":  # Génère la liste embarquée : python -m app.brokers <dossier companies> <sortie.json>
    import sys

    brokers = parse_directory(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(brokers, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(brokers)} courtiers écrits dans {sys.argv[2]}")
