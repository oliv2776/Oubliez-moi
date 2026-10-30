from datetime import datetime, timezone

from app.case_management import Organization, PrivacyCase, add_months
from app.db import Profile, SessionLocal


def test_add_months_handles_calendar_end_of_month():
    assert add_months(datetime(2026, 1, 31), 1) == datetime(2026, 2, 28)
    assert add_months(datetime(2026, 11, 30), 2) == datetime(2027, 1, 30)


def test_create_and_track_rgpd_case(client):
    with SessionLocal() as db:
        org = Organization(name="Organisme dossier test", contact_email="privacy@example.test")
        profile = Profile(full_name="Profil dossier test", emails=["person@example.test"], phones=[], addresses=[], other_names=[])
        db.add_all([org, profile])
        db.commit()
        org_id, profile_id = org.id, profile.id

    response = client.post("/privacy-cases", data={
        "organization_id": str(org_id), "request_type": "access", "subject": "Copie des données",
        "profile_id": str(profile_id), "reference": "RGPD-123",
    }, follow_redirects=False)
    assert response.status_code == 303
    case_id = int(response.headers["location"].rsplit("/", 1)[1])
    assert client.get(f"/privacy-cases/{case_id}").status_code == 200

    response = client.post(f"/privacy-cases/{case_id}/status",
                           data={"status": "sent", "note": "Courrier recommandé"}, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        case = db.get(PrivacyCase, case_id)
        assert case.status == "sent"
        assert case.sent_at is not None
        assert case.deadline_at == add_months(case.sent_at, 1)
        assert any(event.kind == "sent" for event in case.events)

    assert client.post(f"/privacy-cases/{case_id}/note",
                       data={"summary": "Accusé de réception", "body": "Référence du recommandé : XX"},
                       follow_redirects=False).status_code == 303
    assert "Accusé de réception" in client.get(f"/privacy-cases/{case_id}").text

    with SessionLocal() as db:
        case = db.get(PrivacyCase, case_id)
        case.sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
        case.deadline_at = add_months(case.sent_at, 1)
        db.commit()
    assert client.post(f"/privacy-cases/{case_id}/extend", data={"note": "Dossier complexe"},
                       follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        case = db.get(PrivacyCase, case_id)
        assert case.extended_deadline_at == add_months(case.deadline_at, 2)
        assert case.extension_notified_at is not None


def test_case_requires_valid_type_and_auth(client):
    with SessionLocal() as db:
        org = Organization(name="Validation organisme")
        db.add(org)
        db.commit()
        org_id = org.id
    response = client.post("/privacy-cases", data={"organization_id": str(org_id), "request_type": "invalid"})
    assert response.status_code == 400

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anonymous:
        response = anonymous.get("/privacy-cases", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def _late_case(name: str, days_late: int = 3, status: str = "sent") -> int:
    from datetime import timedelta
    from app.db import utcnow
    with SessionLocal() as db:
        org = Organization(name=name, contact_email="dpo@late.example")
        sent = utcnow() - timedelta(days=40 + days_late)
        case = PrivacyCase(organization=org, request_type="access", status=status, subject="Copie",
                           sent_at=sent, deadline_at=utcnow() - timedelta(days=days_late))
        db.add(case)
        db.commit()
        return case.id


def _cleanup_cases():
    from app.case_management import CaseEvent
    with SessionLocal() as db:
        db.query(CaseEvent).delete()
        db.query(PrivacyCase).delete()
        db.query(Organization).delete()
        db.commit()


def test_overdue_case_shown_counted_and_notified_once(client, monkeypatch):
    from datetime import timedelta
    from app import notify, service
    from app.case_management import cases_due
    from app.config import settings
    _cleanup_cases()
    late_id = _late_case("Organisme en retard")
    _late_case("Organisme clos", status="completed")  # clos : jamais signalé
    with SessionLocal() as db:
        soon = PrivacyCase(organization=Organization(name="Organisme bientôt"), request_type="erasure", status="sent",
                           deadline_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=3))
        db.add(soon)
        db.commit()
        late, upcoming = cases_due(db)
        assert [c.id for c in late] == [late_id] and [c.organization.name for c in upcoming] == ["Organisme bientôt"]

    pushes = []
    monkeypatch.setattr(settings, "notify_ntfy_url", "http://ntfy.local/oubliez")
    monkeypatch.setattr(notify, "push", lambda title, message, **kw: pushes.append((title, message)) or [])
    service.check_deadlines()
    service.check_deadlines()  # une seule alerte par échéance
    assert len(pushes) == 1 and "1 dossier(s) RGPD en retard" in pushes[0][0] and "Organisme en retard" in pushes[0][1]
    with SessionLocal() as db:
        case = db.get(PrivacyCase, late_id)
        assert case.overdue_notified_at is not None
        assert [e.kind for e in case.events].count("deadline") == 1
        # Alerte envoyée il y a un mois, puis prolongation dont l'échéance vient aussi de passer : nouvelle alerte
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        case.overdue_notified_at = now - timedelta(days=30)
        case.extended_deadline_at = now - timedelta(days=1)
        db.commit()
    service.check_deadlines()
    assert len(pushes) == 2

    with SessionLocal() as db:  # le tableau de bord complet s'affiche dès qu'un profil existe
        db.add(Profile(full_name="Profil tableau de bord", emails=["p@example.test"], phones=[], addresses=[], other_names=[]))
        db.commit()
    tasks = client.get("/tasks").text
    assert "Organisme en retard" in tasks and "Organisme bientôt" in tasks and "Organisme clos" not in tasks
    assert "Organisme en retard" in client.get("/").text
    # Le compteur du menu inclut le dossier en retard, sur toutes les pages (routeurs secondaires compris)
    for page in ("/", "/privacy-cases", "/search?q=xx", "/exposures"):
        assert '<span class="pill">1</span>' in client.get(page).text, page
    _cleanup_cases()


def test_digest_lists_overdue_cases(client, monkeypatch):
    from app import mailer, service
    from app.config import settings
    _cleanup_cases()
    _late_case("Organisme du récapitulatif")
    sent = []
    monkeypatch.setattr(settings, "notify_email_digest", True)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(mailer, "send_message", lambda msg, force=False: sent.append(msg))
    with SessionLocal() as db:
        from app.db import KV
        db.query(KV).filter(KV.key == "digest_sent_on").delete()
        db.commit()
    assert service.send_digest() == "récapitulatif envoyé (1 éléments)"
    body = sent[0].get_content()
    assert "1 dossier(s) RGPD ont dépassé leur échéance" in body and "Organisme du récapitulatif" in body
    _cleanup_cases()
