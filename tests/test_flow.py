from datetime import timedelta
from email.message import EmailMessage

from sqlalchemy import select

from app import letters, mailer, service
from app.config import settings
from app.db import COMPLETED, NEEDS_ACTION, OVERDUE, QUEUED, SENT, Broker, Profile, Request, SessionLocal, utcnow


def test_seed_loaded(client):
    with SessionLocal() as db:
        n = db.scalar(select(Broker.id).limit(1))
        enabled = db.scalars(select(Broker).where(Broker.enabled.is_(True))).all()
    assert n and len(enabled) > 100
    assert all(set(b.countries) & {"fr", "all"} for b in enabled)


def test_pages_render(client):
    for url in ["/", "/profiles", "/profiles/new", "/requests", "/tasks", "/brokers", "/brokers?state=webform", "/brokers/new", "/settings"]:
        r = client.get(url)
        assert r.status_code == 200, url


def test_auth_required():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anon:
        r = anon.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert anon.get("/healthz").text == "ok"


def test_full_flow(client):
    r = client.post("/profiles", data={
        "full_name": "Camille Martin", "emails": "camille@exemple.fr\nc.martin@autre.fr",
        "phones": "06 12 34 56 78", "addresses": "1 rue de la Paix\n75002 Paris\n\n3 avenue X\n69001 Lyon",
        "language": "auto",
    }, follow_redirects=False)
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        p = db.get(Profile, pid)
        assert p.addresses == ["1 rue de la Paix 75002 Paris", "3 avenue X 69001 Lyon"]

    assert client.get(f"/profiles/{pid}").status_code == 200
    prev = client.get(f"/profiles/{pid}/preview?lang=fr")
    assert "article 17" in prev.text and "Camille Martin" in prev.text

    client.post(f"/profiles/{pid}/campaign")
    with SessionLocal() as db:
        reqs = db.scalars(select(Request).where(Request.profile_id == pid)).all()
        enabled = db.scalars(select(Broker).where(Broker.enabled.is_(True))).all()
        assert len(reqs) == len(enabled)
        assert {r.status for r in reqs} <= {QUEUED, NEEDS_ACTION}

    # Envoi (simulation) de 3 demandes
    for _ in range(3):
        assert service.send_tick().startswith("OK")
    with SessionLocal() as db:
        sent = db.scalars(select(Request).where(Request.status == SENT).order_by(Request.id)).all()
        assert len(sent) == 3
        target = sent[0]
        ref, broker_email = target.reference, target.broker.email

    # Une réponse citant la référence -> terminée
    m = EmailMessage()
    m["From"] = broker_email
    auth_domain = broker_email.rsplit("@", 1)[-1]
    m["Authentication-Results"] = f"mx.test; dkim=pass header.d={auth_domain}; dmarc=pass header.from={auth_domain}"
    m["Subject"] = f"RE: Demande d'effacement – Réf. {ref}"
    m["Message-ID"] = "<reply1@broker>"
    m.set_content("Bonjour, vos données ont été supprimées. Cordialement")
    with SessionLocal() as db:
        req = service.process_incoming(db, mailer.parse_mail(bytes(m)))
        assert req.reference == ref and req.status == COMPLETED

    # Une réponse sans référence mais en In-Reply-To -> action requise
    with SessionLocal() as db:
        second = db.scalars(select(Request).where(Request.status == SENT).order_by(Request.id)).first()
        m2 = EmailMessage()
        m2["From"] = "support@" + second.broker.email.split("@")[1]
        auth_domain = second.broker.email.rsplit("@", 1)[-1]
        m2["Authentication-Results"] = f"mx.test; spf=pass smtp.mailfrom=support@{auth_domain}; dmarc=pass header.from={auth_domain}"
        m2["Subject"] = "Your request"
        m2["In-Reply-To"] = second.message_ids[0]
        m2["Message-ID"] = "<reply2@broker>"
        m2.set_content("Please verify your identity before we can proceed.")
        req2 = service.process_incoming(db, mailer.parse_mail(bytes(m2)))
        assert req2.id == second.id and req2.status == NEEDS_ACTION

    # Nos propres messages sont ignorés
    with SessionLocal() as db:
        m3 = EmailMessage()
        m3["From"] = "camille@exemple.fr"
        m3["Subject"] = f"Fwd: {ref}"
        m3.set_content("x")
        assert service.process_incoming(db, mailer.parse_mail(bytes(m3))) is None

    # Échéance dépassée -> relance, puis « sans réponse »
    with SessionLocal() as db:
        third = db.scalars(select(Request).where(Request.status == SENT).order_by(Request.id)).first()
        third.due_at = utcnow() - timedelta(days=1)
        db.query(Request).filter(Request.status == QUEUED).update({"status": "cancelled"})
        db.commit()
        tid = third.id
    assert "relance" in service.send_tick()
    with SessionLocal() as db:
        t = db.get(Request, tid)
        assert t.reminder_count == 1 and t.status == SENT
        t.due_at = utcnow() - timedelta(days=1)
        db.commit()
    assert service.check_deadlines() == 0  # pas encore : la mise en demeure doit partir d'abord
    assert "mise en demeure" in service.send_tick()
    with SessionLocal() as db:
        t = db.get(Request, tid)
        assert t.reminder_count == 2 and any(e.kind == "formal_notice" and ("Mise en demeure" in e.body or "Formal notice" in e.body) for e in t.events)
        t.due_at = utcnow() - timedelta(days=1)
        db.commit()
    assert service.check_deadlines() >= 1
    with SessionLocal() as db:
        assert db.get(Request, tid).status == OVERDUE
    page = client.get(f"/requests/{tid}")
    assert "Saisir la CNIL" in page.text

    # Renvoi périodique d'une demande terminée ancienne
    with SessionLocal() as db:
        done = db.scalars(select(Request).where(Request.reference == ref)).one()
        done.closed_at = utcnow() - timedelta(days=settings.resend_interval_days + 1)
        db.commit()
    assert service.schedule_resends() == 1
    assert service.schedule_resends() == 0  # le même cycle ne doit pas être créé deux fois
    with SessionLocal() as db:
        again = db.scalars(select(Request).where(Request.broker_id == done.broker_id, Request.profile_id == pid, Request.cycle == 2)).one()
        assert again.status == QUEUED

    for url in ["/", f"/profiles/{pid}", "/requests", "/requests?status=sent", "/tasks", "/export.csv", f"/requests/{tid}"]:
        assert client.get(url).status_code == 200, url


