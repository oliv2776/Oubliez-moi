"""« En bref » : résumé de la dernière réponse, IA ou règles, et marche à suivre."""
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from app import insight, llm
from app.config import settings
from app.db import Broker, Event, Profile, Request, SessionLocal

BODY = "De : DPO <dpo@ins.example>\nDate : hier\nObjet : Re: demande\n\nHello,\n\nPlease send us a copy of your ID card so we can process the request.\n\nOn Mon wrote:\n> old"


def _req(slug, status, events):
    with SessionLocal() as db:
        b = Broker(slug=slug, name=f"Courtier {slug}", email=f"dpo@{slug}.example", countries=["fr"], categories=[])
        p = Profile(full_name="Ins Test", emails=["moi@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add_all([b, p])
        db.flush()
        r = Request(profile_id=p.id, broker_id=b.id, reference=f"OUBLI-{slug.upper()}", status=status)
        db.add(r)
        db.flush()
        db.add(Event(request_id=r.id, kind="reply", summary="Réponse reçue", body=BODY))
        for kind, summary in events:
            db.add(Event(request_id=r.id, kind=kind, summary=summary))
        db.commit()
        return r.id


def _insight(rid):
    with SessionLocal() as db:
        return insight.for_request(db.get(Request, rid))


def test_rules_give_summary_and_what_to_do(client):
    rid = _req("ins-a", "needs_action", [("status", "Classement automatique : demande de pièce d'identité (« copy of your ID »)")])
    ins = _insight(rid)
    assert (ins.kind, ins.by_ai, ins.ai_action) == ("id_request", False, False)
    assert ins.summary == "Please send us a copy of your ID card so we can process the request."
    assert "preuve d'identité" in ins.action and "masquant" in ins.action


def test_ai_summary_and_advice_are_used(client):
    rid = _req("ins-b", "needs_action", [
        ("info", "Analyse IA : Le courtier demande une copie de pièce d'identité. (dossier n° 4521)"),
        ("info", "Conseil IA : Répondez que votre adresse e-mail suffit à vous identifier."),
        ("status", "Classement automatique : demande de pièce d'identité (« IA, probabilité 91 % »)"),
    ])
    ins = _insight(rid)
    assert (ins.by_ai, ins.confidence, ins.ticket) == (True, 91, "4521")
    assert ins.summary == "Le courtier demande une copie de pièce d'identité. (dossier n° 4521)" and ins.ai_summary
    assert ins.action == "Répondez que votre adresse e-mail suffit à vous identifier." and ins.ai_action


def test_decision_model_without_text_and_unverified_sender(client):
    rid = _req("ins-c", "replied", [
        ("info", "Analyse IA : données supprimées (probabilité 64 %)"),
        ("warning", "Réponse non vérifiée de x@autre.example : décision sensible non appliquée"),
        ("status", "Classement automatique : réponse à examiner (« expéditeur non vérifié »)"),
    ])
    ins = _insight(rid)
    assert ins.by_ai and ins.confidence == 64 and not ins.ai_summary
    assert ins.summary.startswith("Please send us a copy")  # pas de résumé IA : début du message
    assert ins.unverified and "authentifié" in ins.caution


def test_triage_shows_brief_and_ai_filter(client):
    rid_ai = _req("ins-d", "needs_action", [
        ("info", "Analyse IA : Le courtier veut une pièce d'identité."),
        ("info", "Conseil IA : Envoyez une copie masquée."),
        ("status", "Classement automatique : demande de pièce d'identité (« IA, probabilité 88 % »)"),
    ])
    rid_rules = _req("ins-e", "needs_action", [("status", "Classement automatique : action demandée")])
    page = html.unescape(client.get(f"/tasks?id={rid_ai}").text)
    assert "En bref" in page and "Analysé par l'IA · 88 %" in page and "Conseil de l'IA :" in page
    assert "Envoyez une copie masquée." in page
    ai_only = html.unescape(client.get("/tasks?cat=ai").text)
    assert "Courtier ins-d" in ai_only and "Courtier ins-e" not in ai_only and "Classées par l'IA" in ai_only
    assert "À faire :" in html.unescape(client.get(f"/tasks?id={rid_rules}").text)


class _Chat(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert '"action"' in body["messages"][0]["content"]  # le modèle doit proposer quoi faire
        answer = {"category": "id_request", "confidence": 0.9, "summary": "Pièce d'identité demandée.",
                  "action": "Répondez que l'adresse e-mail suffit.", "ticket": None}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"message": {"content": json.dumps(answer)}}).encode())

    def log_message(self, *a):
        pass


