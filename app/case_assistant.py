"""Assistant RGPD prudent : analyse locale des réponses, brouillons et export de preuves."""
from __future__ import annotations

import io
import json
import zipfile

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import mailer
from .case_management import CaseEvent, PrivacyCase
from .case_management import TELEMARKETING
from .db import utcnow
from .web import get_db, render
from . import rules

router = APIRouter()
DRAFT_KINDS = {"initial", "followup", "appeal"}
ANALYSIS_ACTIONS = {
    "bounce": ("Vérifier l’adresse de contact et renvoyer la demande par un canal fiable.", "Ne pas considérer la demande comme reçue."),
    "id_request": ("Vérifier si une vérification d’identité est réellement nécessaire et proportionnée. Demander une alternative moins intrusive si possible.", "Ne transmettez pas de pièce d’identité complète sans avoir vérifié le destinataire et masqué les informations inutiles."),
    "confirm_link": ("Vérifier le domaine du lien indépendamment du message, puis confirmer uniquement si le lien est légitime.", "Ne cliquez pas sur un lien inattendu ou suspect."),
    "needs_action": ("Lire précisément l’action demandée et vérifier qu’elle est nécessaire avant de transmettre d’autres informations.", "Ne pas envoyer de données supplémentaires non nécessaires."),
    "completed_no_data": ("Conserver cette réponse comme preuve et vérifier qu’elle répond bien à la demande.", "Une déclaration d’absence de données n’est pas une preuve technique indépendante."),
    "completed_deleted": ("Conserver la réponse et vérifier si elle couvre toutes les données et tous les usages concernés.", "La suppression annoncée par l’organisme n’est pas vérifiée techniquement par Oubliez-moi."),
    "extension": ("Vérifier que la prolongation est motivée et que l’information a été communiquée dans le délai applicable.", "Ne pas recalculer automatiquement le délai sur la seule base d’une phrase ambiguë."),
    "legit_refusal": ("Demander les motifs précis et examiner la réponse au regard du droit concerné ; envisager un conseil ou une réclamation CNIL si nécessaire.", "Une qualification automatique ne constitue pas un avis juridique."),
    "refusal": ("Demander une explication motivée et conserver la réponse. Envisager les voies de recours adaptées.", "Ne pas déposer automatiquement une plainte."),
    "residence_question": ("Répondre uniquement avec le pays de résidence si cela est pertinent et nécessaire.", "Éviter de communiquer une adresse complète si le pays suffit."),
    "ack": ("Noter la date de réception et attendre la réponse de fond jusqu’à l’échéance applicable.", "Un accusé de réception n’est pas une réponse au fond."),
    "reply": ("Relire la réponse complète, la comparer à la demande initiale et consigner manuellement la décision.", "Résultat incertain : aucune conclusion n’est tirée automatiquement."),
}


def _case(db: Session, case_id: int) -> PrivacyCase:
    case = db.scalar(select(PrivacyCase).where(PrivacyCase.id == case_id).options(
        selectinload(PrivacyCase.organization), selectinload(PrivacyCase.profile),
        selectinload(PrivacyCase.events),
    ))
    if case is None:
        raise HTTPException(404)
    return case


def _render(request: Request, db: Session, case: PrivacyCase, **context):
    signal = signalconso_text(case) if case.request_type == TELEMARKETING else None
    return render(request, "case_assistant.html", db, **context, case=case, DRAFT_KINDS=DRAFT_KINDS, signalconso=signal)


def build_draft(case: PrivacyCase, kind: str) -> str:
    if kind not in DRAFT_KINDS:
        raise HTTPException(400, "Type de brouillon invalide.")
    profile = case.profile
    person = profile.full_name if profile else "[Nom et prénom]"
    email = profile.primary_email if profile and profile.primary_email else "[Adresse e-mail]"
    org = case.organization
    if case.request_type == TELEMARKETING and kind == "initial":
        return telemarketing_letter(case)
    if kind == "initial":
        subject = f"Exercice de mon droit RGPD — {case.type_label.lower()}"
        intro = f"Je souhaite exercer mon droit suivant : {case.type_label.lower()}."
    elif kind == "followup":
        subject = f"Suivi de ma demande RGPD — {case.reference or case.subject or 'sans référence'}"
        intro = "Je reviens vers vous au sujet de ma demande mentionnée ci-dessous, dont je souhaite connaître l’état de traitement."
    else:
        subject = f"Demande de réexamen — {case.reference or case.subject or 'sans référence'}"
        intro = "Je vous demande de bien vouloir réexaminer votre réponse et de préciser les motifs de votre position."
    lines = [
        f"À : {org.contact_email or '[Adresse de contact de l’organisme]'}",
        f"Objet : {subject}", "", "Madame, Monsieur,", "",
        intro, "",
        f"Organisme : {org.name}",
        f"Type de droit : {case.type_label}",
        f"Référence : {case.reference or '[Référence éventuelle]'}",
        f"Objet du dossier : {case.subject or '[Objet à préciser]'}", "",
    ]
    if kind == "initial":
        lines += [
            "Je vous remercie de traiter cette demande conformément au règlement (UE) 2016/679 (RGPD) et de me répondre dans les délais prévus.",
            "Merci de m’indiquer si des informations ou précisions strictement nécessaires sont requises pour traiter ma demande.",
        ]
    elif kind == "followup":
        lines += [
            f"Ma demande a été envoyée le {case.sent_at.strftime('%d/%m/%Y') if case.sent_at else '[date d’envoi]'}."
            " Merci de m’indiquer son état d’avancement et, le cas échéant, la date prévisionnelle de réponse.",
        ]
    else:
        lines += [
            "Merci de préciser les faits et fondements juridiques sur lesquels repose votre réponse, ainsi que les voies de recours disponibles.",
            "Je me réserve la possibilité de saisir l’autorité de contrôle compétente après examen de votre réponse.",
        ]
    lines += ["", f"{person}", email, "", "Cordialement,", person,
              "", "---", "Brouillon à relire et compléter. Vérifiez le destinataire, les faits et les pièces avant tout envoi."]
    return "\n".join(lines)


