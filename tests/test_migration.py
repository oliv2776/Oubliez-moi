"""Migration des comptes : inventaire des en-têtes, alias Proton Pass via SimpleLogin, page et actions."""
import json
import threading
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import select

from app import crypto, mailboxes, migration, simplelogin
from app.config import settings
from app.db import KV, AccountMigration, Broker, Mailbox, Profile, Request, SessionLocal, kv_get


def _msg(frm, subject, to="moi@gmail.com", date="Mon, 26 Oct 2026 10:00:00 +0100", extra=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = frm, to, subject, date
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content("corps jamais lu")
    return bytes(m)


class FakeIMAP:
    def __init__(self, folders):
        self.folders, self.current, self.untagged_responses = folders, None, {}

    def select(self, name, readonly=False):
        name = name.strip('"')
        if name not in self.folders:
            return "NO", [b""]
        self.current, self.untagged_responses = name, {"UIDVALIDITY": [b"7"]}
        return "OK", [b""]

    def uid(self, cmd, *args):
        box = self.folders[self.current]
        if cmd == "SEARCH":
            crit = [a for a in args if a is not None]
            uids = [u for u in box if u >= int(crit[1].split(":")[0])] if crit[0] == "UID" else list(box)
            return "OK", [" ".join(map(str, sorted(uids))).encode()]
        assert cmd == "FETCH" and "BODY.PEEK" in args[1]  # lecture seule
        out = []
        for u in map(int, args[0].split(",")):
            head = box[u].split(b"\n\n")[0] + b"\n\n"
            out += [(f"{u} (UID {u} BODY[HEADER.FIELDS] {{{len(head)}}}".encode(), head), b")"]
        return "OK", out

    def logout(self):
        return "BYE", [b""]


@pytest.fixture
def boxes(client, monkeypatch):
    old = FakeIMAP({"INBOX": {
        1: _msg("Amazon <commande@amazon.fr>", "Votre commande n° 123 a été expédiée", date="Mon, 05 Oct 2026 10:00:00 +0200"),
        2: _msg("Amazon <news@amazon.fr>", "Offres de la semaine", date="Wed, 28 Oct 2026 10:00:00 +0100",
                extra={"List-Unsubscribe": "<https://amazon.fr/u>"}),
        3: _msg("Paul Martin <paul.martin@gmail.com>", "Dimanche ?"),          # une personne : ignorée
        4: _msg("Moi <moi@gmail.com>", "Note à moi-même"),                     # mon adresse : ignorée
        5: _msg("DPO <privacy@courtier-mig.example>", "Re: votre demande RGPD"),  # courtier contacté : ignoré
        6: _msg("Le Monde <newsletter@lemonde.fr>", "La matinale", extra={"List-Id": "<matinale.lemonde.fr>"}),
    }, "[Gmail]/Spam": {9: _msg("Casino <win@casino.example>", "Gagnez")}})
    new = FakeIMAP({"INBOX": {
        # Relayé par un alias Proton Pass : l'expéditeur réel est dans le nom affiché
        1: _msg('"Amazon - commande at amazon.fr" <commande_at_amazon_fr_x@passmail.net>', "Votre mot de passe a été changé",
                to="amazon.x1y2@passmail.net", date="Tue, 20 Oct 2026 09:00:00 +0200"),
        2: _msg("Ikea <noreply@ikea.com>", "Bienvenue chez IKEA", to="ikea.k9@passmail.net",
                extra={"X-SimpleLogin-Original-From": "IKEA <noreply@ikea.com>"}),
    }})
    with SessionLocal() as db:
        broker = Broker(slug="courtier-mig", name="Courtier Mig", email="privacy@courtier-mig.example", countries=["fr"], categories=[])
        profile = Profile(full_name="Moi Test", emails=["moi@gmail.com"], phones=[], addresses=[], other_names=[])
        db.add_all([broker, profile])
        db.flush()
        db.add(Request(profile_id=profile.id, broker_id=broker.id, reference="OUBLI-MIG1", status="sent"))
        g = Mailbox(label="Gmail", address="moi@gmail.com", provider="gmail", host="imap.gmail.com", security="ssl",
                    auth="password", secret_enc=crypto.encrypt("x"), folders=["INBOX", "[Gmail]/Spam"], state={})
        p = Mailbox(label="Proton", address="moi@proton.me", provider="proton", host="protonmail-bridge", port=143,
                    security="starttls", auth="password", secret_enc=crypto.encrypt("x"), folders=["INBOX"], state={})
        db.add_all([g, p])
        db.commit()
        ids = {"old": g.id, "new": p.id, "broker": broker.id}
    servers = {ids["old"]: old, ids["new"]: new}
    monkeypatch.setattr(mailboxes, "connect", lambda mb, db=None: servers[mb.id])
    yield ids
    with SessionLocal() as db:
        migration.reset(db)
        db.query(Mailbox).filter(Mailbox.id.in_([ids["old"], ids["new"]])).delete(synchronize_session=False)
        db.query(Broker).filter(Broker.id == ids["broker"]).delete()
        db.commit()


def _acc(domain):
    with SessionLocal() as db:
        acc = db.scalar(select(AccountMigration).where(AccountMigration.domain == domain))
        if acc:
            db.expunge(acc)
        return acc


def test_parse_recovers_real_sender_behind_an_alias():
    h = migration.parse(_msg('"Amazon - commande at amazon.fr" <commande_at_amazon_fr_x@passmail.net>', "Votre compte",
                             to="amazon.x1y2@passmail.net"))
    assert (h["sender"], h["domain"], h["account"]) == ("commande@amazon.fr", "amazon.fr", True)
    assert h["recipients"] == ["amazon.x1y2@passmail.net"]
    assert migration.is_personal_domain("hotmail.fr") and migration.is_personal_domain("gmail.com")
    assert not migration.is_personal_domain("amazon.fr")


def test_inventory_lists_services_only_and_detects_migration(boxes):
    assert migration.inventory(boxes["old"]) == 3       # 2 Amazon + Le Monde ; personnes, moi, courtier et spam ignorés
    assert migration.inventory(boxes["new"]) == 2
    amazon, lemonde, ikea = _acc("amazon.fr"), _acc("lemonde.fr"), _acc("ikea.com")
    assert _acc("gmail.com") is None and _acc("courtier-mig.example") is None and _acc("casino.example") is None
    assert (amazon.old_count, amazon.old_account_count, amazon.old_address) == (2, 1, "moi@gmail.com")
    assert amazon.alias == "amazon.x1y2@passmail.net" and amazon.status == "migrated"
    assert amazon.still_writes_to_old  # la lettre du 28/10 arrive encore sur Gmail
    assert lemonde.status == "todo" and not lemonde.is_account
    assert ikea.status == "migrated" and ikea.old_count == 0 and ikea.alias == "ikea.k9@passmail.net"
    # Incrémental : un second passage ne recompte rien
    assert migration.inventory(boxes["old"]) == 0 and _acc("amazon.fr").old_count == 2
    with SessionLocal() as db:
        assert kv_get(db, f"inv:{boxes['old']}:INBOX") == "7:6"
        assert (db.get(Mailbox, boxes["old"]).state or {}) == {}  # la position de l'analyse des expéditeurs reste intacte


# ------------------------------------------------------------- SimpleLogin ----

class _SL(BaseHTTPRequestHandler):
    calls: list = []

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method):
        if self.headers.get("Authentication") != "sl-key":
            return self._send(401, {"error": "Wrong api key"})
        path = self.path.split("?")[0]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        _SL.calls.append((method, self.path, body))
        if path == "/api/user_info":
            return self._send(200, {"email": "moi@proton.me", "is_premium": True})
        if path == "/api/v2/aliases":
            if "page_id=0" not in self.path:
                return self._send(200, {"aliases": []})
            return self._send(200, {"aliases": [
                {"id": 1, "email": "netflix.ab@passmail.net", "nb_forward": 4, "note": None,
                 "latest_activity": {"action": "forward", "timestamp": 1761900000, "contact": {"email": "info@mailer.netflix.com"}}},
                {"id": 2, "email": "spotify.cd@passmail.net", "nb_forward": 0, "latest_activity": None},
                {"id": 3, "email": "x7k2@passmail.net", "nb_forward": 2, "note": "Proton Pass",
                 "latest_activity": {"action": "reply", "timestamp": 1761000000, "contact": {"email": "moi@gmail.com"}}},
                {"id": 4, "email": "ikea.zz@passmail.net", "nb_forward": 1,
                 "latest_activity": {"action": "forward", "timestamp": 1761800000, "contact": {"email": "noreply@ikea.com"}}},
            ]})
        if path == "/api/aliases/3/activities":
            return self._send(200, {"activities": [{"action": "forward", "from": "hello@deezer.com", "to": "x7k2@passmail.net",
                                                    "timestamp": 1761500000}]})
        if path.startswith("/api/aliases/") and path.endswith("/activities"):
            return self._send(200, {"activities": []})
        if path == "/api/v5/alias/options":
            if "hostname=plein.example" in self.path:
                return self._send(200, {"can_create": False, "suffixes": []})
            return self._send(200, {"can_create": True, "prefix_suggestion": "lemonde", "suffixes": [
                {"suffix": ".mot@aleeas.com", "signed_suffix": "s-aleeas", "is_custom": False, "is_premium": False},
                {"suffix": ".xyz@passmail.net", "signed_suffix": "s-pass", "is_custom": False, "is_premium": False}]})
        if path == "/api/v2/mailboxes":
            return self._send(200, {"mailboxes": [{"id": 5, "email": "autre@proton.me", "default": False},
                                                  {"id": 8, "email": "moi@proton.me", "default": True}]})
        if path == "/api/v3/alias/custom/new":
            return self._send(201, {"id": 99, "email": body["alias_prefix"] + ".xyz@passmail.net"})
        return self._send(404, {"error": "not found"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def log_message(self, *a):
        pass


@pytest.fixture
def sl(client, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _SL)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(simplelogin, "ALLOW_PRIVATE", True)
    monkeypatch.setattr(settings, "simplelogin_url", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(settings, "simplelogin_api_key", "sl-key")
    _SL.calls = []
    with SessionLocal() as db:
        for domain in ("netflix.com", "spotify.com", "deezer.com", "lemonde.fr", "plein.example"):
            db.add(AccountMigration(domain=domain, old_count=3, old_account_count=1, new_count=0, status="todo"))
        db.commit()
        db.get(AccountMigration, db.scalar(select(AccountMigration.id).where(AccountMigration.domain == "deezer.com")))
        deezer = db.scalar(select(AccountMigration).where(AccountMigration.domain == "deezer.com"))
        deezer.alias, deezer.alias_id, deezer.alias_source = "x7k2@passmail.net", 3, "manual"
        deezer.refresh_status()
        db.commit()
    yield _SL
    srv.shutdown()
    with SessionLocal() as db:
        migration.reset(db)


def test_sync_links_aliases_and_detects_migrated_accounts(sl):
    summary = simplelogin.sync()
    assert summary.startswith("4 alias lus")
    netflix, spotify, deezer, ikea = (_acc(d) for d in ("netflix.com", "spotify.com", "deezer.com", "ikea.com"))
    assert (netflix.alias, netflix.status, netflix.new_count) == ("netflix.ab@passmail.net", "migrated", 4)
    assert (spotify.alias, spotify.alias_source, spotify.status) == ("spotify.cd@passmail.net", "guessed", "alias")
    assert deezer.status == "migrated" and deezer.new_first_seen is not None  # vu dans l'historique de l'alias
    assert ikea.status == "migrated" and ikea.old_count == 0                 # service qui n'écrit qu'à l'alias
    assert all(method == "GET" for method, _, _ in sl.calls)  # la synchronisation ne crée ni ne modifie rien


def test_create_alias_prefers_proton_pass_domain(sl):
    with SessionLocal() as db:
        acc = db.scalar(select(AccountMigration).where(AccountMigration.domain == "lemonde.fr"))
        assert simplelogin.create_alias(acc) == "lemonde.xyz@passmail.net"
        db.commit()
    created = [c for c in sl.calls if c[1].startswith("/api/v3/alias/custom/new")][0]
    assert "hostname=lemonde.fr" in created[1]
    assert created[2] == {"alias_prefix": "lemonde", "signed_suffix": "s-pass", "mailbox_ids": [8],
                          "note": "Créé par Oubliez-moi pour lemonde.fr"}
    acc = _acc("lemonde.fr")
    assert (acc.alias_source, acc.alias_id, acc.status) == ("created", 99, "alias")


def test_simplelogin_errors_are_explicit(sl, monkeypatch):
    with SessionLocal() as db:
        acc = db.scalar(select(AccountMigration).where(AccountMigration.domain == "plein.example"))
        with pytest.raises(simplelogin.SimpleLoginError, match="Limite d'alias"):
            simplelogin.create_alias(acc)
    monkeypatch.setattr(settings, "simplelogin_api_key", "mauvaise")
    with pytest.raises(simplelogin.SimpleLoginError, match="refusée"):
        simplelogin.user_info()


# ------------------------------------------------------------------ Page ----

def test_setup_page_then_start(client, boxes, monkeypatch):
    started = []
    monkeypatch.setattr(migration, "start_in_background", started.append)
    page = client.get("/migration").text
    assert "Inventorier mes comptes" in page and "Adresse à quitter" in page
    client.post("/migration/start", data={f"role_{boxes['old']}": "old", f"role_{boxes['new']}": "new"})
    assert started and set(started[0]) >= {boxes["old"], boxes["new"]}
    with SessionLocal() as db:
        assert db.get(Mailbox, boxes["new"]).migration_role == "new"


def test_account_actions(client, boxes):
    migration.inventory(boxes["old"])
    with SessionLocal() as db:
        db.merge(KV(key=migration.STARTED_KEY, value="2026-10-30"))
        db.commit()
    lemonde = _acc("lemonde.fr")
    page = client.get("/migration").text
    assert "lemonde.fr" in page and "Amazon" in page and "Saisir un alias" in page
    assert "lemonde.fr" not in client.get("/migration?kind=account").text  # lettre d'information seulement
    client.post(f"/migration/{lemonde.id}/alias", data={"alias": "pas-une-adresse"})
    assert _acc("lemonde.fr").alias is None
    client.post(f"/migration/{lemonde.id}/alias", data={"alias": "lemonde.k@passmail.net"})
    assert (_acc("lemonde.fr").status, _acc("lemonde.fr").alias_source) == ("alias", "manual")
    client.post(f"/migration/{lemonde.id}/status", data={"status": "abandoned"})
    assert _acc("lemonde.fr").status == "abandoned"
    client.post(f"/migration/{lemonde.id}/status", data={"status": ""})
    assert _acc("lemonde.fr").status == "alias"
    assert client.post(f"/migration/{lemonde.id}/status", data={"status": "nimporte"}).status_code == 400


def test_rejected_guess_is_not_proposed_again(client, sl):
    simplelogin.sync()
    spotify = _acc("spotify.com")
    client.post(f"/migration/{spotify.id}/alias", data={"alias": ""})
    assert (_acc("spotify.com").alias, _acc("spotify.com").alias_source) == (None, "rejected")
    simplelogin.sync()
    assert _acc("spotify.com").alias is None


def test_api_key_is_stored_encrypted(client, sl):
    r = client.post("/migration/simplelogin", data={"api_key": "sl-key"}, follow_redirects=True)
    assert "Connecté à SimpleLogin (moi@proton.me)" in r.text
    with SessionLocal() as db:
        raw = kv_get(db, "cfg:simplelogin_api_key")
    assert "sl-key" not in raw and crypto.decrypt(json.loads(raw)) == "sl-key"
    client.post("/migration/simplelogin", data={"clear": "1"})
    assert settings.simplelogin_api_key == ""
