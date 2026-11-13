"""Règles de classement des réponses des courtiers (mots-clés, sans IA).

Entrée : un message analysé (mailer.ParsedMail). Sortie : une catégorie (`KIND_LABELS`) et le passage
qui l'a déclenchée. Le texte cité (notre propre courrier) est écarté avant tout classement.
Chaque règle vient d'un cas réel : voir tests/test_rules_from_export.py, et tools/replay_export.py
pour rejouer les règles sur un export « À traiter ».
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mailer import ParsedMail

_P = lambda *pats: re.compile("|".join(pats), re.I)  # noqa: E731

ID_RE = _P(
    r"cop(y|ies) of (your |a |an )?(valid )?(government[- ]issued )?(photo )?(id\b|identity|passport|driv)", r"proof of (your )?identity",
    r"photo id", r"identity document", r"pi[eè]ce d'identit[eé]", r"justificatif d'identit[eé]", r"copie de (votre |la )?(carte|passeport)",
    r"identit[äa]tsnachweis", r"ausweiskopie", r"kopie (ihres|des) (personal)?ausweis",
    r"identi(ty|fication) (verification )?form\b", r"formulaire (d'identification|de v[ée]rification d'identit[ée])", r"identit[äa]tsformular",
)
CONFIRM_RE = _P(
    r"confirm (your |the )?(e-?mail|request|address|identity) by clicking", r"(click|tap) (on )?(the|this) (following |below )?(link|button) to (confirm|verify|validate)",
    r"please (confirm|verify) (your |the )?(e-?mail|request|address)", r"verification link", r"confirmation link",
    r"cliquez sur le lien (ci-dessous |suivant )?pour (confirmer|valider|v[ée]rifier)", r"confirmer votre (demande|adresse)", r"valider votre (demande|adresse)",
    r"lien de (confirmation|validation|v[ée]rification)", r"best[äa]tigen sie (ihre|die) (anfrage|e-?mail)", r"best[äa]tigungslink",
    # Le lien est dans un autre message (« follow the prompt in the email you received from… »)
    r"follow(ing)? the (prompt|link|instructions|steps) (in|from) the (e-?mail|message) (you (have )?received|we (have )?sent)",
    r"(click|use|open) the (verification |confirmation )?link (in|from) the (e-?mail|message) (you (have )?received|we (have )?sent)",
    r"(en suivant|cliquez sur|ouvrez) le lien (re[çc]u|envoy[ée]|figurant) (dans|par) (l'|un |le )?(e-?mail|courriel|message)",
)
VERIFY_RE = _P(
    r"verify your identity", r"identity verification", r"v[ée]rifier votre identit[ée]", r"confirmer votre identit[ée]",
)
# Le courtier demande d'écrire depuis une autre adresse (celle dont on demande aussi l'effacement).
_EMAIL = r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+"
CONFIRM_FROM_RE = _P(
    r"(send|reply|write|respond|e-?mail us|confirm)[^.\n]{0,80}\bfrom (the |that |this |your )?(e-?mail )?(address )?(concerned|in question|" + _EMAIL + ")",
    r"(confirm|confirmation)[^.\n]{0,60}(directly )?from (the )?(e-?mail )?(address )?" + _EMAIL,
    r"(depuis|[àa] partir de) (l'adresse|cette adresse|l'adresse e-?mail|l'adresse [ée]lectronique)( e-?mail)? (concern[ée]e|en question|" + _EMAIL + ")",
    r"von (der|dieser|ihrer) (betreffenden |genannten )?e-?mail-?adresse (aus )?(schreiben|senden|best[äa]tigen)",
)
# Le courtier n'accepte que son formulaire / portail, ou cette adresse n'est pas lue.
WEBFORM_RE = _P(
    r"fill (in|out) (the|our|this) (web ?|online ?)?form", r"use our (web ?|online ?|privacy )?(form|portal)", r"submit (your request )?(via|through) our",
    r"(please|kindly) (visit|use|go to|complete|fill (in|out)|click|submit[^.\n]{0,40}(via|through|using|on))[^.\n]{0,90}"
    r"(portal|privacy (center|centre|hub|tool|page|request page|choices)|request (center|centre|page|form)|web ?form|online form|request form|\bform\b|rights request)",
    r"(privacy rights requests|requests?)[^.\n]{0,80}(must|should|need to|have to) be (submitted|made|sent|filed)",
    r"(requests?)[^.\n]{0,40}(made|filed|sent|submitted) (otherwise|through other channels|by e-?mail|via e-?mail)[^.\n]{0,30}(cannot|will not|won't|can't|are not)",
    r"(cannot|can't|will not|won't) be (made|submitted|processed|handled) (through|via|by) (e-?mail|this|privacy@)",
    r"(inbox|mailbox|e-?mail address) is (no longer|not) (actively |being )?(monitored|checked)",
    r"(inbox|mailbox) is (currently )?(nonfunctional|non-functional|not functional|unable to receive)",
    r"(will|do) not (receive|get) a response", r"(dedicated|secure) (online |web )?(form|portal)",
    r"follow the instructions[^.\n]{0,40}\n?[^.\n]{0,80}privacy (policy|notice)",
    r"to exercise (your|any|applicable|these|data subject|your applicable)[^.\n]{0,40}rights,?[^.\n]{0,20}(please )?(visit|use|click|go to|follow|submit)",
    r"(if you('re| are) looking to|if you would like to|if you wish to|if you want to) (make|submit|exercise)[^.\n]{0,80}(please )?(visit|click|use|go to)",
    r"submit (a|an|your) [^.\n]{0,30}request[^.\n]{0,40}(through|via|using|on) [^.\n]{0,50}(portal|form|privacy cent(er|re)|request page)",
    r"only way[^.\n]{0,60}(is|would be) (through|via|using) (our|the|a) (web ?form|form|portal)",
    r"(merci de|veuillez|nous vous invitons [àa]) (passer par|utiliser|remplir|compl[ée]ter)[^.\n]{0,40}(formulaire|portail|espace)",
    r"(remplir|utiliser) (le|notre) formulaire", r"via (notre|le) (formulaire|portail)",
    r"(nutzen|verwenden) sie (bitte )?(unser|das)[^.\n]{0,30}(formular|portal)", r"[üu]ber (unser|das) [^.\n]{0,30}(formular|portal)",
)
# Le courtier ne connaît que des identifiants publicitaires (cookie, appareil) et les réclame.
DEVICE_RE = _P(
    r"(cookie|device|advertising|online|mobile advertising) ?(id|identifier)s?\b[^.\n]{0,80}(is |are )?(required|needed|necessary)",
    r"(provide|send|share|give)( us)?( with)? (your|the) [^.\n]{0,50}(cookie ?id|device id|advertising id|identifier|idfa|aaid|maid)",
    r"(without|in the absence of) (such |these |further information such as your [^.\n]{0,40})?identifiers",
    r"(need|require) (you to provide|your)[^.\n]{0,40}(cookie|device|advertising) ?(id|identifier)",
    r"(identifiant|id) (cookie|publicitaire|de l'appareil)[^.\n]{0,60}(n[ée]cessaire|requis|indispensable)",
    r"(screenshots?|copy|value) of (the )?(relevant )?identifiers", r"onlinekennung", r"(cookie|werbe)-?id\b[^.\n]{0,60}(mit|senden|nennen)",
)
NO_DATA_RE = _P(
    r"no (personal )?(data|information|records?) (about|on|relating to|regarding|associated with|linked to) you",
    r"(do not|don't|does not|doesn't) (hold|have|store|process)( or (hold|have|store|process))? any (other )?(personal )?(data|information)",
    r"(could|were|did) not (find|locate|identify) any", r"unable to (find|locate|identify) (any|your)", r"no records? (were |was )?found",
    r"did not identify any", r"(could|were|did) not (find|locate|identify) (your|an?) (account|record|profile|data)", r"no (personal )?data to (be )?(erased|deleted)", r"(do not|does not|don't|doesn't) (maintain|keep) (any )?records",
    r"aucune donn[ée]e(?![^.\n]{0,60}(communication|communiqu|transmi|partag|c[ée]d[ée]|vendu))",
    r"ne (d[ée]tenons|disposons|poss[ée]dons|traitons) (d')?aucune", r"pas de donn[ée]es (vous )?concernant",
    r"n'avons pas (pu )?trouv(er|[ée]) (de |d'|aucun|un enregistrement)", r"n'avons pas enregistr[ée] de donn[ée]es", r"ne parvenons pas [àa] trouver",
    r"ne figurent? pas dans (notre|nos) (base|fichier)", r"not holding any (personal )?data",
    r"n'apparaissez pas", r"ne figurez pas", r"keine (personenbezogenen )?daten", r"nicht (in unserer datenbank )?gespeichert",
)
DELETED_RE = _P(
    r"(has|have|had) (now )?been (permanently |successfully )?(deleted|erased|removed|anonymi[sz]ed)",
    r"(we|i) (have )?(permanently |successfully )?(deleted|erased|removed) (all )?(your|the)",
    r"successfully (deleted|erased|removed)", r"(added|placed) (you |your [a-z ]+)?(to|on) (our|the) (suppression|opt-?out|do not contact|blocking) list",
    r"(ont|a) [ée]t[ée] (d[ée]finitivement )?(supprim|effac|anonymis)", r"nous avons (bien )?(proc[ée]d[ée] [àa] la suppression|supprim|effac)",
    r"suppression (a [ée]t[ée] )?effectu[ée]e", r"(liste|fichier) (d'opposition|repoussoir|de blocage)",
    r"have (now )?(checked [^.\n]{0,40} and )?(have )?(permanently |successfully )?(deleted|erased|removed) (all )?(of )?your (personal )?data",
    r"wurden? (vollst[äa]ndig |erfolgreich )?gel[öo]scht", r"haben (ihre daten )?gel[öo]scht", r"(sperrliste|werbesperre)",
)
EXTENSION_RE = _P(
    r"(extend|extension of) (the |our |this )?(response |time ?|deadline|period)", r"(additional|further|another) (two|2) months",
    r"deux mois suppl[ée]mentaires", r"prolong(er|ation) (le|du|de notre) d[ée]lai", r"d[ée]lai suppl[ée]mentaire",
    r"fristverl[äa]ngerung", r"weitere(n)? (zwei|2) monate",
)
LEGIT_RE = _P(r"legitimate interests?", r"int[ée]r[êe]ts? l[ée]gitimes?", r"berechtigte[sn]? interesse")
REFUSAL_RE = _P(
    r"unable to (comply|delete|fulfil|fulfill|honou?r)", r"(cannot|can't|will not|won't) (comply|delete|erase|fulfil)", r"not able to (delete|erase|comply)",
    r"(ne pouvons|ne sommes pas en mesure de) (pas )?(donner suite|supprimer|proc[ée]der|faire droit)", r"refusons", r"rejet[ée]e?",
    r"k[öo]nnen .{0,40}nicht (l[öo]schen|nachkommen|entsprechen)",
)
RESIDENCE_RE = _P(
    r"which country (did|do|are) you", r"what country (do|are|did) you", r"(country|state)( and state)?( or region)? in which you (are located|reside|live)",
    r"(country|state) of residence", r"which country (do )?you (live|reside)", r"where (do )?you (live|reside)", r"are you (a )?(resident|located) in",
    r"pays de r[ée]sidence", r"dans quel pays (r[ée]sidez|habitez)", r"wohnsitzland", r"in welchem land (wohnen|leben)",
)
ASK_RE = _P(
    r"(could|would|can) you (please )?(confirm|provide|send|tell us|indicate|specify|clarify|respond|reply)",
    r"please (provide|confirm|send us|indicate|specify|reply with|respond with)", r"(we )?(therefore )?ask you to (send|confirm|provide|indicate)",
    r"we (would |do |will )?(need|require) (you to|your|the following|additional|further|more|to confirm)",
    r"pourriez-vous", r"pouvez-vous (nous )?(indiquer|confirmer|pr[ée]ciser|communiquer|envoyer)",
    r"merci de (bien vouloir )?(nous )?(confirmer|indiquer|pr[ée]ciser|communiquer|transmettre|envoyer)",
    r"nous vous (remercions|prions|demandons) de (bien vouloir )?(nous )?(confirmer|indiquer|pr[ée]ciser|communiquer|transmettre|lancer|faire)",
    r"bitte (teilen|senden|best[äa]tigen|nennen) sie",
)
# Questions simples auxquelles une réponse type suffit (voir clarification_topic).
CLARIFY_TOPICS = (
    ("relationship", _P(
        r"relationship (with|to) [^.\n]{0,60}(employee|customer|consumer|client|vendor|candidate)",
        r"(are you|whether you are|if you are) (an? |our )?(current |former )?(employee|customer|client|consumer|candidate)",
        r"(êtes-vous|si vous êtes|[ée]tiez-vous) (un |une |notre )?(client|salari[ée]|employ[ée]|collaborat|candidat)",
        r"(votre )?(lien|relation) avec (notre|la) (soci[ée]t[ée]|entreprise|groupe)",
    )),
    ("unknown_details", _P(
        r"\b(user ?name|account (name|number|id)|customer (number|id)|membership number|order number)\b",
        r"\b(work|business|professional|corporate) e-?mail( address)?\b",
        r"nom d'utilisateur|identifiant de connexion|num[ée]ro (de )?(client|compte|commande|adh[ée]rent)|e-?mail professionnel|adresse professionnelle",
        r"\b(agence|campagne|campaign|advertis(ing|ement) campaign)\b[^.\n]{0,80}", r"p[ée]riode concern[ée]e|time ?frame concerned|period concerned",
        r"\bbenutzername|kundennummer|kundenkonto\b",
    )),
    ("which_email", _P(
        r"(confirm|indicate|provide|tell us|specify)[^.\n]{0,40}\b(e-?mail )?address(es)? (concerned|in question|you used|used|associated)",
        r"(confirmer|indiquer|pr[ée]ciser|communiquer)[^.\n]{0,40}l'adresse (e-?mail |[ée]lectronique )?(concern[ée]e|utilis[ée]e|en question)",
    )),
)


def normalize_reply(text: str) -> str:
    """Apostrophes typographiques et lignes coupées à 72 caractères recollées (les paragraphes restent séparés)."""
    text = (text or "").replace("\u2019", "'")
    return re.sub(r"[ \t]+", " ", re.sub(r"(?<!\n)[ \t]*\n[ \t]*(?!\n)", " ", text))


def clarification_topic(text: str) -> str | None:
    """Sujet de la question simple posée par le courtier : relationship | unknown_details | which_email."""
    text = normalize_reply(text)
    for topic, rx in CLARIFY_TOPICS:
        if rx.search(text):
            return topic
    return None


# Ce que dit le courtier juste après « aucune donnée » quand il attend encore quelque chose.
_BUT_RE = re.compile(r"\b(however|but|nevertheless|cependant|toutefois|n[ée]anmoins|jedoch|allerdings)\b", re.I)
# Délais et incidents de distribution temporaires, ou limités à un membre d'une liste de diffusion.
DELIVERY_ISSUE_RE = _P(
    r"\bdelay(ed)?\b", r"temporar(y|ily)", r"will (be )?retr(y|ied)", r"(d'autres|nouvelles) tentatives", r"probl[èe]me temporaire",
    r"vor[üu]bergehend", r"verz[öo]gert", r"mailbox (is )?full", r"quota", r"\b5\.2\.2\b", r"bo[îi]te (aux lettres |de r[ée]ception )?pleine",
)
# Phrases de nos propres courriers : si elles apparaissent, c'est notre message cité.
OWN_LETTER_RE = re.compile(
    r"(?is)(pursuant to article 17 of the general data protection regulation \(gdpr, regulation \(eu\) 2016/679\), i request"
    r"|conform[ée]ment [àa] l'article 17 du r[èe]glement g[ée]n[ée]ral sur la protection des donn[ée]es \(rgpd, r[èe]glement \(ue\) 2016/679\), je vous demande"
    r"|please confirm in writing that my data has been erased|je vous remercie de me confirmer par [ée]crit l'effacement de mes donn[ée]es"
    r"|on [^\n]{1,40}, i sent you a request for the erasure of my personal data|le [^\n]{1,40}, je vous ai adress[ée] une demande d'effacement"
    r"|you have asked me for proof of identity in order to process my erasure request|vous me demandez un justificatif d'identit[ée] pour traiter"
    r"|you have declined my request of [^\n]{1,40} on the basis of your legitimate|vous refusez de donner suite [àa] ma demande du"
    r"|in answer to your question: i reside in|en r[ée]ponse [àa] votre question : je r[ée]side)"
    .replace(" ", r"\s+")
)
# Formules qui annoncent une vérification éventuelle, sans rien demander.
# (ce qui arrivera plus tard, ou ce que le courtier n'exige pas, n'est pas une demande)
_MAYBE_LATER_RE = re.compile(
    r"(?i)(we )?may need to (confirm|verify) your identity[^.\n]*|(vous allez recevoir|vous recevrez|you will (then )?receive|you'll receive)[^.\n]{0,120}"
    r"|(do not|don't|does not|will not|won't|no) (require|need|ask for)[^.\n]{0,30}(identity document|proof of identity|copy of (your )?id|photo id|pi[èe]ce d'identit[ée])"
)

ACK_RE = _P(
    r"accus[ée] de r[ée]ception", r"accusons r[ée]ception", r"automatic reply", r"auto(matic)?[- ]?reply", r"out of (the )?office", r"r[ée]ponse automatique",
    r"we (have )?received your (request|message|e-?mail)", r"nous avons (bien )?re[çc]u votre",
    r"ticket (number|#|no)", r"request (number|#|id)", r"eingangsbest[äa]tigung",
    r"(request|message|e-?mail|inquiry) (has been|was|have been) (successfully )?(received|submitted|logged)", r"confirm (the )?receipt",
    r"(has been|was) forwarded to", r"automatische antwort", r"abwesenheit",
    r"(passed|forwarded|transferred|escalated) (your request|it|this|your message) to", r"will (get back to|be in touch with|contact|respond to|reply to|email) you",
    r"(are|is|will be) (usually |normally |generally )?processed within", r"we will (process|handle|review) (your|the|this) request",
    r"nous reviendrons vers vous", r"wir (melden uns|werden uns)[^.\n]{0,40}(melden|zur[üu]ckmelden)",
)
# Formules de politesse : un accusé seulement si le message s'arrête là (sinon, il dit autre chose).
ACK_WEAK_RE = _P(r"thank you for (contacting|your (request|message|e-?mail)|submitting|reaching out)", r"merci (de|pour) votre (message|demande)")
ACK_WEAK_MAX = 700

KIND_LABELS = {
    "bounce": "adresse invalide",
    "id_request": "demande de pièce d'identité",
    "confirm_link": "lien de confirmation à cliquer",
    "needs_action": "action demandée",
    "completed_no_data": "aucune donnée détenue",
    "completed_deleted": "données supprimées",
    "extension": "prolongation du délai",
    "legit_refusal": "refus au nom de l'intérêt légitime",
    "refusal": "refus probable",
    "residence_question": "question sur le pays de résidence",
    "ack": "accusé de réception",
    "webform": "formulaire ou portail exigé",
    "confirm_from_address": "confirmation depuis une autre adresse demandée",
    "clarification": "question simple sur votre demande",
    "device_id": "identifiant publicitaire demandé",
    "delivery_issue": "incident de distribution temporaire",
    "reply": "réponse à examiner",
}


@dataclass
class Classification:
    kind: str
    reason: str = ""


_QUOTE_START_RE = re.compile(
    r"(?im)^\s*(?:[-_]{2,}\s*(?:original message|message d'origine|message initial|forwarded message|urspr[üu]ngliche nachricht|"
    r"original nachricht|oorspronkelijk bericht|messaggio originale|mensaje original|weitergeleitete nachricht)\s*[-_]*|"
    r"(?:original message|urspr[üu]ngliche nachricht|message d'origine)\s*:?|"
    r"begin forwarded message:|on .{1,240} wrote:|le .{1,240} a [ée]crit\s*:|.{0,240}\b(?:wrote|schrieb|a [ée]crit|schreef|skrev|escribi[óo]|ha scritto)\s*:|"
    r"\*{2,}\s*(?:original message|message d'origine)\s*\*{2,})\s*$"
)
# En-têtes « De / Envoyé / À / Objet » d'Outlook et équivalents, dans les langues courantes.
_HEADER_FROM_RE = re.compile(r"(?i)^\s*\**(?:from|de|von|van|fr[åa]n|da|fra|od|desde)\s*:")
_HEADER_NEXT_RE = re.compile(
    r"(?i)\b(?:sent|envoy[ée]|to|[àa]|subject|objet|date|datum|gesendet|an|betreff|verzonden|aan|onderwerp|skickat|till|[äa]mne|"
    r"inviato|oggetto|enviado|para|asunto|cc)\s*:"
)


def latest_reply_text(body: str) -> str:
    """Écarte le bloc cité pour ne classer que la réponse la plus récente."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    own = OWN_LETTER_RE.search(text)
    if own:  # notre propre courrier, cité sans marque reconnaissable (ticket, client mail exotique)
        text = text[:text.rfind("\n", 0, own.start()) + 1]
    lines = text.split("\n")
    kept = []
    for index, line in enumerate(lines):
        if not line.strip():
            kept.append(line)
            continue
        if line.lstrip().startswith(">") or _QUOTE_START_RE.match(line):
            break
        # Outlook et certains clients introduisent la citation par un bloc d'en-têtes.
        if _HEADER_FROM_RE.match(line):
            following = " ".join(lines[index + 1:index + 5])
            if _HEADER_NEXT_RE.search(following):
                break
        kept.append(line)
    return "\n".join(kept).strip()


