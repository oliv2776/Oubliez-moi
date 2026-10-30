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
