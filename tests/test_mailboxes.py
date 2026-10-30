import json
import threading
import time
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import select

from app import confirm, crypto, mailboxes, oauth_ms
from app.db import Mailbox, Sender, SessionLocal


def _msg(frm, subject, unsub=None, post=False, list_id=None, date="Mon, 26 Oct 2026 10:00:00 +0100"):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "moi@exemple.fr"
    m["Subject"] = subject
    m["Date"] = date
    if unsub:
        m["List-Unsubscribe"] = unsub
    if post:
        m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if list_id:
        m["List-Id"] = list_id
    m.set_content("corps du message, jamais lu")
    return bytes(m)


class FakeIMAP:
    """Serveur IMAP minimal en mémoire : SELECT, UID SEARCH/FETCH/MOVE, CREATE."""
    capabilities = ("IMAP4REV1", "MOVE", "UIDPLUS")

    def __init__(self, folders):
        self.folders = folders  # {nom: {uid: bytes}}
        self.current = None
        self.untagged_responses = {}
        self.created = []

    def select(self, name, readonly=False):
        name = name.strip('"')
        if name not in self.folders:
            return "NO", [b""]
        self.current = name
        self.untagged_responses = {"UIDVALIDITY": [b"42"]}
        return "OK", [str(len(self.folders[name])).encode()]

    def create(self, name):
        name = name.strip('"')
        self.folders.setdefault(name, {})
        self.created.append(name)
        return "OK", [b""]

    def uid(self, cmd, *args):
        box = self.folders[self.current]
        if cmd == "SEARCH":
            crit = [a for a in args if a is not None]
            if crit[0] == "UID":
                start = int(crit[1].split(":")[0])
                uids = [u for u in box if u >= start] or ([max(box)] if box else [])
            elif crit[0] == "FROM":
                who = crit[1].strip('"')
                uids = [u for u, raw in box.items() if who.encode() in raw.split(b"\n\n")[0]]
            else:
                uids = list(box)
            return "OK", [" ".join(map(str, sorted(uids))).encode()]
        if cmd == "FETCH":
            out = []
            for u in map(int, args[0].split(",")):
                head = box[u].split(b"\n\n")[0] + b"\n\n"
                out += [(f"{u} (UID {u} BODY[HEADER.FIELDS] {{{len(head)}}}".encode(), head), b")"]
            return "OK", out
        if cmd == "MOVE":
            target = args[1].strip('"')
            for u in map(int, args[0].split(",")):
                self.folders.setdefault(target, {})[u + 1000] = box.pop(u)
            return "OK", [b""]
        raise AssertionError(cmd)

    def list(self):
        return "OK", [f'(\\HasNoChildren) "/" "{f}"'.encode() for f in self.folders]

    def logout(self):
        return "BYE", [b""]


@pytest.fixture()
def fake_box(monkeypatch):
    server = FakeIMAP({
        "INBOX": {
            1: _msg("Promo Shop <news@shop.example>", "Soldes -50 %", "<https://unsub.shop.example/u?t=1>, <mailto:unsub@shop.example>", post=True),
            2: _msg("Promo Shop <news@shop.example>", "Derniers jours !", "<https://unsub.shop.example/u?t=1>", post=True,
                    date="Wed, 28 Oct 2026 10:00:00 +0100"),
            3: _msg("Maman <maman@exemple.fr>", "Dimanche ?"),  # e-mail personnel : ignoré
            4: _msg("Club <club@asso.example>", "Lettre du club", "<https://asso.example/desinscription>"),
            5: _msg("Liste <liste@forum.example>", "Digest", list_id="<forum.example>"),
        },
        "[Gmail]/Spam": {7: _msg("Casino <win@casino.example>", "Gagnez", "<mailto:stop@casino.example?subject=stop>")},
    })
    monkeypatch.setattr(mailboxes, "connect", lambda mb, db=None: server)
    with SessionLocal() as db:
        mb = Mailbox(label="Gmail test", address="moi@exemple.fr", provider="gmail", host="imap.gmail.com", port=993,
                     security="ssl", auth="password", secret_enc=crypto.encrypt("app-password"),
                     folders=["INBOX", "[Gmail]/Spam"], state={}, file_folder="Newsletters")
        db.add(mb)
        db.commit()
        return server, mb.id


