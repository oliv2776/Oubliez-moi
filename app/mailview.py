"""Affichage lisible et sûr des e-mails.

Le HTML des réponses reçues vient de l'extérieur : il est nettoyé (nh3 : pas de
script, pas de formulaire, pas d'image, pas de CSS chargeant une ressource),
puis affiché dans un cadre isolé servi avec une politique de sécurité qui
interdit tout chargement externe. Les pixels de pistage ne peuvent donc pas
signaler au courtier que vous avez lu son message.
"""
from __future__ import annotations

import html
import re

import nh3

TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "cite", "code", "col", "colgroup", "dd", "del", "details",
    "div", "dl", "dt", "em", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "ins", "kbd", "li", "mark", "ol",
    "p", "pre", "q", "s", "small", "span", "strike", "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "tt", "u", "ul",
}
COMMON_ATTRS = {"style", "align", "valign", "width", "height", "bgcolor", "dir", "lang", "title"}
ATTRIBUTES = {
    "*": COMMON_ATTRS,
    "a": {"href"},
    "font": {"color", "size", "face"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "table": {"cellpadding", "cellspacing", "border"},
    "ol": {"start", "type"},
}
# Propriétés CSS sans possibilité de charger une ressource (pas de background, pas d'url()).
STYLE_PROPERTIES = {
    "color", "background-color", "font", "font-weight", "font-style", "font-size", "font-family", "line-height",
    "text-decoration", "text-align", "text-transform", "letter-spacing", "white-space", "vertical-align", "display",
    "width", "max-width", "min-width", "height", "margin", "margin-top", "margin-bottom", "margin-left", "margin-right",
    "padding", "padding-top", "padding-bottom", "padding-left", "padding-right", "border", "border-top", "border-bottom",
    "border-left", "border-right", "border-color", "border-width", "border-style", "border-collapse", "border-radius",
    "border-spacing",
}

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)

_BASE_CSS = """
html,body{height:auto!important;min-height:0!important}
html{background:#fff}
body{margin:0;padding:14px 16px;font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#1d232b;overflow-wrap:anywhere}
a{color:#23524a}
p{margin:0 0 .8em}
blockquote{margin:.6em 0;padding:.1em 0 .1em .9em;border-left:3px solid #d5d2c8;color:#5a616b}
pre{white-space:pre-wrap;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace}
table{max-width:100%!important}
details{margin-top:1em}
summary{cursor:pointer;color:#6a7280;font-size:13px}
.quoted{color:#5a616b}
"""

_URL_RE = re.compile(r"(https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+)")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# Début du message cité (notre demande initiale, le plus souvent)
_QUOTE_START = re.compile(
    r"^\s*(>|-{2,}\s*(original message|message d'origine|ursprüngliche nachricht|forwarded message)|"
    r"(le|on|am)\s.{5,120}\s(a écrit|wrote|schrieb)\s*:\s*$|(de|from|von)\s*:\s.+@.+)",
    re.I,
)


def sanitize_html(raw: str) -> str:
    return nh3.clean(
        raw,
        tags=TAGS,
        clean_content_tags={"script", "style", "title", "head", "noscript", "template"},
        attributes=ATTRIBUTES,
        url_schemes={"http", "https", "mailto", "tel"},
        filter_style_properties=STYLE_PROPERTIES,
        link_rel="noopener noreferrer nofollow",
        set_tag_attribute_values={"a": {"target": "_blank"}},
        strip_comments=True,
    )


def _linkify(escaped_line: str) -> str:
    def url(m):
        href = m.group(0)
        trail = ""
        while href and href[-1] in ".,;:!?":
            href, trail = href[:-1], href[-1] + trail
        target = href if href.startswith("http") else f"https://{href}"
        return f'<a href="{target}" target="_blank" rel="noopener noreferrer nofollow">{href}</a>{trail}'

    line = _URL_RE.sub(url, escaped_line)
    if "<a " not in line:
        line = _EMAIL_RE.sub(lambda m: f'<a href="mailto:{m.group(0)}">{m.group(0)}</a>', line)
    return line


def _paragraphs(lines: list[str]) -> str:
    out, para, quote = [], [], []

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_linkify(html.escape(x)) for x in para) + "</p>")
            para.clear()

    def flush_quote():
        if quote:
            out.append("<blockquote>" + _paragraphs([q[1:].lstrip() if q.startswith(">") else q for q in quote]) + "</blockquote>")
            quote.clear()

    for line in lines:
        if line.lstrip().startswith(">"):
            flush_para()
            quote.append(line.lstrip())
        elif not line.strip():
            flush_quote()
            flush_para()
        else:
            flush_quote()
            para.append(line)
    flush_quote()
    flush_para()
    return "".join(out)


def strip_quoted(text: str) -> str:
    """Retire le message cité (notre demande d'origine) d'une réponse en texte brut."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if i > 0 and _QUOTE_START.match(line):
            return "\n".join(lines[:i]).strip()
    return (text or "").strip()


def text_to_html(text: str, collapse_quote: bool = True) -> str:
    """Texte brut -> HTML : paragraphes, liens cliquables, citations repliées."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cut = None
    if collapse_quote:
        for i, line in enumerate(lines):
            if i > 0 and _QUOTE_START.match(line):
                cut = i
                break
    main = _paragraphs(lines[:cut] if cut else lines)
    if cut:
        main += '<details><summary>Afficher le message cité</summary><div class="quoted">' + _paragraphs(lines[cut:]) + "</div></details>"
    return main


def split_headers(body: str) -> tuple[dict, str]:
    """Sépare l'en-tête « De / Date / Objet » que l'on stocke devant le texte d'une réponse."""
    headers, rest = {}, body or ""
    head, sep, tail = rest.partition("\n\n")
    if sep:
        parsed = {}
        for line in head.split("\n"):
            key, colon, value = line.partition(":")
            key = key.strip()
            if not colon or key not in ("De", "Date", "Objet"):
                parsed = None
                break
            parsed[key] = value.strip()
        if parsed:
            headers, rest = parsed, tail
    return headers, rest


def document(inner_html: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8"><base target="_blank">'
        f"<style>{_BASE_CSS}</style></head><body>{inner_html}</body></html>"
    )


def render_event(body: str | None, raw_html: str | None) -> str:
    """Document HTML complet pour le cadre d'affichage d'un message."""
    _, text = split_headers(body or "")
    inner = sanitize_html(raw_html) if raw_html else text_to_html(text)
    return document(inner or "<p><em>(message vide)</em></p>")
