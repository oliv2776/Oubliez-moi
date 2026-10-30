from email.message import EmailMessage

from sqlalchemy import select

from app import mailer, mailview, service
from app.db import Broker, Event, Profile, SessionLocal

HOSTILE = (
    '<style>body{background:url(http://t/css)}</style><p style="color:red;background:url(http://t/bg)" onclick="x()">Salut '
    '<a href="javascript:alert(1)">j</a> <a href="https://ok.example/">ok</a><img src="http://t/pixel.gif"></p>'
    '<script>alert(1)</script><form action="http://t"><input name="p"></form><iframe src="http://t"></iframe>'
)


def test_sanitizer_blocks_active_and_remote_content():
    out = mailview.sanitize_html(HOSTILE)
    for bad in ("script", "onclick", "javascript:", "<img", "url(", "<form", "<input", "<iframe", "http://t"):
        assert bad not in out, bad
    assert 'style="color:red"' in out and 'href="https://ok.example/"' in out and 'target="_blank"' in out


def test_text_rendering_links_and_collapsed_quote():
    html = mailview.text_to_html("Bonjour,\nVoir https://ex.org/a.\n\nLe 29/10/2026, X a écrit :\n> citation")
    assert '<a href="https://ex.org/a"' in html and "</a>." in html
    assert "<details>" in html and "<blockquote>" in html


def test_split_headers():
    h, rest = mailview.split_headers("De : A <a@b.c>\nDate : x\nObjet : Re: test\n\nCorps")
    assert h["Objet"] == "Re: test" and rest == "Corps"
    assert mailview.split_headers("Pas d'en-tête\n\nCorps")[0] == {}


def test_sender_with_unquoted_comma():
    m = mailer.parse_mail(b"From: Acme, Inc. Privacy <dpo@acme.example>\nSubject: x\n\nhi")
    assert m.from_addr == "dpo@acme.example" and m.from_name == "Acme, Inc. Privacy"


def _reply_with_html(client):
    with SessionLocal() as db:
        p = Profile(full_name="Html Test", emails=["html@exemple.fr"], phones=[], addresses=[], other_names=[])
        db.add(p)
        db.commit()
        b = db.scalars(select(Broker).where(Broker.enabled.is_(True), Broker.email.is_not(None)).offset(7)).first()
        r = service._create_request(db, p, b, cycle=1)
        db.commit()
        service.send_initial(db, r)
        msg = EmailMessage()
        msg["From"] = b.email
        msg["Subject"] = f"Re: {r.reference}"
        msg.set_content("texte")
        msg.add_alternative(HOSTILE + "<p>Pourriez-vous préciser votre ancienne adresse ?</p>", subtype="html")
        service.process_incoming(db, mailer.parse_mail(bytes(msg)))
        ev = db.scalars(select(Event).where(Event.request_id == r.id, Event.kind == "reply")).one()
        return r.id, ev.id


def test_event_view_is_sandboxed(client):
    rid, eid = _reply_with_html(client)
    resp = client.get(f"/events/{eid}/view")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "img-src data:" in csp
    assert "Pourriez-vous" in resp.text and "<script" not in resp.text and "http://t" not in resp.text
    page = client.get(f"/requests/{rid}")
    assert f'src="/events/{eid}/view"' in page.text and "sandbox=" in page.text and "allow-scripts" not in page.text


def test_triage_page_and_next_redirect(client):
    rid, _ = _reply_with_html(client)
    page = client.get(f"/tasks?cat=replied&id={rid}")
    assert page.status_code == 200 and "Que dit-il" in page.text
    assert 'name="next" value="/tasks?cat=replied' in page.text
    r = client.post(f"/requests/{rid}/status", data={"status": "completed", "next": "/tasks?cat=replied"}, follow_redirects=False)
    assert r.headers["location"] == "/tasks?cat=replied"
    # Redirection externe refusée
    r = client.post(f"/requests/{rid}/status", data={"status": "replied", "next": "//evil.example/x"}, follow_redirects=False,
                    headers={"referer": f"/requests/{rid}"})
    assert r.headers["location"] == f"/requests/{rid}"
    for cat in ("", "needs", "overdue", "failed"):
        assert client.get(f"/tasks?cat={cat}").status_code == 200
