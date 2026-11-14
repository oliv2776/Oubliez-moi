"""Brouillons de réponse par l'IA, comparateur de modèles, dossier « démarchage téléphonique »."""
import html
import time

import pytest

from app import llm, mailer, model_compare, service
from app.config import settings
from app.db import NEEDS_ACTION, SENT, Broker, Event, Profile, Request, SessionLocal, utcnow

QUESTION = ("De : Privacy <privacy@{slug}.example>\nDate : hier\nObjet : Re: Request for erasure\n\nHello,\nBefore we can go "
            "further, we would like to understand the context of your request. Kind regards.")


def _request(slug, text=QUESTION, status=NEEDS_ACTION):
    with SessionLocal() as db:
        b = Broker(slug=slug, name=f"Courtier {slug}", email=f"privacy@{slug}.example", countries=["fr"], categories=[])
        p = Profile(full_name="Brouillon Test", emails=["moi@exemple.fr"], phones=["06 00 00 00 01"],
                    addresses=["1 rue Secrète, 75000 Paris"], other_names=[], birth_date="1990-01-02")
        db.add_all([b, p])
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference=f"OUBLI-{slug.upper()[-6:]}", status=status,
                    sent_at=utcnow(), last_reply_at=utcnow())
        db.add(r)
        db.flush()
        db.add(Event(request_id=r.id, kind="reply", summary="Réponse reçue", body=text.format(slug=slug), message_id=f"<q@{slug}.example>"))
        db.commit()
        return r.id


@pytest.fixture
def chat_model(monkeypatch):
    """IA conversationnelle simulée : note ce qu'on lui envoie, répond avec un brouillon."""
    calls = []

    def fake_post(path, payload, timeout):
        calls.append(payload)
        return {"message": {"content": "<think>hmm</think>Dear Sir or Madam,\n\nI confirm my erasure request ref. OUBLI-X.\n\nBrouillon Test"}}
    monkeypatch.setattr(llm, "_post", fake_post)
    monkeypatch.setattr(settings, "llm_url", "http://ollama.test")
    monkeypatch.setattr(settings, "llm_mode", "chat")
    monkeypatch.setattr(settings, "llm_model", "qwen3:8b")
    return calls


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_message", lambda msg, force=False: sent.append(msg))
    return sent


# ------------------------------------------------------------ brouillons ---

def test_draft_is_written_shown_and_never_sent_by_itself(client, chat_model, outbox):
    rid = _request("drft01")
    page = html.unescape(client.get(f"/tasks?id={rid}").text)
    assert "Rédiger un brouillon de réponse avec l'IA" in page and 'name="body"' not in page
    client.post(f"/requests/{rid}/draft", data={"next": "/tasks"})
    assert not outbox                                        # rédiger n'envoie rien
    page = html.unescape(client.get(f"/tasks?id={rid}").text)
    assert "I confirm my erasure request" in page and "<think>" not in page and "Relisez chaque phrase" in page
    # Le modèle ne reçoit que ce que la demande contenait déjà : ni téléphone, ni adresse postale, ni date de naissance.
    prompt = "\n".join(m["content"] for m in chat_model[-1]["messages"])
    assert "Brouillon Test" in prompt and "moi@exemple.fr" in prompt and "understand the context" in prompt
    for secret in ("06 00 00 00 01", "rue Secrète", "1990"):
        assert secret not in prompt
    with SessionLocal() as db:
        r = db.get(Request, rid)
        assert r.status == NEEDS_ACTION and r.events[-1].kind == "draft" and r.events[-1].actor == "ai"


def test_reviewed_text_is_sent_to_the_broker_only(client, chat_model, outbox):
    rid = _request("drft02")
    client.post(f"/requests/{rid}/draft", data={"next": "/tasks"})
    client.post(f"/requests/{rid}/send-draft", data={"body": "Texte que j'ai corrigé moi-même avant l'envoi.", "next": "/tasks"})
    (msg,) = outbox
    assert msg["To"] == "privacy@drft02.example" and msg["In-Reply-To"] == "<q@drft02.example>"
    assert "corrigé moi-même" in msg.get_content() and msg["Subject"].startswith("Re:")
    with SessionLocal() as db:
        r = db.get(Request, rid)
        assert r.status == SENT
        sent = [e for e in r.events if e.kind == "manual_reply"][-1]
        assert (sent.actor, sent.category) == ("manual", "manual_reply")
        assert service.current_draft(r) is None              # parti : plus proposé
    client.post(f"/requests/{rid}/send-draft", data={"body": "trop court"})
    assert len(outbox) == 1


def test_no_draft_with_a_decision_model_or_when_a_template_reply_exists(client, chat_model, monkeypatch):
    rid = _request("drft03")
    monkeypatch.setattr(settings, "llm_mode", "decision")
    assert "Rédiger un brouillon" not in html.unescape(client.get(f"/tasks?id={rid}").text)
    with SessionLocal() as db, pytest.raises(ValueError, match="ne rédige pas"):
        service.make_draft(db, db.get(Request, rid))
    monkeypatch.setattr(settings, "llm_mode", "chat")
    country = _request("drft04", "De : <privacy@{slug}.example>\nObjet : Re\n\nWhich country did you sign up to?")
    page = html.unescape(client.get(f"/tasks?id={country}").text)
    assert "Réponse type prête" in page and "Rédiger un brouillon" not in page