def _phone(case: PrivacyCase) -> str:
    phones = [p for p in ((case.profile.phones if case.profile else None) or []) if p]
    return phones[0] if phones else "[Votre numéro de téléphone]"


def telemarketing_letter(case: PrivacyCase) -> str:
    """Courrier après un appel de démarchage : opposition, effacement, origine du numéro, preuve du consentement."""
    profile, org = case.profile, case.organization
    person = profile.full_name if profile else "[Nom et prénom]"
    email = profile.primary_email if profile and profile.primary_email else "[Adresse e-mail]"
    call = (case.subject or "Appel de démarchage").replace("Appel de démarchage", "", 1).strip() or "[date et numéro de l’appel]"
    return "\n".join([
        f"À : {org.contact_email or '[Adresse du délégué à la protection des données ou du service client]'}",
        "Objet : Démarchage téléphonique non consenti — opposition, effacement et origine de mes données", "",
        "Madame, Monsieur,", "",
        f"J’ai reçu un appel de prospection commerciale de {org.name} ou pour son compte ({call}), sur mon numéro {_phone(case)}.",
        "Je n’ai pas consenti à être démarché par téléphone et je ne suis pas votre client.", "",
        "Depuis le 11 août 2026, la prospection commerciale par téléphone est interdite sans le consentement préalable "
        "du consommateur (loi du 30 juin 2025), sauf dans le cadre d’un contrat en cours. En conséquence :", "",
        "1. Je m’oppose à tout traitement de mes données à des fins de prospection (RGPD, article 21, paragraphes 2 et 3) "
        "et vous demande de cesser tout appel, par vous-même ou par vos prestataires.",
        "2. Je vous demande d’effacer mes données personnelles, en particulier mon numéro de téléphone (article 17), "
        "en ne conservant que ce qui est nécessaire pour respecter mon opposition.",
        "3. Je vous demande de m’indiquer l’origine de mes données, c’est-à-dire qui vous a transmis mon numéro "
        "(article 15, paragraphe 1, g), et à qui vous l’avez communiqué (article 15, paragraphe 1, c).",
        "4. Si vous estimez que j’avais consenti à être appelé, je vous demande d’en apporter la preuve : date, support "
        "et formulation exacte du consentement (article 7, paragraphe 1).",
        "5. Je vous demande de notifier mon opposition et cet effacement aux destinataires de mes données (article 19).", "",
        "Je vous remercie de me répondre par écrit dans le délai d’un mois prévu par l’article 12, paragraphe 3, du RGPD. "
        "À défaut, je saisirai la CNIL. Cet appel fait par ailleurs l’objet d’un signalement sur SignalConso.", "",
        "Je vous prie d’agréer, Madame, Monsieur, l’expression de mes salutations distinguées.", "",
        person, email,
        "", "---", "Brouillon à relire et compléter. Vérifiez le destinataire, la date et le numéro de l’appel avant tout envoi.",
    ])


def signalconso_text(case: PrivacyCase) -> str:
    """Texte à coller dans le signalement SignalConso (catégorie « Démarchage téléphonique »)."""
    call = (case.subject or "").replace("Appel de démarchage", "", 1).strip() or "[date et numéro de l’appel]"
    return (f"J’ai reçu un appel de démarchage commercial de {case.organization.name} ({call}) sur mon numéro {_phone(case)}. "
            "Je n’ai jamais donné mon accord pour être démarché par téléphone par cette entreprise et je n’ai aucun contrat "
            "en cours avec elle. Je lui ai demandé par écrit de cesser ses appels, d’effacer mes données et de m’indiquer "
            "l’origine de mon numéro ainsi que la preuve de mon consentement.")


@router.get("/privacy-cases/{case_id}/assistant", response_class=HTMLResponse)
def assistant_page(request: Request, case_id: int, kind: str = "initial", db: Session = Depends(get_db)):
    case = _case(db, case_id)
    if kind not in DRAFT_KINDS:
        kind = "initial"
    latest_analysis = next((e for e in reversed(case.events) if e.kind == "assistant_analysis"), None)
    return _render(request, db, case, kind=kind, draft_text=build_draft(case, kind), analysis=None,
                   latest_analysis=latest_analysis)


