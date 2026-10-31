"""À traiter › Actions requises : catégories précises de ce qu'il faut faire."""
import html

from app.db import Broker, Event, Profile, Request, SessionLocal, utcnow

TEXTS = {
    "form": "Hello. This inbox is not actively monitored. Please use our Privacy Request Center to submit your request.",
    "link": "Please verify your identity by following the prompt in the email you received from privacy-requests@x.example.",
    "answer": "In order to start processing your erasure, we do need to confirm your relationship with Acme as either an "
              "employee or consumer.",
    "identity": "Please complete, sign and return the Identity Form attached hereto.",
}


def _request(slug, text, stored="Classement automatique : action demandée"):
    with SessionLocal() as db:
        b = Broker(slug=slug, name=f"Courtier {slug}", email=f"dpo@{slug}.example", countries=["fr"], categories=[])
        p = db.query(Profile).first() or Profile(full_name="Act Test", emails=["moi@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add_all([b, p])
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference=f"OUBLI-{slug.upper().replace('-', '')[-6:]}",
                    status="needs_action", sent_at=utcnow())
        db.add(r)
        db.flush()
        db.add(Event(request_id=r.id, kind="reply", summary="Réponse reçue",
                     body=f"De : DPO <dpo@{slug}.example>\nDate : hier\nObjet : Re: x\n\n{text}"))
        db.add(Event(request_id=r.id, kind="status", summary=stored))
        db.commit()
        return r.id


def test_needs_are_split_by_what_to_do(client):
    ids = {k: _request(f"act-{k}", t) for k, t in TEXTS.items()}
    page = html.unescape(client.get("/tasks?cat=needs").text)
    for label in ("Formulaire à remplir", "Lien à cliquer", "Réponse prête à envoyer", "Pièce d'identité demandée"):
        assert label in page, label
    form_only = html.unescape(client.get("/tasks?cat=needs:form").text)
    assert "Courtier act-form" in form_only and "Courtier act-answer" not in form_only
    # La liste montre le type d'action, et « En bref » le classement actuel (plus précis que celui enregistré)
    page = html.unescape(client.get(f"/tasks?cat=needs:answer&id={ids['answer']}").text)
    assert "Réponse prête" in page and "question simple sur votre demande" in page and "Réponse type prête" in page


def test_unknown_sub_category_falls_back_to_all_actions(client):
    _request("act-zz", TEXTS["form"])
    assert client.get("/tasks?cat=needs:nimportequoi").status_code == 200
