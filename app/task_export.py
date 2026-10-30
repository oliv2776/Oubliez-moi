"""Export des réponses « À traiter » pour les analyser et trouver de nouvelles règles de classement.

Le fichier contient, pour chaque demande, la réponse reçue (sans le message cité), ce qu'en ont dit
les règles et l'IA, et l'historique. Avec l'option « exemples », il ajoute les réponses que
l'utilisateur a déjà tranchées à la main : sa décision sert alors d'étiquette.

Par défaut, les données personnelles (nom, adresses e-mail, téléphones, adresse postale, date de
naissance, alias) sont remplacées par des marqueurs et les jetons des liens sont masqués.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import __version__, insight, mailer, mailview
from .config import settings
from .db import COMPLETED, SENT, STATUS_LABELS, TODO_STATUSES, AccountMigration, Mailbox, Profile, Request

MAX_TEXT = 6000
MAX_EXAMPLES = 300
MANUAL = "Statut changé manuellement : "
_LABEL_TO_STATUS = {label: code for code, label in STATUS_LABELS.items()}
_EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", re.U)
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_TOKENISH = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_\-=%.]{16,}$|^[A-Fa-f0-9\-]{20,}$")
_SEP = r"[\s.\-/()]*"

README = (
    "Export « À traiter » d'Oubliez-moi, pour chercher de nouvelles règles de classement. "
    "« elements » : réponses qui attendent une décision. « terminees » : demandes closes (données "
    "supprimées ou inexistantes), pour repérer les faux positifs : « dernier_changement_de_statut.par » dit "
    "si la clôture vient des règles, de l'IA ou d'une décision manuelle. « en_attente_apres_reponse » : "
    "réponses traitées sans vous (accusé, prolongation, lien confirmé, réponse automatique). "
    "« exemples_decides » : autres réponses tranchées à la main (refus, abandon…). « decision_manuelle » "
    "donne le statut choisi à la main après la dernière réponse. « regles_actuelles » : ce que donnent les "
    "mots-clés aujourd'hui sur le texte (« declencheur » = passage reconnu). « classement_enregistre » : "
    "ce qui a été décidé à la réception (règles ou IA). Les textes ne contiennent que la partie nouvelle "
    "de la réponse, sans notre demande citée. Catégories possibles : voir « categories »."
)


class Redactor:
    """Remplace les données personnelles connues par des marqueurs lisibles."""

    def __init__(self, db: Session):
        self.rules: list[tuple[re.Pattern, str]] = []
        self.own_emails: set[str] = set()
        profiles = db.scalars(select(Profile)).all()
        emails = {settings.smtp_from, settings.imap_user}
        emails |= {e for p in profiles for e in (p.emails or [])}
        emails |= {m.address for m in db.scalars(select(Mailbox))}
        emails |= {m.alias for m in db.scalars(select(AccountMigration).where(AccountMigration.alias.is_not(None)))}
        emails |= {m.old_address for m in db.scalars(select(AccountMigration))}
        self.own_emails = {e.strip().lower() for e in emails if e and "@" in e}

        names: set[str] = set()
        for p in profiles:
            for full in [p.full_name, *(p.other_names or [])]:
                if not full:
                    continue
                names.add(full.strip())
                names |= {part for part in re.split(r"[\s\-]+", full) if len(part) >= 3}
        for name in sorted(names, key=len, reverse=True):
            self._add(r"(?<!\w)" + _accent_insensitive(name) + r"(?!\w)", "[MON_NOM]")
        # Partie avant « @ » de mes adresses, citée seule (rapports de non-distribution, en-têtes)
        for local in sorted({e.split("@")[0] for e in self.own_emails if len(e.split("@")[0]) >= 6}, key=len, reverse=True):
            self._add(r"(?<![\w.+-])" + re.escape(local) + r"(?![\w-])", "[MON_EMAIL]")

        for p in profiles:
            for raw in p.phones or []:
                digits = re.sub(r"\D", "", raw or "")
                if len(digits) >= 8:
                    core = digits[-9:]  # suffit à reconnaître +33 6…, 06…, 0033 6…
                    prefix = r"(?:(?:\+|00)\d{1,3}" + _SEP + r"(?:\(0\)" + _SEP + r")?|0" + _SEP + r")?"
                    self._add(r"(?<!\d)" + prefix + _SEP.join(core) + r"(?!\d)", "[MON_TÉLÉPHONE]")
            for addr in p.addresses or []:
                parts = [addr.strip()] + [x.strip() for x in re.split(r"[,\n]", addr or "")]
                for part in sorted({x for x in parts if len(x) >= 5}, key=len, reverse=True):
                    self._add(re.escape(part).replace(r"\ ", r"\s+"), "[MON_ADRESSE]")
            if p.birth_date:
                for variant in _date_variants(p.birth_date):
                    self._add(re.escape(variant), "[MA_DATE_DE_NAISSANCE]")

    def _add(self, pattern: str, mark: str) -> None:
        self.rules.append((re.compile(pattern, re.I), mark))

    def email(self, address: str, keep_domain: str = "") -> str:
        a = (address or "").strip().lower()
        if not a:
            return a
        if a in self.own_emails:
            return "[MON_EMAIL]"
        domain = a.rsplit("@", 1)[-1]
        if domain in mailer.FREEMAIL_DOMAINS or domain.endswith((".passmail.net", ".passmail.com", ".simplelogin.com")) \
                or domain in ("passmail.net", "passmail.com", "simplelogin.com", "aleeas.com", "slmail.me"):
            return "[EMAIL_PERSO]"
        return address

    def url(self, url: str) -> str:
        parts = urlsplit(url)
        path = "/".join("[jeton]" if _TOKENISH.match(seg) else seg for seg in parts.path.split("/"))
        return f"{parts.scheme}://{parts.netloc}{path}" + ("?[…]" if parts.query else "") + ("#[…]" if parts.fragment else "")

    def text(self, value: str | None) -> str:
        if not value:
            return value or ""
        out = _URL_RE.sub(lambda m: self.url(m.group(0)), value)
        out = _EMAIL_RE.sub(lambda m: self.email(m.group(0)), out)
        for rx, mark in self.rules:
            out = rx.sub(mark, out)
        return out


class _Passthrough:
    def text(self, value):
        return value or ""

    def email(self, address, keep_domain=""):
        return address


_ACCENTS = {"a": "aàâäáã", "e": "eéèêë", "i": "iîïíì", "o": "oôöóòõ", "u": "uùûüú", "c": "cç", "n": "nñ", "y": "yÿ"}


def _accent_insensitive(text: str) -> str:
    """Motif qui reconnaît le texte avec ou sans accents (« Deschênes » / « Deschenes »)."""
    out = []
    for ch in text:
        base = next((b for b, variants in _ACCENTS.items() if ch.lower() in variants), None)
        out.append(f"[{_ACCENTS[base]}{_ACCENTS[base].upper()}]" if base else (r"\s+" if ch.isspace() else re.escape(ch)))
    return "".join(out)


def _date_variants(value: str) -> list[str]:
    value = value.strip()
    out = [value]
    hit = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", value)
    if hit:
        y, m, d = hit.groups()
        out += [f"{d}/{m}/{y}", f"{d}.{m}.{y}", f"{d}-{m}-{y}", f"{m}/{d}/{y}"]
    return out


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="minutes") if dt else None


def _reply(e, red) -> dict:
    headers, text = mailview.split_headers(e.body or "")
    current = mailer.latest_reply_text(text) or mailview.strip_quoted(text)
    return {
        "recue_le": _iso(e.at),
        "de": red.text(headers.get("De", "")),
        "objet": red.text(headers.get("Objet", "")),
        "texte": red.text(current[:MAX_TEXT]) + ("\n[… tronqué]" if len(current) > MAX_TEXT else ""),
        "citation_retiree": len(current) < len(text.strip()),
        "html_disponible": bool(e.html),
    }


def _rules_now(e) -> dict:
    """Ce que les mots-clés actuels disent de cette réponse (sans IA, sans effet)."""
    headers, text = mailview.split_headers(e.body or "")
    sender = re.search(r"[\w.+'-]+@[\w.-]+", headers.get("De", ""))
    m = mailer.ParsedMail(uid="", from_addr=(sender.group(0).lower() if sender else ""), from_name="",
                          subject=headers.get("Objet", ""), message_id="", text=text)
    m.is_bounce = m.from_addr.startswith(("mailer-daemon@", "postmaster@"))
    c = mailer.classify(m)
    return {"categorie": c.kind, "libelle": mailer.KIND_LABELS.get(c.kind, c.kind), "declencheur": c.reason or None}


def _status_change(r: Request, red) -> dict | None:
    """Dernier changement de statut et qui l'a décidé (règles, IA, réanalyse, à la main)."""
    e = next((e for e in reversed(r.events) if e.kind == "status"), None)
    if e is None:
        return None
    s = e.summary or ""
    if s.startswith(MANUAL):
        by = "manuel"
    elif "IA, probabilité" in s:
        by = "ia"
    elif s.startswith("Réanalyse"):
        by = "reanalyse"
    elif s.startswith("Classement automatique"):
        by = "regles"
    else:
        by = "autre"
    return {"date": _iso(e.at), "par": by, "resume": red.text(s)}


