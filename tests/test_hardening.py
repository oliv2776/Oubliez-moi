import csv
import io

from app.db import Broker, Profile, Request, SessionLocal


def test_security_headers_and_private_pages_are_not_cached(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"

    login = client.get("/login")
    assert login.headers["Cache-Control"] == "no-store"


def test_csv_export_neutralizes_spreadsheet_formulas(client):
    with SessionLocal() as db:
        profile = Profile(full_name="  =HYPERLINK(\"https://evil.example\")",
                          emails=["person@example.test"], phones=[], addresses=[], other_names=[])
        broker = Broker(slug="csv-formula-test", name="@SUM(1+1)", email="+cmd|' /C calc'!A0",
                        countries=["fr"], categories=["custom"], source="custom")
        db.add_all([profile, broker])
        db.flush()
        db.add(Request(profile_id=profile.id, broker_id=broker.id, reference="-2+3", status="draft"))
        db.commit()

    response = client.get("/export.csv")
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.text.lstrip("\ufeff")), delimiter=";"))
    row = next(row for row in rows[1:] if row[0] == "'-2+3")
    assert row[1].startswith("'  =HYPERLINK")
    assert row[2] == "'@SUM(1+1)"
    assert row[3].startswith("'+cmd")
    assert response.headers["Cache-Control"] == "no-store"