def test_scan_finds_bulk_senders_only(client, fake_box):
    server, mid = fake_box
    assert mailboxes.scan(mid) == 5  # 2 + 1 + 1 messages en masse dans INBOX, 1 dans le spam
    with SessionLocal() as db:
        senders = {s.address: s for s in db.scalars(select(Sender).where(Sender.mailbox_id == mid))}
        assert set(senders) == {"news@shop.example", "club@asso.example", "liste@forum.example", "win@casino.example"}
        shop = senders["news@shop.example"]
        assert shop.count == 2 and shop.unsub_kind == "one_click" and shop.last_subject == "Derniers jours !"
        assert senders["club@asso.example"].unsub_kind == "link"
        assert senders["win@casino.example"].unsub_kind == "mailto"
        assert senders["liste@forum.example"].unsub_kind == "none"
        mb = db.get(Mailbox, mid)
        assert mb.last_scan_at and not mb.last_error and mb.state["INBOX"] == "42:5"
        assert db.get(Mailbox, mid).secret_enc.startswith("enc:v1:")  # mot de passe chiffré
    # Analyse incrémentale : rien de nouveau, rien de recompté
    assert mailboxes.scan(mid) == 0
    server.folders["INBOX"][6] = _msg("Promo Shop <news@shop.example>", "Nouveau", "<https://unsub.shop.example/u?t=1>", post=True,
                                      date="Thu, 29 Oct 2026 10:00:00 +0100")
    assert mailboxes.scan(mid) == 1
    with SessionLocal() as db:
        assert db.scalars(select(Sender).where(Sender.mailbox_id == mid, Sender.address == "news@shop.example")).one().count == 3


def test_one_click_unsubscribe_and_manual_cases(client, fake_box, monkeypatch):
    server, mid = fake_box
    mailboxes.scan(mid)
    posted = []
    monkeypatch.setattr(confirm, "_is_public", lambda host: host != "10.0.0.1")
    monkeypatch.setattr(mailboxes, "_post_one_click", lambda url: posted.append(url) or 200)
    with SessionLocal() as db:
        shop = db.scalars(select(Sender).where(Sender.mailbox_id == mid, Sender.address == "news@shop.example")).one()
        shop_url = shop.unsub_url
        shop.unsub_url = "https://10.0.0.1/u"  # adresse interne : refusée (protection SSRF)
        assert mailboxes.unsubscribe(shop)[0] == "failed"
        shop.unsub_url = shop_url
        db.commit()
        ids = [s.id for s in db.scalars(select(Sender).where(Sender.mailbox_id == mid))]
    client.post("/senders/action", data={"action": "unsubscribe", "ids": ids, "next": "/mailboxes"})
    for _ in range(100):
        if all(j["finished"] for j in mailboxes.jobs.values()):
            break
        time.sleep(0.05)
    with SessionLocal() as db:
        st = {s.address: s.status for s in db.scalars(select(Sender).where(Sender.mailbox_id == mid))}
    assert posted == ["https://unsub.shop.example/u?t=1"]
    assert st["news@shop.example"] == "unsubscribed"
    assert st["club@asso.example"] == "manual" and st["win@casino.example"] == "manual"
    assert st["liste@forum.example"] == "failed"
    page = client.get(f"/mailboxes?status=manual&mailbox={mid}").text
    assert "Ouvrir la page" in page and "mailto:stop@casino.example" in page


def test_file_messages_moves_to_folder(client, fake_box):
    server, mid = fake_box
    mailboxes.scan(mid)
    with SessionLocal() as db:
        mb = db.get(Mailbox, mid)
        shop = db.scalars(select(Sender).where(Sender.mailbox_id == mid, Sender.address == "news@shop.example")).one()
        assert mailboxes.file_sender_messages(db, mb, [shop]) == 2
        assert shop.filed == 2
    assert "Newsletters" in server.created and len(server.folders["Newsletters"]) == 2
    assert len(server.folders["INBOX"]) == 3  # les autres messages restent