def test_chat_model_returns_advice(client, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Chat)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "llm_url", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(settings, "llm_model", "qwen3:8b")
    monkeypatch.setattr(settings, "llm_mode", "chat")
    try:
        res = llm.classify("Re: demande", "Please send your ID.")
        assert res["action"] == "Répondez que l'adresse e-mail suffit."
    finally:
        srv.shutdown()


def test_summarize_on_demand_keeps_status(client, monkeypatch):
    rid = _req("ins-f", "needs_action", [("status", "Classement automatique : action demandée")])
    monkeypatch.setattr(settings, "llm_url", "http://ollama:11434")
    monkeypatch.setattr(settings, "llm_model", "qwen3:8b")
    monkeypatch.setattr(settings, "llm_mode", "chat")
    assert "Résumer et conseiller avec l'IA" in html.unescape(client.get(f"/tasks?id={rid}").text)
    monkeypatch.setattr(llm, "classify", lambda subject, text, **kw: {
        "category": "id_request", "confidence": 0.9, "summary": "Ils veulent une pièce d'identité.",
        "action": "Envoyez une copie masquée.", "ticket": "77", "mode": "chat"})
    client.post(f"/requests/{rid}/summarize", data={"next": f"/tasks?id={rid}"})
    ins = _insight(rid)
    assert (ins.summary, ins.action, ins.ticket) == ("Ils veulent une pièce d'identité. (dossier n° 77)", "Envoyez une copie masquée.", "77")
    with SessionLocal() as db:
        assert db.get(Request, rid).status == "needs_action"


def test_model_input_is_cleaned_and_own_reference_is_not_a_ticket(client):
    text = ("We submitted your deletion request. Please verify via the email from privacy-requests@branch.io.\n\n\n\n"
            "<https://us.content.exclaimer.net/?url=https%3A%2F%2Fwww.branch.io%2F&tenantid=abc&signature=" + "x" * 300 + ">;")
    cleaned = llm.clean_for_model(text)
    assert "[lien us.content.exclaimer.net]" in cleaned and "signature" not in cleaned and "\n\n\n" not in cleaned
    assert "privacy-requests@branch.io" in cleaned
    assert llm._their_ticket("OUBLI-YSYGBH") is None and llm._their_ticket("null") is None
    assert llm._their_ticket("CASE-4521") == "CASE-4521"
    assert "N'invente aucun lien" in llm.SYSTEM and "OUBLI-" in llm.SYSTEM


def test_old_notes_with_own_reference_are_cleaned(client):
    rid = _req("ins-g", "needs_action", [
        ("info", "Analyse IA : Le courtier demande une vérification d'identité. (dossier n° OUBLI-YSYGBH)"),
        ("status", "Classement automatique : action demandée (« IA, probabilité 90 % »)")])
    ins = _insight(rid)
    assert ins.summary == "Le courtier demande une vérification d'identité." and ins.ticket is None


def test_long_tracking_links_are_shortened_in_mail_text():
    from app import mailview
    out = mailview.text_to_html("Best,\nhttps://us.content.exclaimer.net/?url=https%3A%2F%2Fwww.branch.io%2F&signature=" + "y" * 200)
    assert ">us.content.exclaimer.net/…</a>" in out and "y" * 200 in out  # le lien complet reste dans href
