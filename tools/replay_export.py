#!/usr/bin/env python3
"""Rejoue les règles de classement actuelles sur un export « À traiter » (page À traiter › Exporter pour analyse).

    python tools/replay_export.py chemin/vers/oubliez-moi-analyse-AAAAMMJJ-HHMM.json

Affiche la répartition par catégorie, puis chaque réponse dont le classement actuel diffère de celui
enregistré : de quoi vérifier une nouvelle règle sur vos vraies réponses avant de l'installer.
Rien n'est modifié ni envoyé. Les exports contiennent le texte de vos échanges : ne les publiez pas.
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="oubli-replay-"))

from app import mailer, rules  # noqa: E402

SECTIONS = ("elements", "terminees", "en_attente_apres_reponse", "exemples_decides")
_BOUNCE_SENDER = re.compile(r"^(mailer-daemon|postmaster|non-?delivery|mail-?daemon)@", re.I)
_BOUNCE_SUBJECT = re.compile(r"^\s*(undeliverable|undelivered mail|delivery status notification|mail delivery (failed|failure)|"
                             r"returned mail|non remis|unzustellbar|nicht zustellbar|failure notice)", re.I)


def classify(reply: dict) -> rules.Classification:
    sender = re.search(r"[\w.+'-]+@[\w.-]+", reply.get("de") or "")
    addr = sender.group(0).lower() if sender else ""
    bounce = bool(_BOUNCE_SENDER.match(addr) or _BOUNCE_SUBJECT.match(reply.get("objet") or "")
                  or ("[EMAIL_PERSO]" in (reply.get("de") or "") and "Delivery" in (reply.get("objet") or "")))
    return rules.classify(mailer.ParsedMail(uid="", from_addr=addr, from_name="", subject=reply.get("objet") or "",
                                            message_id="", text=reply.get("texte") or "", is_bounce=bounce))


def replay(data: dict) -> tuple[collections.Counter, list[dict]]:
    """(répartition par catégorie, réponses dont le classement actuel diffère de celui enregistré)."""
    counts, changes = collections.Counter(), []
    for section in SECTIONS:
        for item in data.get(section) or []:
            if not item.get("reponses"):
                continue
            c = classify(item["reponses"][-1])
            counts[c.kind] += 1
            stored = (item.get("classement_enregistre") or {}).get("categorie")
            if stored != c.kind:
                changes.append({"section": section, "courtier": item["courtier"]["nom"], "statut": item["statut"],
                                "enregistre": stored, "actuel": c.kind, "declencheur": c.reason})
    return counts, changes


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    counts, changes = replay(json.loads(Path(argv[1]).read_text(encoding="utf-8")))
    print(f"{sum(counts.values())} réponses rejouées")
    for kind, n in counts.most_common():
        print(f"  {n:4}  {rules.KIND_LABELS.get(kind, kind)}")
    print(f"\n{len(changes)} classement(s) différent(s) de celui enregistré")
    for ch in changes:
        print(f"  {ch['courtier'][:32]:32} {ch['statut']:13} {ch['enregistre'] or '—':20} -> {ch['actuel']:20} « {ch['declencheur'][:50]} »")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