@router.post("/privacy-cases/{case_id}/assistant/analyze", response_class=HTMLResponse)
def analyze_reply(request: Request, case_id: int, subject: str = Form(""),
                  reply_text: str = Form(...), db: Session = Depends(get_db)):
    case = _case(db, case_id)
    text = reply_text.strip()
    if not text:
        raise HTTPException(400, "Collez le texte de la réponse à analyser.")
    if len(text) > 20000:
        raise HTTPException(413, "La réponse dépasse 20 000 caractères.")
    parsed = mailer.ParsedMail(uid="manual", from_addr=case.organization.contact_email or "",
                               from_name=case.organization.name, subject=subject.strip()[:300],
                               message_id="", text=text)
    result = rules.classify(parsed)
    action, caution = ANALYSIS_ACTIONS.get(result.kind, ANALYSIS_ACTIONS["reply"])
    summary = f"Analyse indicative : {rules.KIND_LABELS.get(result.kind, result.kind)}"
    detail = f"Catégorie : {result.kind}\nIndice détecté : {result.reason[:180] or 'aucun'}\nAction suggérée : {action}\nPrécaution : {caution}"
    db.add(CaseEvent(case=case, kind="assistant_analysis", summary=summary, body=detail))
    db.commit()
    case = _case(db, case_id)
    return _render(request, db, case, kind="initial", draft_text=build_draft(case, "initial"),
                   analysis={"label": rules.KIND_LABELS.get(result.kind, result.kind),
                             "action": action, "caution": caution, "reason": result.reason})


@router.post("/privacy-cases/{case_id}/assistant/draft")
def save_draft(request: Request, case_id: int, kind: str = Form(...),
               draft_text: str = Form(...), db: Session = Depends(get_db)):
    case = _case(db, case_id)
    if kind not in DRAFT_KINDS:
        raise HTTPException(400, "Type de brouillon invalide.")
    draft = draft_text.strip()
    if not draft or len(draft) > 30000:
        raise HTTPException(400, "Le brouillon est vide ou trop long (30 000 caractères maximum).")
    labels = {"initial": "Demande initiale", "followup": "Relance", "appeal": "Demande de réexamen"}
    db.add(CaseEvent(case=case, kind="assistant_draft", summary=f"Brouillon enregistré — {labels[kind]}",
                     body=f"Type : {kind}\n\n{draft}"))
    db.commit()
    request.session.setdefault("flash", []).append(["ok", "Brouillon conservé dans la chronologie. Aucun message n’a été envoyé."])
    return RedirectResponse(f"/privacy-cases/{case_id}/assistant?kind={kind}", status_code=303)


@router.get("/privacy-cases/{case_id}/export.zip")
def export_case_zip(case_id: int, db: Session = Depends(get_db)):
    case = _case(db, case_id)
    org = case.organization
    timeline = [{
        "date": event.at.isoformat() if event.at else None,
        "type": event.kind, "titre": event.summary, "details": event.body,
    } for event in case.events]
    manifest = {
        "exported_at": utcnow().isoformat(), "case_id": case.id,
        "organization": org.name, "contact": org.contact_email,
        "request_type": case.request_type, "request_type_label": case.type_label,
        "status": case.status, "status_label": case.status_label,
        "reference": case.reference, "subject": case.subject,
        "sent_at": case.sent_at.isoformat() if case.sent_at else None,
        "deadline_at": case.deadline_at.isoformat() if case.deadline_at else None,
        "effective_deadline": case.effective_deadline.isoformat() if case.effective_deadline else None,
        "events": len(timeline),
        "notice": "Les liens ou références de pièces sont exportés comme texte ; les fichiers externes ne sont pas copiés.",
    }
    md = [
        f"# Dossier RGPD #{case.id}", "", f"- Organisme : {org.name}",
        f"- Contact : {org.contact_email or 'non renseigné'}",
        f"- Droit : {case.type_label}", f"- Statut : {case.status_label}",
        f"- Référence : {case.reference or '—'}", f"- Objet : {case.subject or '—'}",
        f"- Envoi enregistré : {manifest['sent_at'] or '—'}",
        f"- Échéance : {manifest['effective_deadline'] or '—'}", "",
        "## Chronologie", "",
    ]
    for event in case.events:
        md += [f"### {event.at.strftime('%d/%m/%Y %H:%M') if event.at else 'Date inconnue'} — {event.summary}",
               "", event.body or "", ""]
    md += ["## Limites", "", manifest["notice"], ""]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("chronologie.json", json.dumps(timeline, ensure_ascii=False, indent=2))
        archive.writestr("dossier.md", "\n".join(md))
        archive.writestr("LISEZ-MOI.txt",
                         "Export Oubliez-moi. Les brouillons et notes sont des éléments de suivi, pas des preuves de transmission. "
                         "Les pièces référencées par URL ou chemin ne sont pas intégrées automatiquement.\n")
    buffer.seek(0)
    filename = f"dossier-rgpd-{case.id}.zip"
    return StreamingResponse(buffer, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})
