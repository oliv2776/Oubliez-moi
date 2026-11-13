"""Historique structuré : auteur et catégorie des événements, sans relire le texte."""
from sqlalchemy import text

from app import actions, history
from app.db import Broker, Event, Profile, Request, SessionLocal, _backfill_event_columns, engine


def test_infer_from_old_texts():
    cases = {
        ("status", "Classement automatique : aucune donnée détenue (« no data »)"): ("rules", "completed_no_data"),
        ("status", "Classement automatique : demande de pièce d'identité (« IA, probabilité 88 % »)"): ("ai", "id_request"),
        ("status", "Classement automatique : Terminée (« has been erased »)"): ("rules", "status:completed"),
        ("status", "Réanalyse : accusé de réception (« IA, probabilité 95 % »)"): ("reanalysis-ai", "ack"),
        ("status", "Réanalyse : formulaire ou portail exigé (« x »)"): ("reanalysis", "webform"),
        ("status", "Statut changé manuellement : Refusée — ils gardent tout"): ("manual", "refused"),
        ("status", "Relance manuelle envoyée"): ("system", None),
        ("info", "Analyse IA : résumé"): ("ai", "summary"),
        ("info", "Conseil IA : cliquez"): ("ai", "advice"),
        ("warning", "Réponse non vérifiée de x@y.example : décision sensible non appliquée"): ("system", "unverified"),
        ("note", "Classement automatique : faux ami"): (None, None),
    }
    for (kind, summary), expected in cases.items():
        assert history.infer(kind, summary) == expected, summary


def _request(slug):
    with SessionLocal() as db:
        b = Broker(slug=slug, name=slug, email=f"dpo@{slug}.example", countries=["fr"], categories=[])
        p = Profile(full_name="Hist Test", emails=["moi@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add_all([b, p])
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference=f"OUBLI-{slug.upper()[-6:]}", status="completed")
        db.add(r)
        db.commit()
        return r.id


def test_events_created_before_the_columns_are_backfilled(client):
    rid = _request("hist01")
    with engine.begin() as conn:  # événement « ancien » : écrit sans passer par le modèle
        conn.execute(text("INSERT INTO events (request_id, at, kind, summary) VALUES (:r, '2026-10-30 10:00:00', 'status', "
                          "'Statut changé manuellement : Terminée')"), {"r": rid})
        _backfill_event_columns(conn)
    with SessionLocal() as db:
        ev = db.query(Event).filter_by(request_id=rid).one()
        assert (ev.actor, ev.category) == ("manual", "completed")


def test_a_reworded_label_no_longer_changes_the_meaning(client):
    """Le texte peut changer : c'est la colonne qui dit qui a décidé."""
    from app import reanalysis
    rid = _request("hist02")
    with SessionLocal() as db:
        db.add(Event(request_id=rid, kind="status", summary="Décision de l'utilisateur", actor="manual", category="completed"))
        db.commit()
        assert not reanalysis.decided_automatically(db.get(Request, rid))


def test_reply_classification_is_computed_once(client, monkeypatch):
    rid = _request("hist03")
    with SessionLocal() as db:
        db.add(Event(request_id=rid, kind="reply", summary="Réponse", body="De : <a@b.example>\nObjet : Re\n\nPlease use our online form."))
        db.commit()
        ev = db.query(Event).filter_by(request_id=rid).one()
        assert actions.kind_of_reply(ev) == "webform"
        monkeypatch.setattr(actions.mailer, "classify", lambda m: 1 / 0)  # un second calcul échouerait
        assert actions.kind_of_reply(ev) == "webform"
