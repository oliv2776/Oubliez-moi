import json
import threading
from datetime import timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import select

from app import settings_store, confirm, llm, mailer, notify, service
from app.config import settings
from app.db import FAILED, NEEDS_ACTION, QUEUED, REPLIED, SENT, Broker, Event, Profile, Request, SessionLocal, utcnow


def _sent_request(slug_offset=20, web=None):
    with SessionLocal() as db:
        p = Profile(full_name="Auto Mate", emails=["auto@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add(p)
        db.commit()
        b = db.scalars(select(Broker).where(Broker.enabled.is_(True), Broker.email.is_not(None)).offset(slug_offset)).first()
        if web:
            b.web = web
        r = service._create_request(db, p, b, cycle=1)
        db.commit()
        service.send_initial(db, r)
        return r.id


def _reply(rid, body, sender=None, subject=None, html=None, msgid=None, reply_to=None):
    with SessionLocal() as db:
        r = db.get(Request, rid)
        m = EmailMessage()
        m["From"] = sender or r.broker.email
        m["Authentication-Results"] = "mx.test; spf=pass; dkim=pass; dmarc=pass"
        m["Subject"] = subject or f"Re: {r.reference}"
        m["Message-ID"] = msgid or f"<{abs(hash(body)) % 10**9}.{rid}@broker.example>"
        if reply_to:
            m["Reply-To"] = reply_to
        m.set_content(body)
        if html:
            m.add_alternative(html, subtype="html")
        service.process_incoming(db, mailer.parse_mail(bytes(m)))
        db.commit()


def _events(rid, kind):
    with SessionLocal() as db:
        return [(e.summary, e.body) for e in db.get(Request, rid).events if e.kind == kind]


def test_id_request_gets_one_automatic_rebuttal(client):
    rid = _sent_request(20)
    _reply(rid, "Please send us a copy of your passport to proceed.", sender="noreply@broker-tickets.example",
           reply_to=None)
    autos = _events(rid, "auto_reply")
    assert len(autos) == 1 and "pièce d'identité" in autos[0][0]
    with SessionLocal() as db:
        r = db.get(Request, rid)
        assert r.status == SENT
        assert r.broker.email in autos[0][0]  # « noreply » : on écrit à l'adresse du courtier
        assert "Article 12(6)" in autos[0][1] or "article 12, paragraphe 6" in autos[0][1]
    # Le courtier insiste : pas de seconde réponse automatique, la main revient à l'utilisateur
    _reply(rid, "We still need a copy of your ID card.", msgid="<insist@broker.example>")
    assert len(_events(rid, "auto_reply")) == 1
    with SessionLocal() as db:
        assert db.get(Request, rid).status == NEEDS_ACTION


def test_duplicate_message_is_ignored(client):
    rid = _sent_request(21)
    _reply(rid, "Question : quel est votre ancien code postal ?", msgid="<dup@broker.example>")
    _reply(rid, "Question : quel est votre ancien code postal ?", msgid="<dup@broker.example>")
    assert len(_events(rid, "reply")) == 1


def test_legit_refusal_countered_and_extension_moves_deadline(client):
    rid = _sent_request(22)
    with SessionLocal() as db:
        dom = db.get(Request, rid).broker.email.split("@")[1]
    _reply(rid, "We are unable to delete your data, which we process on the basis of our legitimate interest.",
           msgid="<legit@b.example>", reply_to=f"privacy-team@{dom}")
    autos = _events(rid, "auto_reply")
    assert len(autos) == 1 and f"privacy-team@{dom}" in autos[0][0] and "21" in autos[0][1]
    # Adresse de réponse hors du domaine du courtier : ignorée, on écrit à l'adresse connue du courtier
    rid3 = _sent_request(30)
    _reply(rid3, "Could you confirm your country of residence?", msgid="<spoof@b.example>", reply_to="collect@attacker.example")
    autos = _events(rid3, "auto_reply")
    assert len(autos) == 1 and "attacker" not in autos[0][0]
    rid2 = _sent_request(23)
    _reply(rid2, "Due to the complexity, we need to extend the response period by a further two months.", msgid="<ext@b.example>")
    with SessionLocal() as db:
        r = db.get(Request, rid2)
        assert r.status == SENT and r.due_at >= r.sent_at + timedelta(days=89)


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = "/confirm" in self.path
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = "<title>Privacy</title><h1>Your request has been confirmed.</h1>" if ok else "<form><button>Confirm</button></form>"
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def local_site():
    srv = HTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    confirm.ALLOW_PRIVATE = True
    yield f"http://localhost:{srv.server_port}"
    confirm.ALLOW_PRIVATE = False
    srv.shutdown()


def test_confirmation_link_clicked(client, local_site):
    settings.auto_confirm_links = True
    try:
        rid = _sent_request(24, web="http://localhost")
        _reply(rid, "Please confirm your request by clicking the link below.", msgid="<conf@b.example>",
               html=f'<p>Please confirm your request by clicking the link below.</p><a href="{local_site}/unsubscribe?x=1">Unsubscribe</a>'
                    f'<a href="{local_site}/confirm?token=abc">Confirm my request</a><a href="https://evil.example/confirm">Confirm</a>')
        clicks = _events(rid, "confirm")
        assert len(clicks) == 1 and clicks[0][1].endswith("/confirm?token=abc") and "confirmation" in clicks[0][0]
        with SessionLocal() as db:
            assert db.get(Request, rid).status == SENT
        # Page qui demande un bouton : la demande reste « Action requise »
        rid2 = _sent_request(25, web="http://localhost")
        _reply(rid2, "Please confirm your email by clicking the link.", msgid="<conf2@b.example>",
               html=f'<a href="{local_site}/verify?t=1">Verify</a>')
        with SessionLocal() as db:
            assert db.get(Request, rid2).status == NEEDS_ACTION
    finally:
        settings.auto_confirm_links = False


def test_ssrf_protection():
    confirm.ALLOW_PRIVATE = False
    assert not confirm.click("http://127.0.0.1:9/confirm").ok
    assert confirm.candidate_links([("https://acme.com/confirm?t=1", ""), ("https://other.com/confirm", "")], "dpo@acme.com", None) == [
        "https://acme.com/confirm?t=1"]


class _Ollama(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert payload["format"] == "json"
        answer = {"category": "completed_deleted", "confidence": 0.92, "summary": "Le courtier a retiré l'adresse de sa base.", "ticket": "A-42"}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"message": {"content": json.dumps(answer)}}).encode())

    def log_message(self, *a):
        pass