def classify(m: ParsedMail) -> Classification:
    current_reply = normalize_reply(latest_reply_text(m.text))
    text = f"{m.subject}\n{current_reply}"[:20000]
    if m.is_bounce:
        hit = DELIVERY_ISSUE_RE.search(f"{m.subject}\n{current_reply[:1500]}")
        if hit:
            return Classification("delivery_issue", hit.group(0))
        return Classification("bounce", "message de non-distribution")
    text = _MAYBE_LATER_RE.sub(" ", text)
    # « Aucune donnée » ne clôt que si le courtier n'attend rien d'autre (« however… », question).
    vetoed = None
    for kind, rx in (("completed_no_data", NO_DATA_RE), ("completed_deleted", DELETED_RE)):
        hit = rx.search(text)
        if hit:
            after = text[hit.end():hit.end() + 400]
            ask = _BUT_RE.search(after) or ASK_RE.search(text) or DEVICE_RE.search(text) or WEBFORM_RE.search(text)
            if not ask:
                return Classification(kind, hit.group(0))
            vetoed = vetoed or hit.group(0)
    for kind, rx in (
        ("confirm_from_address", CONFIRM_FROM_RE),
        ("id_request", ID_RE),
        ("confirm_link", CONFIRM_RE),
        ("device_id", DEVICE_RE),
        ("webform", WEBFORM_RE),
        ("needs_action", VERIFY_RE),
        ("extension", EXTENSION_RE),
    ):
        hit = rx.search(text)
        if hit:
            return Classification(kind, hit.group(0))
    refusal = REFUSAL_RE.search(text)
    legit = LEGIT_RE.search(text)
    if legit and refusal:
        return Classification("legit_refusal", legit.group(0))
    if refusal:
        return Classification("refusal", refusal.group(0))
    hit = RESIDENCE_RE.search(text)
    if hit:
        return Classification("residence_question", hit.group(0))
    hit = ASK_RE.search(text)
    if hit or vetoed:
        # Une question à laquelle une réponse type suffit (lien avec l'entreprise, adresse concernée, compte…)
        for _topic, rx in CLARIFY_TOPICS:
            q = rx.search(text)
            if q:
                return Classification("clarification", q.group(0))
        return Classification("needs_action", hit.group(0) if hit else f"{vetoed}, mais…")
    hit = ACK_RE.search(text) or (ACK_WEAK_RE.search(text) if len(current_reply) <= ACK_WEAK_MAX else None)
    if hit or m.is_auto:
        return Classification("ack", hit.group(0) if hit else "en-tête de réponse automatique")
    return Classification("reply", "")


_BOUNCED_TO_RE = re.compile(
    r"(?i)(?:failed to these recipients or groups:\s*|your message to |n'est pas parvenu [àa] |could not be delivered to |"
    r"wasn't delivered to |konnte nicht an |delivery to the following recipients? failed[^\n]*\s*)<?([\w.+'-]+@[\w.-]+\w)"
)


def emails_in(text: str) -> list[str]:
    """Adresses e-mail citées dans un texte, en minuscules, dans l'ordre et sans doublon."""
    out: list[str] = []
    for a in re.findall(_EMAIL, text or ""):
        a = a.lower().strip(".")
        if a not in out:
            out.append(a)
    return out


def bounced_recipients(text: str) -> set[str]:
    """Adresses que le rapport de non-distribution déclare en échec (vide si on ne sait pas)."""
    return {a.lower() for a in _BOUNCED_TO_RE.findall(text or "")}
