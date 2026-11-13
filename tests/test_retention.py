"""Conservation limitée : le contenu des messages des demandes closes est effacé après le délai choisi."""
from datetime import timedelta

from app import service
from app.config import settings
from app.db import Broker, Event, Profile, Request, SessionLocal, utcnow


def _request(slug, status, closed_days_ago):
    with SessionLocal() as db:
        b = Broker(slug=slug, name=slug, email=f"dpo@{slug}.example", countries=["fr"], categories=[])
        p = Profile(full_name="Ret Test", emails=["moi@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add_all([b, p])
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference=f"OUBLI-{slug.upper()[-6:]}", status=status,
                    closed_at=utcnow() - timedelta(days=closed_days_ago))
        db.add(r)
        db.flush()
        db.add(Event(request_id=r.id, kind="sent", summary="Demande envoyée", body="Mon nom, mon adresse"))
        db.add(Event(request_id=r.id, kind="reply", summary="Réponse reçue", body="Dear Mr X", html="<p>Dear</p>"))
        db.commit()
        return r.id


def _bodies(rid):
    with SessionLocal() as db:
        return [(e.kind, e.body, e.html, e.category) for e in db.get(Request, rid).events]


def test_purge_only_old_closed_requests(client, monkeypatch):
    old, recent = _request("ret001", "completed", 400), _request("ret002", "completed", 100)
    refused, cnil = _request("ret003", "refused", 400), _request("ret004", "cnil", 400)
    monkeypatch.setattr(settings, "retention_months", 0)
    assert service.purge_old_messages() == 0 and _bodies(old)[0][1]  # désactivé par défaut
    monkeypatch.setattr(settings, "retention_months", 12)
    assert service.purge_old_messages() == 1
    events = _bodies(old)
    assert all(body is None and html is None for _, body, html, _ in events)
    assert [k for k, *_ in events][:2] == ["sent", "reply"] and events[-1][3] == "purged"  # l'historique reste
    for rid in (recent, refused, cnil):
        assert _bodies(rid)[0][1] == "Mon nom, mon adresse"
    assert service.purge_old_messages() == 0  # rien à refaire
    assert client.get(f"/requests/{old}").status_code == 200 and client.get("/tasks").status_code == 200


def test_setting_is_saved_and_bounded(client):
    assert 'name="retention_months"' in client.get("/settings").text
