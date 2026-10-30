"""Garde-fous de la traduction anglaise (app/static/i18n.js)."""
import collections
import html
import json
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
SOURCE = (APP / "static" / "i18n.js").read_text(encoding="utf-8")


def _pairs():
    start = SOURCE.index("const translations = {")
    body = SOURCE[start:SOURCE.index("};", start)]
    return [(json.loads(f'"{fr}"'), json.loads(f'"{en}"'))
            for fr, en in re.findall(r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"', body)]


def test_no_duplicate_keys():
    counts = collections.Counter(fr for fr, _ in _pairs())
    assert not [k for k, n in counts.items() if n > 1]


def test_every_key_still_exists_in_the_interface():
    """Si un texte change dans un gabarit, sa traduction doit être mise à jour (sinon elle ne s'applique plus)."""
    corpus = "".join(html.unescape(f.read_text(encoding="utf-8"))
                     for f in [*(APP / "templates").glob("*.html"), *APP.glob("*.py")])
    stale = [fr for fr, _ in _pairs() if fr not in corpus]
    assert not stale, f"Traductions orphelines (texte introuvable dans l'interface) : {stale}"


def test_translations_are_not_empty_or_identical_sentences():
    for fr, en in _pairs():
        assert en.strip(), fr
        if " " in fr.strip():
            assert en != fr, f"Phrase non traduite : {fr}"


def test_user_content_is_never_translated():
    """E-mails, courriers et texte à coller dans un formulaire doivent rester intacts."""
    skip = re.search(r'const SKIP = "([^"]+)"', SOURCE).group(1)
    for selector in ("pre", "textarea", ".mail-body", "[translate='no']"):
        assert selector in skip