def test_letters_en():
    p = Profile(full_name="Jane Doe", emails=["jane@x.com"], phones=[], addresses=[], other_names=[], language="auto")
    b = Broker(name="Acme Data", email="privacy@acme.com", countries=["all"])
    assert letters.letter_language(p, b) == "en"
    subject, body = letters.erasure_letter(p, b, "OUBLI-AAAAAA", "en")
    assert "Article 17" in subject and "Acme Data" in body and "jane@x.com" in body


def test_reference_from_unrelated_domain_is_attached_but_not_trusted(client):
    """La référence identifie la demande, mais un domaine tiers ne peut pas la clôturer."""
    with SessionLocal() as db:
        broker = Broker(slug="custom-trusted", name="Trusted Broker", email="privacy@trusted.example",
                        web="https://trusted.example", countries=["fr"], categories=["custom"], source="custom")
        profile = Profile(full_name="Test User", emails=["user@example.net"], phones=[], addresses=[], other_names=[])
        db.add_all([broker, profile])
        db.flush()
        req = Request(profile_id=profile.id, broker_id=broker.id, reference="OUBLI-ABC123", status=SENT,
                      sent_at=utcnow(), message_ids=["<original@trusted.example>"])
        db.add(req)
        db.commit()
        raw = EmailMessage()
        raw["From"] = "attacker@evil.example"
        raw["Authentication-Results"] = "mx.test; spf=pass; dkim=pass; dmarc=pass"
        raw["Subject"] = "Re: OUBLI-ABC123"
        raw["Message-ID"] = "<forged@evil.example>"
        raw.set_content("Your data has been deleted.")

        matched = service.process_incoming(db, mailer.parse_mail(bytes(raw)))

        assert matched is not None and matched.id == req.id
        assert db.get(Request, req.id).status != COMPLETED
        assert any(e.kind == "warning" and "Réponse non vérifiée" in e.summary and "DMARC" in e.summary for e in req.events)


def test_automatic_resends_stop_at_configured_cycle_limit(client):
    from app.config import settings
    with SessionLocal() as db:
        broker = Broker(slug="resend-cap", name="Resend Cap", email="privacy@resend.example",
                        countries=["fr"], categories=["custom"], source="custom")
        profile = Profile(full_name="Cycle User", emails=["cycle@example.net"], phones=[], addresses=[], other_names=[])
        db.add_all([broker, profile])
        db.flush()
        req = Request(profile_id=profile.id, broker_id=broker.id, reference="OUBLI-CAP123", status=COMPLETED,
                      cycle=settings.max_automatic_resend_cycles, closed_at=utcnow() - timedelta(days=settings.resend_interval_days + 1))
        db.add(req)
        db.commit()
    assert service.schedule_resends() == 0


def test_deleting_custom_broker_archives_and_preserves_history(client):
    with SessionLocal() as db:
        broker = Broker(slug="custom-archive-test", name="Archive Test", email="privacy@archive.example",
                        countries=["fr"], categories=["custom"], source="custom", enabled=True)
        profile = Profile(full_name="Archive User", emails=["user@example.net"], phones=[], addresses=[], other_names=[])
        db.add_all([broker, profile])
        db.flush()
        req = Request(profile_id=profile.id, broker_id=broker.id, reference="OUBLI-ARC123", status=SENT)
        db.add(req)
        db.commit()
        broker_id, request_id = broker.id, req.id

    response = client.post(f"/brokers/{broker_id}/delete", follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        broker = db.get(Broker, broker_id)
        req = db.get(Request, request_id)
        assert broker is not None and broker.enabled is False
        assert req is not None and req.broker_id == broker_id
