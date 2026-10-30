from app.db import Broker, ExposureLink, Mailbox, Sender, SessionLocal
from app.exposures import broker_domain_sources, discover_candidates, normalize_domain


def test_normalize_domain_rejects_urls_and_email_addresses():
    assert normalize_domain(" News.Example.com. ") == "news.example.com"
    for value in ("https://example.com", "person@example.com", "bad..example.com", "-bad.example.com"):
        try:
            normalize_domain(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"domain should be rejected: {value}")


def test_exact_domain_match_is_only_a_proposal(client):
    with SessionLocal() as db:
        broker = Broker(
            slug="exposure-acme-example", name="Acme Data Group",
            email="privacy@acme-exposure.example", web="https://www.acme-exposure.example/",
            countries=["fr"], categories=["ads"], enabled=True,
        )
        mailbox = Mailbox(label="Exposure test", address="person@example.test", host="imap.example.test",
                          username="person@example.test", secret_enc="", folders=[], state={})
        db.add_all([broker, mailbox])
        db.flush()
        sender = Sender(mailbox_id=mailbox.id, address="news@acme-exposure.example",
                        domain="acme-exposure.example", name="Acme newsletter", count=3, status="new")
        db.add(sender)
        db.commit()
        broker_id = broker.id

        assert broker_domain_sources(broker)["acme-exposure.example"] == "contact_domain"
        assert discover_candidates(db) == 1
        link = db.query(ExposureLink).filter_by(sender_domain="acme-exposure.example", broker_id=broker_id).one()
        assert link.status == "proposed"
        assert link.confidence == 0.95
        assert link.source == "automatic"

        link.status = "rejected"
        db.commit()
        assert discover_candidates(db) == 0
        assert link.status == "rejected"

        db.delete(link)
        db.delete(sender)
        db.delete(mailbox)
        db.delete(broker)
        db.commit()


def test_exposure_page_and_review_require_auth(client):
    with SessionLocal() as db:
        broker = Broker(slug="exposure-review-example", name="Review Example",
                        email="privacy@review-exposure.example", countries=["fr"], categories=["ads"])
        mailbox = Mailbox(label="Review box", address="review@example.test", host="imap.example.test",
                          username="review@example.test", secret_enc="", folders=[], state={})
        db.add_all([broker, mailbox])
        db.flush()
        sender = Sender(mailbox_id=mailbox.id, address="news@review-exposure.example",
                        domain="review-exposure.example", name="Review newsletter", count=1, status="new")
        db.add(sender)
        db.commit()
        broker_id, mailbox_id, sender_id = broker.id, mailbox.id, sender.id

    response = client.get("/exposures")
    assert response.status_code == 200
    assert "Cartographie des expositions" in response.text
    with SessionLocal() as db:
        link = db.query(ExposureLink).filter_by(sender_domain="review-exposure.example", broker_id=broker_id).one()
        link_id = link.id
    response = client.post(f"/exposures/{link_id}/review", data={
        "decision": "confirmed", "group_name": "Groupe Review", "note": "Vérifié sur les mentions légales"
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        link = db.get(ExposureLink, link_id)
        assert link.status == "confirmed"
        assert link.group_name == "Groupe Review"
        assert link.review_note == "Vérifié sur les mentions légales"
        db.delete(link)
        db.delete(db.get(Sender, sender_id))
        db.delete(db.get(Mailbox, mailbox_id))
        db.delete(db.get(Broker, broker_id))
        db.commit()

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anonymous:
        response = anonymous.get("/exposures", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