def _item(r: Request, red) -> dict:
    replies = [e for e in r.events if e.kind == "reply"]
    ins = insight.for_request(r)
    broker = r.broker
    item = {
        "id": r.id,
        "reference": r.reference,
        "statut": r.status,
        "statut_libelle": r.status_label,
        "courtier": {
            "nom": broker.name, "slug": broker.slug, "email": broker.email, "site": broker.web,
            "formulaire": broker.webform, "pays": broker.countries, "categories": broker.categories,
            "identite_requise": broker.needs_id,
        },
        "langue": r.language,
        "canal": r.channel,
        "cycle": r.cycle,
        "envoyee_le": _iso(r.sent_at),
        "relances": r.reminder_count,
        "derniere_reponse_le": _iso(r.last_reply_at),
        "cloturee_le": _iso(r.closed_at),
        "erreur": red.text(r.last_error) or None,
        "dernier_changement_de_statut": _status_change(r, red),
    }
    if ins:
        item["classement_enregistre"] = {
            "categorie": ins.kind, "libelle": ins.label, "par_ia": ins.by_ai, "confiance_ia": ins.confidence,
            "resume_ia": red.text(ins.summary) if ins.ai_summary else None,
            "conseil_ia": red.text(ins.action) if ins.ai_action else None,
            "numero_de_dossier": ins.ticket, "expediteur_non_verifie": ins.unverified,
        }
        item["regles_actuelles"] = _rules_now(replies[-1])
    decision = _manual_decision(r)
    if decision:
        item["decision_manuelle"] = {"statut": decision[0], "libelle": STATUS_LABELS.get(decision[0], decision[0]),
                                     "note": red.text(decision[1]) or None}
    item["reponses"] = [_reply(e, red) for e in replies[-3:]]
    if len(replies) > 3:
        item["reponses_plus_anciennes"] = len(replies) - 3
    item["historique"] = [
        {"date": _iso(e.at), "type": e.kind, "resume": red.text(e.summary)}
        for e in r.events if e.kind != "reply"
    ][-25:]
    return item


