"""Courtiers qui réclament un identifiant publicitaire : marche à suivre commune et clôture groupée."""
import html

from app import reanalysis
from app.db import Broker, Event, Profile, Request, SessionLocal

TEADS = ("De : Teads <do-not-reply@teads-dev.example>\nDate : hier\nObjet : Response to your Data Subject Request\n\n"
         "Hello,\nWe do not hold traditional information such as your name or email address, therefore we are unable to "
         "exercise privacy rights on your behalf without further information such as your online identifier. In the "
         "absence of such identifiers, we are unable to action your request.\nPlease click on the button "
         "https://www.teads-dev.example/privacy/opt-out to opt out.\nFollow us https://www.linkedin.com/company/teads")
OTHER = "De : DPO <dpo@autre-dev.example>\nDate : hier\nObjet : Re\n\nPlease use our online form to submit your request."


def _req(slug, body, status="needs_action", summary=None):
    with SessionLocal() as db:
        b = Broker(slug=slug, name=f"Courtier {slug}", email=f"dpo@{slug}.example", web=f"https://www.{slug}.example",
                   countries=["fr"], categories=[])
        p = Profile(full_name="Dev Id", emails=["moi@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add_all([b, p])
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference=f"OUBLI-{slug.upper().replace('-', '')[-6:]}", status=status)
        db.add(r)
        db.flush()
        db.add(Event(request_id=r.id, kind="reply", summary="Réponse reçue", body=body))
        if summary:
            db.add(Event(request_id=r.id, kind="status", summary=summary))
        db.commit()
        return r.id


def test_page_lists_brokers_with_their_opt_out_links(client):
    rid = _req("teads-dev", TEADS, summary="Réanalyse : identifiant publicitaire demandé (« x »)")
    other = _req("autre-dev", OTHER)
    page = html.unescape(client.get("/tasks/device-ids").text)
    assert "Courtier teads-dev" in page and "Courtier autre-dev" not in page
    assert "https://www.teads-dev.example/privacy/opt-out" in page and "linkedin" not in page
    assert f'value="{rid}"' in page and f'value="{other}"' not in page
    # Bandeau dans À traiter et lien depuis « En bref »
    tasks = html.unescape(client.get(f"/tasks?id={rid}").text)
    assert "1 courtier(s)" in tasks and "Marche à suivre commune" in tasks


def test_current_rules_also_detect_it(client):
    """Classée autrement à l'époque (pays de résidence…), mais les règles actuelles y voient un identifiant."""
    _req("quant-dev", TEADS.replace("teads-dev", "quant-dev"), summary="Réanalyse : question sur le pays de résidence")
    assert "Courtier quant-dev" in client.get("/tasks/device-ids").text


def test_bulk_close_is_a_manual_decision(client):
    a = _req("devid-a", TEADS)
    b = _req("devid-b", TEADS)
    other = _req("devid-c", OTHER)
    r = client.post("/tasks/device-ids/close", data={"ids": [a, other]}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        ra, rb, rc = db.get(Request, a), db.get(Request, b), db.get(Request, other)
        assert ra.status == "completed" and ra.closed_at is not None
        assert rb.status == "needs_action"      # non cochée
        assert rc.status == "needs_action"      # pas concernée : ignorée même si envoyée
        last = [e.summary for e in ra.events if e.kind == "status"][-1]
        assert last.startswith("Statut changé manuellement : Terminée") and "identifiants publicitaires" in last
        assert not reanalysis.decided_automatically(ra)  # la réanalyse ne la rouvrira pas
