from datetime import timedelta

from sqlalchemy import select

from app import service
from app.db import COMPLETED, SENT, Broker, Profile, Request, SessionLocal, utcnow


def _profile_with_sent_requests(n=3):
    with SessionLocal() as db:
        p = Profile(full_name="Supp Lément", emails=["ancien@exemple.fr"], phones=["06 11 22 33 44"], addresses=[], other_names=[])
        db.add(p)
        db.commit()
        brokers = db.scalars(select(Broker).where(Broker.enabled.is_(True), Broker.email.is_not(None)).limit(n)).all()
        ids = []
        for b in brokers:
            r = service._create_request(db, p, b, cycle=1)
            db.commit()
            service.send_initial(db, r)
            ids.append(r.id)
        return p.id, ids


def test_nothing_missing_right_after_sending(client):
    pid, _ = _profile_with_sent_requests()
    with SessionLocal() as db:
        assert service.supplement_candidates(db, db.get(Profile, pid)) == []


def test_new_email_triggers_supplement(client):
    pid, ids = _profile_with_sent_requests()
    with SessionLocal() as db:
        done = db.get(Request, ids[0])
        service.set_status(db, done, COMPLETED)
        done.closed_at = utcnow() - timedelta(days=5)
        db.commit()

    # Ajout d'une adresse via le formulaire ; le téléphone est ressaisi sous une autre forme (pas un nouveau numéro)
    r = client.post(f"/profiles/{pid}", data={"full_name": "Supp Lément", "emails": "ancien@exemple.fr\nnouvelle@exemple.fr",
                                              "phones": "+33 6 11 22 33 44"}, follow_redirects=True)
    assert "pas reçu les nouvelles informations" in r.text
    assert "Nouvelles informations à transmettre" in r.text and "nouvelle@exemple.fr" in r.text

    with SessionLocal() as db:
        cands = service.supplement_candidates(db, db.get(Profile, pid))
        assert len(cands) == 3
        assert all(m == [("emails", "nouvelle@exemple.fr")] for _, m in cands)

    client.post(f"/profiles/{pid}/supplement")
    # Les premiers envois en file passent avant les compléments : on vide la file d'abord.
    results = [service.send_tick() for _ in range(60)]
    assert sum("complément" in (r or "") for r in results) == 3
    with SessionLocal() as db:
        reqs = [db.get(Request, i) for i in ids]
        assert not any(r.supplement_pending for r in reqs)
        assert reqs[0].status == SENT and reqs[0].closed_at is None  # la demande terminée est rouverte
        body = [e for e in reqs[1].events if e.kind == "supplement"][0].body
        assert "nouvelle@exemple.fr" in body and reqs[1].reference in body
        assert service.supplement_candidates(db, db.get(Profile, pid)) == []