def _manual_decision(r: Request) -> tuple[str, str] | None:
    """Décision prise à la main après la dernière réponse : (statut, note), ou None."""
    events = list(r.events)
    last = max((i for i, e in enumerate(events) if e.kind == "reply"), default=None)
    if last is None:
        return None
    for e in reversed(events[last + 1:]):
        if e.kind == "status" and (e.summary or "").startswith(MANUAL):
            label, _, note = e.summary[len(MANUAL):].partition(" — ")
            code = _LABEL_TO_STATUS.get(label.strip())
            if code:
                return code, note
    return None


# Parties de l'export : clé du fichier, statuts concernés, faut-il une réponse reçue.
SECTIONS = {
    "todo": ("elements", TODO_STATUSES, False),
    "done": ("terminees", [COMPLETED], False),
    "waiting": ("en_attente_apres_reponse", [SENT], True),
}


def build(db: Session, anonymize: bool = True, sections: set[str] | None = None) -> dict:
    """sections : todo (à traiter), done (terminées), waiting (réponse reçue puis remise en attente
    automatiquement : accusé, prolongation, lien confirmé, réponse automatique), examples (autres
    décisions prises à la main : refus, abandon…)."""
    sections = sections if sections is not None else {"todo", "examples"}
    red = Redactor(db) if anonymize else _Passthrough()
    load = (selectinload(Request.broker), selectinload(Request.events))
    out = {
        "format": "oubliez-moi/a-traiter",
        "version_format": 2,
        "application": __version__,
        "exporte_le": datetime.now().isoformat(timespec="seconds"),
        "anonymise": anonymize,
        "classement": {
            "mots_cles": settings.auto_classify,
            "ia": settings.llm_enabled,
            "modele_ia": settings.llm_model if settings.llm_enabled else None,
            "mode_ia": settings.llm_mode if settings.llm_enabled else None,
            "reponses_automatiques": settings.auto_replies,
            "liens_confirmes_automatiquement": settings.auto_confirm_links,
        },
        "categories": mailer.KIND_LABELS,
        "statuts": STATUS_LABELS,
        "a_lire": README,
    }
    covered: list[str] = []
    for key in ("todo", "done", "waiting"):
        if key not in sections:
            continue
        name, statuses, needs_reply = SECTIONS[key]
        q = select(Request).options(*load).where(Request.status.in_(statuses))
        if needs_reply:
            q = q.where(Request.last_reply_at.is_not(None))
        rows = db.scalars(q.order_by(Request.last_reply_at.desc(), Request.id.desc())).all()
        out[name] = [_item(r, red) for r in rows]
        covered += statuses
    if "examples" in sections:
        done = db.scalars(select(Request).options(*load)
                          .where(Request.status.not_in(covered + TODO_STATUSES), Request.last_reply_at.is_not(None))
                          .order_by(Request.last_reply_at.desc())).all()
        out["exemples_decides"] = [_item(r, red) for r in done if _manual_decision(r)][:MAX_EXAMPLES]
    out["totaux"] = {k: len(v) for k, v in out.items() if isinstance(v, list)}
    return out
