"""Réanalyse des réponses déjà reçues : propositions sans effet, validation, garde-fous."""
from datetime import timedelta
from email.message import EmailMessage

import pytest

from app import llm, mailer, reanalysis
from app.config import settings
from app.db import KV, Broker, Event, Profile, Request, SessionLocal, utcnow

TEXT_DONE = "Hello, we confirm that your personal data has been erased from our systems."
TEXT_AMBIGUOUS = "Thanks for reaching out, our team looked into it and there is nothing further on our side."


def _raw(msgid, text, trusted=True, domain="acme-re.example"):
    m = EmailMessage()
    m["From"] = f"dpo@{domain}"
    m["Subject"] = "Re: votre demande"
    m["Message-ID"] = msgid
    if trusted:
        m["Authentication-Results"] = f"mx.test; dkim=pass header.d={domain}; dmarc=pass header.from={domain}"
    m.set_content(text)
    return bytes(m)


def _request(status, text, msgid, slug):
    with SessionLocal() as db:
        broker = db.query(Broker).filter(Broker.slug == slug).first() or Broker(
            slug=slug, name=f"Acme {slug}", email="dpo@acme-re.example", countries=["fr"], categories=[])
        profile = Profile(full_name="Re Test", emails=["moi@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add_all([broker, profile])
        db.flush()
        due = utcnow() + timedelta(days=12)
        req = Request(profile_id=profile.id, broker_id=broker.id, reference=f"OUBLI-{slug.upper()[:6]}", status=status,
                      sent_at=utcnow() - timedelta(days=18), due_at=due, reminder_count=1)
        db.add(req)
        db.flush()
        db.add(Event(request_id=req.id, kind="reply", summary="Réponse reçue",
                     body=f"De : DPO <dpo@acme-re.example>\nDate : hier\nObjet : Re: votre demande\n\n{text}", message_id=msgid))
        db.commit()
        return req.id, due


@pytest.fixture
def imap(monkeypatch):
    monkeypatch.setattr(settings, "imap_host", "imap.test")
    monkeypatch.setattr(settings, "imap_user", "moi@exemple.fr")
    monkeypatch.setattr(settings, "imap_password", "x")
    box: dict[str, bytes] = {}
    monkeypatch.setattr(mailer, "fetch_by_message_id", lambda ids: {i: box[i] for i in ids if i in box})
    yield box
    with SessionLocal() as db:
        db.query(KV).filter(KV.key.like("reanalysis:%")).delete(synchronize_session=False)
        db.commit()


def test_misclassified_reply_is_proposed_not_applied(client, imap):
    rid, due = _request("replied", TEXT_DONE, "<done@acme-re.example>", "re-a")
    imap["<done@acme-re.example>"] = _raw("<done@acme-re.example>", TEXT_DONE)
    proposals = reanalysis.analyse()
    p = next(p for p in proposals if p["request_id"] == rid)
    assert (p["from_status"], p["to_status"], p["trusted"], p["from_mailbox"]) == ("replied", "completed", True, True)
    with SessionLocal() as db:
        assert db.get(Request, rid).status == "replied"  # rien n'est appliqué sans validation


def test_no_closure_without_authenticated_sender(client, imap):
    rid, _ = _request("replied", TEXT_DONE, "<gone@acme-re.example>", "re-b")  # message absent de la boîte
    assert not [p for p in reanalysis.analyse([rid]) if p["request_id"] == rid]
    rid2, _ = _request("needs_action", TEXT_DONE, "<forged@acme-re.example>", "re-c")
    imap["<forged@acme-re.example>"] = _raw("<forged@acme-re.example>", TEXT_DONE, trusted=False)
    p = reanalysis.analyse([rid2])[0]
    assert p["to_status"] == "replied" and p["downgraded"]


def test_new_model_reclassifies_ambiguous_reply(client, imap, monkeypatch):
    monkeypatch.setattr(settings, "llm_url", "http://ollama:11434")
    monkeypatch.setattr(settings, "llm_model", "qwen3:8b")
    monkeypatch.setattr(llm, "classify", lambda subject, text, **kw: {
        "category": "completed_no_data", "confidence": 0.88, "summary": "Aucune donnée vous concernant.", "ticket": None})
    rid, _ = _request("replied", TEXT_AMBIGUOUS, "<amb@acme-re.example>", "re-d")
    imap["<amb@acme-re.example>"] = _raw("<amb@acme-re.example>", TEXT_AMBIGUOUS)
    p = reanalysis.analyse([rid])[0]
    assert p["to_status"] == "completed" and p["note"] == "Analyse IA : Aucune donnée vous concernant."


def test_apply_changes_status_only(client, imap):
    rid, due = _request("replied", TEXT_DONE, "<apply@acme-re.example>", "re-e")
    imap["<apply@acme-re.example>"] = _raw("<apply@acme-re.example>", TEXT_DONE)
    rid2, _ = _request("replied", TEXT_DONE, "<moved@acme-re.example>", "re-f")
    imap["<moved@acme-re.example>"] = _raw("<moved@acme-re.example>", TEXT_DONE)
    with SessionLocal() as db:
        db.merge(KV(key=reanalysis.PROPOSALS_KEY, value=__import__("json").dumps(reanalysis.analyse([rid, rid2]))))
        db.get(Request, rid2).status = "needs_action"  # changé à la main entre-temps
        db.commit()
    r = client.post("/tasks/reanalysis/apply", data={"ids": [str(rid), str(rid2)]}, follow_redirects=True)
    assert "1 correction(s) appliquée(s)" in r.text and "1 ignorée(s)" in r.text
    with SessionLocal() as db:
        req = db.get(Request, rid)
        kinds = [e.kind for e in req.events]
        assert req.status == "completed" and req.due_at == due and req.reminder_count == 1
        assert "auto_reply" not in kinds and "confirm" not in kinds and "reminder" not in kinds
        assert any(e.summary.startswith("Réanalyse : données supprimées") for e in req.events)
        assert db.get(Request, rid2).status == "needs_action"
        assert reanalysis.load(db) == []


def test_ack_returns_to_waiting_without_resetting_deadline(client, imap):
    text = "We have received your request and will respond within one month."
    rid, due = _request("replied", text, "<ack@acme-re.example>", "re-g")
    imap["<ack@acme-re.example>"] = _raw("<ack@acme-re.example>", text)
    with SessionLocal() as db:
        db.merge(KV(key=reanalysis.PROPOSALS_KEY, value=__import__("json").dumps(reanalysis.analyse([rid]))))
        db.commit()
        assert reanalysis.apply(db, [rid]) == (1, 0)
        req = db.get(Request, rid)
        assert req.status == "sent" and req.due_at == due


def test_pages_and_model_change_banner(client, imap, monkeypatch):
    started = []
    monkeypatch.setattr(reanalysis, "start", lambda ids=None: started.append(ids) or True)
    _request("replied", TEXT_AMBIGUOUS, "<p@acme-re.example>", "re-h")
    monkeypatch.setattr(settings, "llm_url", "http://ollama:11434")
    monkeypatch.setattr(settings, "llm_model", "qwen3:8b")
    assert "Le modèle d'IA a changé (qwen3:8b)" in __import__("html").unescape(client.get("/tasks").text)
    client.post("/tasks/reanalysis/start")
    assert started == [None]
    assert "Réanalyse des réponses" in client.get("/tasks/reanalysis").text


def test_fetch_by_message_id_searches_watched_and_filing_folders(monkeypatch):
    class Fake:
        def __init__(self):
            self.current, self.searched = None, []

        def select(self, name, readonly=False):
            assert readonly
            self.current = name.strip('"')
            return "OK", [b""]

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                self.searched.append((self.current, args[-1]))
                return "OK", [b"7" if self.current == "Oubliez-moi" else b""]
            assert "BODY.PEEK" in args[1]
            return "OK", [(b"7 (BODY[] {3}", b"raw"), b")"]

        def logout(self):
            pass

    fake = Fake()
    monkeypatch.setattr(mailer, "_imap_connect", lambda: fake)
    monkeypatch.setattr(settings, "imap_folders", ["INBOX"])
    monkeypatch.setattr(settings, "imap_file_mode", "move")
    monkeypatch.setattr(settings, "imap_file_folder", "Oubliez-moi")
    assert mailer.fetch_by_message_id(["<a@b.example>", 'pas"valide']) == {"<a@b.example>": b"raw"}
    assert fake.searched == [("INBOX", '"<a@b.example>"'), ("Oubliez-moi", '"<a@b.example>"')]
