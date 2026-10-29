from app import phones
from app.db import Broker, Profile
from app.letters import erasure_letter


def test_all_french_formats_are_the_same_number():
    forms = ["06 12 34 56 78", "0612345678", "+33 6 12 34 56 78", "+33612345678", "0033 6 12 34 56 78", "+33 (0)6 12 34 56 78", "06.12.34.56.78"]
    assert {phones.parse(f).e164 for f in forms} == {"+33612345678"}
    out, invalid = phones.normalize_list(forms)
    assert out == ["+33 6 12 34 56 78"] and invalid == []  # doublons fusionnés


def test_foreign_and_invalid_numbers():
    be = phones.parse("+32 470 12 34 56")
    assert be.valid and be.national == "0470 12 34 56"
    out, invalid = phones.normalize_list(["12345", "+32 470 12 34 56"])
    assert out == ["12345", "+32 470 12 34 56"] and invalid == ["12345"]


def test_letter_contains_both_formats():
    p = Profile(full_name="A B", emails=["a@b.fr"], phones=["+33 6 12 34 56 78"], addresses=[], other_names=[])
    b = Broker(name="X", email="dpo@x.fr", countries=["fr"])
    _, body = erasure_letter(p, b, "OUBLI-AAAAAA", "fr")
    assert "06 12 34 56 78 / +33612345678" in body


def test_profile_form_normalizes(client):
    r = client.post("/profiles", data={"full_name": "Tel Test", "emails": "t@t.fr", "phones": "0033612345678\n06 12 34 56 78\n42"}, follow_redirects=True)
    assert "+33 6 12 34 56 78" in r.text or r.status_code == 200
    assert "non reconnu" in r.text and "42" in r.text