def test_local_ai_classifies_ambiguous_reply(client):
    """Mode conversationnel (/api/chat) : catégorie, résumé et numéro de dossier."""
    srv = HTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    settings.llm_url = f"http://127.0.0.1:{srv.server_port}"
    settings.llm_mode, settings.llm_model = "chat", "qwen2.5:3b"
    try:
        rid = _sent_request(26)
        _reply(rid, "Bonjour, nous avons fait le nécessaire concernant votre profil. Cordialement.", msgid="<ai@b.example>")
        with SessionLocal() as db:
            r = db.get(Request, rid)
            assert r.status == "completed"
            assert any(e.summary.startswith("Analyse IA") and "A-42" in e.summary for e in r.events)
    finally:
        settings.llm_url = ""
        settings.llm_mode, settings.llm_model = "decision", "tev1:4b"
        srv.shutdown()


class _DecisionOllama(BaseHTTPRequestHandler):
    """Faux Ollama : modèles de décision (/v1/systemone), liste (/api/tags) et téléchargement (/api/pull)."""
    pulled = set()

    deleted = set()

    def do_GET(self):
        if self.path == "/api/tags":
            names = [n for n in ["tev1:4b", "qwen2.5:3b", "mistral:7b"] + sorted(self.pulled) if n not in self.deleted]
            self._json({"models": [{"name": n, "size": 3_400_000_000} for n in names]})

    def do_DELETE(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        name = body.get("model") or body.get("name")
        if name in ("tev1:4b", "qwen2.5:3b", "mistral:7b") or name in self.pulled:
            type(self).deleted.add(name)
            self._json({})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/systemone":
            q = body["questions"]["category"]
            assert q["type"] == "choice" and "completed_no_data" in q["criteria"]
            assert "message" in body["state"]
            confident = "nothing" in body["state"]["message"]
            probs = {"completed_no_data": 0.91, "reply": 0.05} if confident else {"completed_no_data": 0.40, "reply": 0.35}
            self._json({"model": body["model"], "answers": {"category": {
                "type": "choice", "choice": "completed_no_data", "probabilities": probs, "confidence": 0.8}}})
        elif self.path == "/api/pull":
            self.send_response(200)
            self.end_headers()
            for line in ({"status": "pulling manifest"},
                         {"status": "downloading", "total": 2_000_000_000, "completed": 1_000_000_000},
                         {"status": "success"}):
                self.wfile.write((json.dumps(line) + "\n").encode())
            type(self).pulled.add(body["model"])

    def _json(self, data):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *a):
        pass


