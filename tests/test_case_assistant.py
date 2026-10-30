from zipfile import ZipFile
from io import BytesIO

from app.case_assistant import build_draft
from app.case_management import Organization, PrivacyCase
from app.db import Profile, SessionLocal


def _case():
    with SessionLocal() as db:
        org = Organization(name="Organisme assistant", contact_email="privacy@example.test")
        profile = Profile(full_name="Alex Exemple", emails=["alex@example.test"], phones=[], addresses=[], other_names=[])
        db.add_all([org, profile])
        db.flush()
        case = PrivacyCase(organization=org, profile=profile, request_type="access",
                           subject="Copie des données", reference="RGPD-ABC")
        db.add(case)
        db.commit()
        return case.id


def test_drafts_are_personalized_but_not_sent(client):
    case_id = _case()
    with SessionLocal() as db:
        case = db.get(PrivacyCase, case_id)
        draft = build_draft(case, "initial")
        assert "privacy@example.test" in draft
        assert "Alex Exemple" in draft
        assert "droit d’accès" in draft
    response = client.post(f"/privacy-cases/{case_id}/assistant/draft",
                           data={"kind": "followup", "draft_text": "Texte relu"},
                           follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        case = db.get(PrivacyCase, case_id)
        assert any(e.kind == "assistant_draft" and "Texte relu" in (e.body or "") for e in case.events)


def test_reply_analysis_stores_only_classification_not_full_reply(client):
    case_id = _case()
    raw = "Nous avons supprimé vos données personnelles de notre base marketing."
    response = client.post(f"/privacy-cases/{case_id}/assistant/analyze",
                           data={"subject": "Votre demande", "reply_text": raw})
    assert response.status_code == 200
    assert "données supprimées" in response.text
    with SessionLocal() as db:
        case = db.get(PrivacyCase, case_id)
        analysis = next(e for e in case.events if e.kind == "assistant_analysis")
        assert raw not in (analysis.body or "")
        assert "Action suggérée" in analysis.body


def test_case_export_zip_contains_manifest_and_timeline(client):
    case_id = _case()
    client.post(f"/privacy-cases/{case_id}/note",
                data={"summary": "Preuve reçue", "body": "Référence externe : archive-123"})
    response = client.get(f"/privacy-cases/{case_id}/export.zip")
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        assert {"manifest.json", "chronologie.json", "dossier.md", "LISEZ-MOI.txt"} <= set(archive.namelist())
        assert "archive-123" in archive.read("dossier.md").decode()


def test_assistant_requires_auth(client):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anonymous:
        response = anonymous.get("/privacy-cases/1/assistant", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
