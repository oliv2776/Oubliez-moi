from app.db import Broker, Mailbox, Profile, Request, Sender, SessionLocal


def test_global_search_finds_profiles_brokers_requests_and_senders(client):
    with SessionLocal() as db:
        profile = Profile(full_name="Search Example Alice", emails=["alice@example.fr"])
        broker = Broker(slug="privacy-search-example", name="Privacy Search Example",
                        email="privacy@search-example.test", countries=["fr"], categories=[])
        mailbox = Mailbox(label="Boîte test", address="alice@example.fr", host="imap.example.fr",
                          username="alice@example.fr", secret_enc="", folders=[], state={})
        db.add_all([profile, broker, mailbox])
        db.flush()
        req = Request(profile_id=profile.id, broker_id=broker.id, reference="OUBLI-SEARCH1")
        sender = Sender(mailbox_id=mailbox.id, address="news@search-example.test",
                        domain="search-example.test", name="Search Example News", count=4,
                        status="new")
        db.add_all([req, sender])
        db.commit()
        broker_id, request_id, sender_id = broker.id, req.id, sender.id

    response = client.get("/search?q=search-example")
    assert response.status_code == 200
    assert "Privacy Search Example" in response.text
    assert "Search Example News" in response.text
    assert f"/requests/{request_id}" in response.text
    assert 'href="/brokers?q=' in response.text
    assert "/mailboxes?status=all" in response.text
    with SessionLocal() as db:
        db.delete(db.get(Sender, sender_id))
        mailbox_row = db.get(Mailbox, mailbox.id)
        if mailbox_row:
            db.delete(mailbox_row)
        db.delete(db.get(Broker, broker_id))
        db.commit()


def test_global_search_requires_two_characters(client):
    response = client.get("/search?q=x")
    assert response.status_code == 200
    assert "au moins deux caractères" in response.text


def test_global_search_is_authenticated():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anonymous:
        response = anonymous.get("/search", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_global_search_treats_sql_wildcards_literally(client):
    with SessionLocal() as db:
        db.add_all([Profile(full_name="Jean_Dupont", emails=["j@example.fr"]), Profile(full_name="Jeanne Martin", emails=["m@example.fr"])])
        db.commit()
    text = client.get("/search?q=n_").text  # non échappé, « _ » accepterait « Jeanne »
    assert "Jean_Dupont" in text and "Jeanne Martin" not in text
    text = client.get("/search?q=%25%25").text  # « %% » ne doit pas tout renvoyer
    assert "Jean_Dupont" not in text and "Jeanne Martin" not in text
