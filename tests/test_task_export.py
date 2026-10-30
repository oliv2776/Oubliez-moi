"""Export pour analyse : réponses à traiter, terminées, traitées sans l'utilisateur, avec masquage."""
import json

from app import task_export
from app.db import Broker, Event, Profile, Request, SessionLocal

REPLY = ("De : DPO <dpo@{slug}.example>\nDate : hier\nObjet : Re: Olivier Testeur\n\n"
         "Dear Olivier Testeur,\n\n{text}\n\nYou wrote from moi.perso@gmail.com, phone +33 6 12 34 56 78, "
         "12 rue des Lilas, 75011 Paris, born 14/03/1985. Mobile 06 12 34 56 78. Mr Testéur. moi.perso couldn't be reached. Confirm: https://{slug}.example/c/AbC123xyz987TOKENabc?u=42\n\n"
         "On Mon, Olivier wrote:\n> Je vous demande de supprimer mes données")


def _make(slug, status, text, events):
    with SessionLocal() as db:
        b = db.query(Broker).filter_by(slug=slug).one_or_none() or Broker(
            slug=slug, name=f"Courtier {slug}", email=f"dpo@{slug}.example", countries=["fr"], categories=[])
        p = db.query(Profile).first() or Profile(
            full_name="Olivier Testeur", emails=["moi.perso@gmail.com"], phones=["06 12 34 56 78"],
            addresses=["12 rue des Lilas, 75011 Paris"], other_names=[], birth_date="1985-03-14")
        db.add_all([b, p])
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference=f"OUBLI-{slug.upper()[:6]}", status=status,
                    last_reply_at=task_export.datetime(2026, 10, 1))
        db.add(r)
        db.flush()
        db.add(Event(request_id=r.id, kind="reply", summary="Réponse reçue", body=REPLY.format(slug=slug, text=text)))
        for kind, summary in events:
            db.add(Event(request_id=r.id, kind=kind, summary=summary))
        db.commit()
        return r.id


def _seed():
    _make("exp-todo", "replied", "We need more details about your request.",
          [("status", "Classement automatique : réponse à examiner")])
    _make("exp-done", "completed", "Your data has been deleted.",
          [("status", "Classement automatique : données supprimées (« has been deleted »)")])
    _make("exp-ia", "completed", "We will look into it.",
          [("status", "Classement automatique : aucune donnée détenue (« IA, probabilité 81 % »)")])
    _make("exp-ack", "sent", "Thank you for your request, ticket number 4411.",
          [("info", "Accusé de réception détecté (« thank you for your request »)")])
    _make("exp-refus", "refused", "We cannot delete your data.",
          [("status", "Classement automatique : refus probable"), ("status", "Statut changé manuellement : Refusée — ils gardent tout")])


def test_sections_and_how_each_request_was_closed():
    _seed()
    with SessionLocal() as db:
        data = task_export.build(db, sections={"todo", "done", "waiting", "examples"})
    assert data["totaux"] == {"elements": 1, "terminees": 2, "en_attente_apres_reponse": 1, "exemples_decides": 1}
    done = {x["courtier"]["slug"]: x for x in data["terminees"]}
    assert done["exp-done"]["dernier_changement_de_statut"]["par"] == "regles"
    assert done["exp-ia"]["dernier_changement_de_statut"]["par"] == "ia"
    # Les règles actuelles, rejouées sur le texte : la réponse vague n'aurait pas été close sans l'IA.
    assert done["exp-ia"]["regles_actuelles"]["categorie"] == "reply"
    assert done["exp-done"]["regles_actuelles"]["declencheur"] == "has been deleted"
    assert data["en_attente_apres_reponse"][0]["classement_enregistre"]["categorie"] == "ack"
    ex = data["exemples_decides"][0]
    assert ex["decision_manuelle"] == {"statut": "refused", "libelle": "Refusée", "note": "ils gardent tout"}
    # Seule la partie nouvelle de la réponse : notre demande citée est retirée.
    assert "supprimer mes données" not in json.dumps(data, ensure_ascii=False)


def test_personal_data_is_masked_by_default():
    _seed()
    with SessionLocal() as db:
        masked = json.dumps(task_export.build(db, sections={"todo"}), ensure_ascii=False)
        raw = json.dumps(task_export.build(db, anonymize=False, sections={"todo"}), ensure_ascii=False)
    for secret in ("Olivier", "Testeur", "Testéur", "moi.perso", "moi.perso@gmail.com", "12 34 56 78", "rue des Lilas", "75011", "14/03/1985",
                   "AbC123xyz987TOKENabc", "u=42"):
        assert secret not in masked, secret
        assert secret in raw, secret
    for mark in ("[MON_NOM]", "[MON_EMAIL]", "[MON_TÉLÉPHONE]", "[MON_ADRESSE]", "[MA_DATE_DE_NAISSANCE]", "[jeton]"):
        assert mark in masked, mark
    # Le texte du courtier et son domaine restent lisibles.
    assert "We need more details about your request." in masked and "dpo@exp-todo.example" in masked


def test_export_route_and_menu(client):
    _seed()
    page = client.get("/tasks").text
    assert 'action="/tasks/export"' in page and 'value="done"' in page
    res = client.get("/tasks/export?parts=done&parts=todo")
    assert res.status_code == 200 and res.headers["content-disposition"].startswith("attachment;")
    data = res.json()
    assert data["anonymise"] is True and set(data["totaux"]) == {"elements", "terminees"}
    assert "Olivier" not in res.text
    assert client.get("/tasks/export").json()["totaux"].keys() == {"elements", "exemples_decides"}


def test_completed_list_offers_analysis_export(client):
    assert "/tasks/export?parts=done" in client.get("/requests?status=completed").text
    assert "/tasks/export?parts=done" not in client.get("/requests").text