# ----------------------------------------------------------- comparateur ---

def test_compare_models_on_recognised_replies(client, monkeypatch):
    _request("cmp001", "De : <a@{slug}.example>\nObjet : Re\n\nWe do not hold any personal data about you.", status="completed")
    _request("cmp002", "De : <a@{slug}.example>\nObjet : Re\n\nPlease use our online form to submit your request.")
    _request("cmp003", "De : <a@{slug}.example>\nObjet : Re\n\nWe have received your request, ticket number 12.", status="sent")
    _request("cmp004", "De : <a@{slug}.example>\nObjet : Re\n\nPlease send us a copy of your ID card.")
    _request("cmp005", "De : <a@{slug}.example>\nObjet : Re\n\nYour data has been deleted from our systems.", status="completed")
    _request("cmp006", "De : <a@{slug}.example>\nObjet : Re\n\nUnclear answer nobody can classify.")
    with SessionLocal() as db:
        items = model_compare.sample(db, 40)
    ours = {i["broker"]: i["expected"] for i in items if i["broker"].startswith("Courtier cmp")}
    assert ours == {"Courtier cmp001": "clôture", "Courtier cmp002": "action demandée", "Courtier cmp003": "accusé de réception",
                    "Courtier cmp004": "pièce d'identité", "Courtier cmp005": "clôture"}   # l'incertaine ne sert pas de référence

    answers = {"good": lambda text: ("completed_no_data" if "do not hold" in text else "completed_deleted" if "deleted" in text
                                     else "needs_action" if "form" in text else "ack" if "ticket" in text else "id_request"),
               "vague": lambda text: "reply"}

    def fake(model, mode, subject, text, timeout=300):
        if model == "broken":
            raise OSError("modèle introuvable")
        return {"category": answers[model](text), "confidence": 0.9}
    monkeypatch.setattr(llm, "classify_with", fake)
    monkeypatch.setattr(llm, "installed_models", lambda max_age=20.0: ["good", "vague", "broken"])
    monkeypatch.setattr(model_compare.energy, "ollama_state", lambda: {"models": [{"name": "good", "size": 4_000_000_000, "size_vram": 0}]})
    assert model_compare.start(["absent"], 20) == "Choisissez au moins un modèle installé."
    assert model_compare.start(["good", "vague", "broken"], 40) == ""
    for _ in range(200):
        if not model_compare.state.get("running"):
            break
        time.sleep(0.02)
    with SessionLocal() as db:
        res = {r["model"]: r for r in model_compare.last(db)["results"]}
    assert res["good"]["percent"] == 100 and res["good"]["memory_gb"] == 4.0 and res["good"]["gpu_share"] == 0
    assert res["vague"]["percent"] == 0 and res["vague"]["abstain"] == res["vague"]["total"] and res["vague"]["wrong"] == 0
    assert res["broken"]["errors"] >= 3 and res["broken"]["percent"] is None
    monkeypatch.setattr(settings, "llm_url", "http://ollama.test")
    page = html.unescape(client.get("/settings/models/compare").text)
    assert "Dernière comparaison" in page and "100 %" in page and "Lancer la comparaison" in page
    assert "/settings/models/compare" in client.get("/settings").text


# ------------------------------------------------- démarchage téléphonique ---

def test_telemarketing_case_prepares_letter_and_report(client):
    with SessionLocal() as db:
        p = Profile(full_name="Appelé Test", emails=["appele@exemple.fr"], phones=["06 11 22 33 44"], addresses=[], other_names=[])
        db.add(p)
        db.commit()
        pid = p.id
    assert "centre d’appels vous a démarché" in html.unescape(client.get("/privacy-cases").text)
    r = client.post("/privacy-cases/telemarketing", data={
        "company": "Isolation Plus", "call_date": "2026-11-12", "caller_number": "01 62 00 00 00<script>",
        "contact_email": "dpo@isolation-plus.example", "profile_id": pid, "note": "pompe à chaleur"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/assistant")
    page = html.unescape(client.get(r.headers["location"]).text)
    for expected in ("À : dpo@isolation-plus.example", "du 12/11/2026 depuis le 01 62 00 00 00", "06 11 22 33 44",
                     "article 21, paragraphes 2 et 3", "article 15, paragraphe 1, g", "article 7, paragraphe 1",
                     "11 août 2026", "Signaler l’appel", "signal.conso.gouv.fr", "Appelé Test"):
        assert expected in page, expected
    assert "<script>" not in page.split("Signaler l’appel")[1].split("</section>")[0]
    assert "Démarchage téléphonique" in html.unescape(client.get("/privacy-cases").text)
    # Relance et réexamen gardent les modèles généraux
    assert "état de traitement" in html.unescape(client.get(r.headers["location"] + "?kind=followup").text)