def test_keep_and_reset_and_pages(client, fake_box):
    server, mid = fake_box
    mailboxes.scan(mid)
    with SessionLocal() as db:
        sid = db.scalars(select(Sender).where(Sender.mailbox_id == mid, Sender.address == "club@asso.example")).one().id
    client.post("/senders/action", data={"action": "keep", "ids": [sid]})
    with SessionLocal() as db:
        assert db.get(Sender, sid).status == "kept"
    client.post("/senders/action", data={"action": "reset", "ids": [sid]})
    with SessionLocal() as db:
        assert db.get(Sender, sid).status == "new"
    page = client.get(f"/mailboxes?mailbox={mid}").text
    assert "Promo Shop" in page and "en un clic" in page and "maman@exemple.fr" not in page
    assert client.get(f"/mailboxes/{mid}/edit").status_code == 200


def test_mailbox_form_encrypts_password(client):
    r = client.post("/mailboxes", data={"provider": "proton", "address": "moi@proton.me", "host": "172.17.0.1", "port": "1143",
                                        "security": "starttls", "password": "bridge-pass", "folders": "INBOX, Spam",
                                        "months": "6", "file_folder": "Newsletters"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        mb = db.scalars(select(Mailbox).where(Mailbox.address == "moi@proton.me")).one()
        assert mb.secret_enc.startswith("enc:v1:") and crypto.decrypt(mb.secret_enc) == "bridge-pass"
        assert mb.verify_tls is False and mb.months == 6 and mb.auth == "password"


class _MS(BaseHTTPRequestHandler):
    polls = 0

    def do_POST(self):
        data = dict(x.split("=", 1) for x in self.rfile.read(int(self.headers["Content-Length"])).decode().split("&"))
        if self.path.endswith("/devicecode"):
            assert "IMAP.AccessAsUser.All" in data["scope"].replace("%2F", "/").replace("+", " ")
            body = {"device_code": "dev-123", "user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin",
                    "interval": 0, "expires_in": 900}
            code = 200
        elif data.get("grant_type", "").endswith("device_code"):
            type(self).polls += 1
            if type(self).polls < 2:
                body, code = {"error": "authorization_pending"}, 400
            else:
                body, code = {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}, 200
        else:  # refresh_token
            body, code = {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}, 200
        self.send_response(code)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):
        pass


def test_microsoft_device_code_flow(client, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _MS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(oauth_ms, "AUTHORITY", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(mailboxes, "scan_in_background", lambda mid: None)
    try:
        client.post("/mailboxes", data={"provider": "outlook", "address": "moi@hotmail.fr", "oauth_client_id": "client-1",
                                        "oauth_tenant": "consumers", "folders": "INBOX, Junk", "months": "12"})
        with SessionLocal() as db:
            mid = db.scalars(select(Mailbox).where(Mailbox.address == "moi@hotmail.fr")).one().id
        start = client.post(f"/mailboxes/{mid}/oauth/start").json()
        assert start["user_code"] == "ABCD-EFGH"
        assert client.get(f"/mailboxes/{mid}/oauth/poll").json()["state"] == "pending"
        assert client.get(f"/mailboxes/{mid}/oauth/poll").json()["state"] == "ok"
        with SessionLocal() as db:
            mb = db.get(Mailbox, mid)
            assert crypto.decrypt(mb.secret_enc) == "rt-1"
        oauth_ms.forget(mid)  # jeton d'accès expiré : renouvellement via le jeton de rafraîchissement
        token, new_refresh = oauth_ms.access_token(mid, "client-1", "consumers", "rt-1")
        assert token == "at-2" and new_refresh == "rt-2"
        assert oauth_ms.xoauth2("moi@hotmail.fr", "at-2") == b"user=moi@hotmail.fr\x01auth=Bearer at-2\x01\x01"
    finally:
        srv.shutdown()


def test_one_click_redirects_are_not_followed():
    import urllib.request

    handler = mailboxes._NoRedirect()
    request = urllib.request.Request("https://unsubscribe.example/u")
    assert handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/admin") is None