def test_decision_model_classifies_with_probability(client):
    srv = HTTPServer(("127.0.0.1", 0), _DecisionOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    settings.llm_url = f"http://127.0.0.1:{srv.server_port}"
    settings.llm_mode, settings.llm_model = "decision", "tev1:4b"
    try:
        # Probabilité 91 % : appliqué ; le numéro de dossier vient du texte
        rid = _sent_request(32)
        _reply(rid, "Hello, we hold nothing about you anymore. Our reference: CS-55120.", msgid="<dec1@b.example>")
        with SessionLocal() as db:
            r = db.get(Request, rid)
            assert r.status == "completed"
            note = [e.summary for e in r.events if e.summary.startswith("Analyse IA")][0]
            assert "aucune donnée" in note and "91 %" in note and "CS-55120" in note
        # Probabilité 40 % : sous le seuil, la réponse reste à examiner
        rid = _sent_request(33)
        _reply(rid, "Bonjour, votre dossier suit son cours chez nos équipes.", msgid="<dec2@b.example>")
        with SessionLocal() as db:
            assert db.get(Request, rid).status == "replied"
    finally:
        settings.llm_url = ""
        srv.shutdown()


def test_model_picker_download_and_status(client):
    import time
    from app import llm
    srv = HTTPServer(("127.0.0.1", 0), _DecisionOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    form = {"_settings_form": "1", "tab": "automatismes", "dry_run": "true", "send_per_hour": "30", "llm_url": url}
    try:
        # Modèle du catalogue : le type est imposé, même si le formulaire dit autre chose
        client.post("/settings", data={**form, "llm_model": "qwen2.5:7b", "llm_mode": "decision"})
        assert settings.llm_model == "qwen2.5:7b" and settings.llm_mode == "chat"
        client.post("/settings", data={**form, "llm_model": "nimble"})
        assert settings.llm_mode == "decision"
        # « Autre modèle » : champ libre et type choisi
        client.post("/settings", data={**form, "llm_model": "__custom__", "llm_model_custom": "mistral:7b", "llm_mode": "chat"})
        assert settings.llm_model == "mistral:7b" and settings.llm_mode == "chat"
        page = client.get("/settings")
        assert "Tev1 4B" in page.text and "Nimble 9B" in page.text and "Qwen2.5 3B" in page.text
        assert "✓ installé" in page.text
        # Statut puis téléchargement d'un modèle absent
        st = client.get("/settings/llm-status?model=nimble").json()
        assert st["reachable"] and not st["installed"]
        assert client.post("/settings/llm-pull", data={"model": "nimble"}).status_code == 200
        for _ in range(50):
            st = client.get("/settings/llm-status?model=nimble").json()
            if st["pull"].get("done"):
                break
            time.sleep(0.05)
        assert st["pull"]["done"] and not st["pull"]["error"] and st["installed"]
        assert client.post("/settings/llm-pull", data={"model": "bad name;rm"}).status_code == 400
        # Liste des modèles installés avec leur taille, puis suppression
        page = client.get("/settings")
        assert "Modèles installés sur votre serveur" in page.text and "3,4 Go" in page.text
        assert client.post("/settings/llm-delete", data={"model": "mistral:7b"}).json() == {"ok": True}
        llm._installed_cache["names"] = None
        assert "mistral:7b" not in (llm.installed_models() or [])
        assert client.post("/settings/llm-delete", data={"model": "inconnu:1b"}).status_code == 400
        assert client.post("/settings/llm-delete", data={"model": "x;rm -rf"}).status_code == 400
    finally:
        settings_store.reset_to_env()
        llm._installed_cache["names"] = None
        srv.shutdown()


def test_notifications_and_digest(client, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_post", lambda url, data, headers=None: sent.append((url, data, headers)))
    settings.notify_ntfy_url, settings.notify_ntfy_token, settings.app_url = "https://ntfy.example/mon-sujet", "tk", "http://10.0.0.5:27480"
    try:
        assert notify.push("Titre accentué", "Message") == []
        url, data, headers = sent[-1]
        assert url == "https://ntfy.example" and data["topic"] == "mon-sujet" and data["click"].endswith("/tasks")
        assert headers["Authorization"] == "Bearer tk"
    finally:
        settings.notify_ntfy_url, settings.notify_ntfy_token, settings.app_url = "", "", ""

    mails = []
    monkeypatch.setattr(mailer, "send_message", lambda msg, force=False: mails.append(msg))
    settings.notify_email_digest, settings.smtp_host = True, "smtp.example"
    try:
        rid = _sent_request(27)
        _reply(rid, "Merci de nous indiquer votre ancienne adresse.", msgid="<digest@b.example>")
        assert "récapitulatif" in (service.send_digest() or "")
        assert "à traiter" in mails[-1]["Subject"] and service.send_digest() is None  # une fois par jour
    finally:
        settings.notify_email_digest, settings.smtp_host = False, ""


def test_catalog_new_address_requeues_failed(client):
    rid = _sent_request(28)
    with SessionLocal() as db:
        r = db.get(Request, rid)
        service.set_status(db, r, FAILED)
        db.commit()
        b = r.broker
        rec = {"slug": b.slug, "name": b.name, "email": "nouveau-dpo@exemple.org", "webform": None, "web": b.web, "postal_address": None,
               "countries": b.countries, "categories": b.categories, "needs_id": False, "quality": None, "source": "datenanfragen"}
        service.sync_brokers(db, [rec])
        assert db.get(Request, rid).status == QUEUED


def test_dossier_page(client):
    rid = _sent_request(29)
    page = client.get(f"/requests/{rid}/dossier")
    assert page.status_code == 200 and "Dossier de réclamation" in page.text and "Pièce 1" in page.text


def test_utf7_folder_names():
    assert mailer.imap_utf7("Désinscriptions") == "D&AOk-sinscriptions" and mailer.imap_utf7("R&D") == "R&-D"


def test_cancel_links_are_never_clicked():
    from app.confirm import candidate_links
    links = [
        ("https://privacy.acme.example/request/cancel?token=abc", "Annuler ma demande"),
        ("https://privacy.acme.example/withdraw?token=abc", "Withdraw my request"),
        ("https://privacy.acme.example/r?token=abc", "Voir le dossier"),
        ("https://privacy.acme.example/r/confirm?token=abc", "Confirmer ma demande"),
        ("https://evil.example/confirm?t=1", "Confirm"),
    ]
    out = candidate_links(links, "dpo@acme.example", "https://www.acme.example")
    assert out[0].endswith("/r/confirm?token=abc")
    assert not any("cancel" in u or "withdraw" in u or "evil" in u for u in out)


def test_accented_folder_names_are_encoded():
    from app.mailer import _quote_folder
    assert _quote_folder("[Gmail]/Messages envoyés") == '"[Gmail]/Messages envoy&AOk-s"'
    assert _quote_folder("INBOX") == '"INBOX"'


def test_escalation_reminder_then_formal_notice_then_overdue(client):
    from datetime import timedelta
    from app.db import OVERDUE, SENT, utcnow
    rid = _sent_request(31)
    with SessionLocal() as db:
        db.query(Request).filter(Request.status == "queued").update({"status": "cancelled"})
        db.query(Request).filter(Request.status == SENT, Request.id != rid).update({"due_at": utcnow() + timedelta(days=30)})
        db.query(Request).filter(Request.supplement_pending.is_(True)).update({"supplement_pending": False})
        r = db.get(Request, rid)
        r.due_at = utcnow() - timedelta(days=1)
        db.commit()
    assert "relance" in service.send_tick()
    with SessionLocal() as db:
        r = db.get(Request, rid)
        assert r.reminder_count == 1 and r.status == SENT
        r.due_at = utcnow() - timedelta(days=1)
        db.commit()
    assert "mise en demeure" in service.send_tick()
    with SessionLocal() as db:
        r = db.get(Request, rid)
        assert r.reminder_count == 2 and any(e.kind == "formal_notice" for e in r.events)
        body = [e for e in r.events if e.kind == "formal_notice"][0].body
        assert "Mise en demeure" in body or "Formal notice" in body
        r.due_at = utcnow() - timedelta(days=1)
        db.commit()
    service.check_deadlines()
    with SessionLocal() as db:
        assert db.get(Request, rid).status == OVERDUE


def test_unverified_ticket_sender_cannot_confirm_deletion(client):
    rid = _sent_request(34)
    _reply(rid, "Your data has been deleted successfully.",
           sender="support@external-ticketing.example", reply_to=None)
    with SessionLocal() as db:
        req = db.get(Request, rid)
        assert req.status != "completed"
        assert any("Réponse non vérifiée" in e.summary for e in req.events)
