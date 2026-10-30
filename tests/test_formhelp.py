from datetime import timedelta

from sqlalchemy import select

from app import letters, service
from app.brokers import account_risk
from app.db import NEEDS_ACTION, OVERDUE, SENT, Broker, Profile, Request, SessionLocal, utcnow


_n = iter(range(1000))


def _webform_request():
    with SessionLocal() as db:
        b = Broker(slug=f"test-webform-only-{next(_n)}", name="Formulaire SA", webform="https://example.org/privacy-form", countries=["fr"], enabled=True)
        p = Profile(full_name="Camille Martin", emails=["camille@exemple.fr"], phones=["+33 6 12 34 56 78"],
                    addresses=["12 rue des Lilas 75011 Paris"], other_names=[], birth_date="01/02/1990")
        db.add_all([b, p])
        db.commit()
        r = service._create_request(db, p, b, cycle=1)
        db.commit()
        return r.id


def test_form_fields_and_message(client):
    rid = _webform_request()
    with SessionLocal() as db:
        r = db.get(Request, rid)
        assert r.status == NEEDS_ACTION
        f = dict(letters.form_fields(r))
        assert f["Prénom"] == "Camille" and f["Nom de famille"] == "Martin"
        assert f["Téléphone (national)"] == "06 12 34 56 78" and f["Téléphone (international)"] == "+33612345678"
        assert f["Adresse – code postal"] == "75011" and f["Adresse – ville"] == "Paris"
        assert f["Référence de la demande"] == r.reference
        assert "article 17" in letters.form_message(r, "fr") and "Article 17" in letters.form_message(r, "en")
    page = client.get(f"/requests/{rid}")
    assert "Remplir le formulaire en ligne" in page.text and "Copier" in page.text


def test_webform_sent_then_overdue_without_email_reminder(client):
    rid = _webform_request()
    client.post(f"/requests/{rid}/status", data={"status": "sent", "note": "ticket 42"})
    with SessionLocal() as db:
        r = db.get(Request, rid)
        assert r.status == SENT and r.sent_at and r.due_at
        r.due_at = utcnow() - timedelta(days=1)
        db.commit()
        assert service._next_reminder(db) is None or service._next_reminder(db).broker.email
    service.check_deadlines()
    with SessionLocal() as db:
        assert db.get(Request, rid).status == OVERDUE


def test_account_risk_flags():
    assert account_risk("gfk") and account_risk("zoominfo") and account_risk("awin")
    assert account_risk("criteo") is None and account_risk("gfk-gb")


def test_disabled_broker_queued_request_is_not_sent(client):
    with SessionLocal() as db:
        p = Profile(full_name="X Y", emails=["xy@exemple.fr"], phones=[], addresses=[], other_names=[])
        b = db.scalars(select(Broker).where(Broker.slug == "zoominfo")).one()
        db.add(p)
        db.commit()
        r = service._create_request(db, p, b, cycle=1)
        b.enabled = False
        db.commit()
        assert service._next_queued(db) is None or service._next_queued(db).broker.enabled
        b.enabled = True
        db.commit()
