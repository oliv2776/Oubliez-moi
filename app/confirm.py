"""Ouverture automatique des liens de confirmation envoyés par les courtiers.

Garde-fous :
- seuls les liens http(s) du domaine du courtier (adresse e-mail ou site) ou
  d'une plateforme de gestion des demandes RGPD connue sont ouverts ;
- jamais de lien de désinscription, de connexion ou de suppression de compte ;
- jamais d'adresse du réseau local ou du serveur lui-même (protection SSRF),
  y compris après une redirection ;
- une simple requête GET, sans cookie ni JavaScript : si la page demande une
  action (bouton, formulaire), la demande reste « Action requise ».
"""
from __future__ import annotations

import html
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .mailer import registered_domain

# Plateformes utilisées par de nombreuses entreprises pour gérer les demandes RGPD.
PRIVACY_PLATFORMS = {
    "onetrust.com", "trustarc.com", "transcend.io", "datagrail.io", "ketch.com", "securiti.ai", "osano.com",
    "didomi.io", "truyo.com", "mineos.ai",
}
CONFIRM_WORDS = re.compile(
    r"confirm|verif|valid|activate|best[äa]tig|approve|consent|token=|code=|ticket", re.I)
FORBIDDEN_WORDS = re.compile(
    r"unsub|d[ée]sinscri|desabonn|opt-?out|abmeld|login|sign-?in|signin|password|mot-de-passe|delete-account|close-account|"
    # Un lien « annuler / retirer ma demande » ne doit jamais être suivi automatiquement.
    r"cancel|annul|revoke|r[ée]voqu|withdraw|retir|retract|reject|refus|decline|stop-request|storn|widerruf|abbrech|zur[üu]ckzieh|"
    r"not-?me|pas-moi|report|signaler|abuse|fraud", re.I)
STRONG_CONFIRM = re.compile(r"confirm|verif|valid|best[äa]tig|approve", re.I)
SUCCESS_RE = re.compile(
    r"(request|e-?mail|address|identity) (has been |was |is )?(confirmed|verified|validated)|successfully (confirmed|verified|submitted)|"
    r"thank you for (confirming|verifying)|confirmation (successful|complete)|"
    r"(demande|adresse|identit[ée]) (a [ée]t[ée] |est )?(bien )?(confirm[ée]e|valid[ée]e|v[ée]rifi[ée]e)|merci (pour votre|de votre) confirmation|"
    r"erfolgreich best[äa]tigt|wurde best[äa]tigt", re.I)
ACTION_RE = re.compile(r"<form\b|<button\b|type=[\"']?submit", re.I)

ALLOW_PRIVATE = False  # uniquement pour les tests


@dataclass
class ClickResult:
    ok: bool
    url: str
    detail: str


def _host_allowed(host: str, broker_domains: set[str]) -> bool:
    dom = registered_domain(host)
    return dom in broker_domains or dom in PRIVACY_PLATFORMS


def _is_public(host: str) -> bool:
    if ALLOW_PRIVATE:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def candidate_links(links: list[tuple[str, str]], broker_email: str | None, broker_web: str | None) -> list[str]:
    domains = {d for d in (registered_domain((broker_email or "").split("@")[-1]), registered_domain(broker_web or "")) if d}
    out = []
    for url, label in links:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        if not _host_allowed(parsed.hostname, domains):
            continue
        if FORBIDDEN_WORDS.search(url) or FORBIDDEN_WORDS.search(label or ""):
            continue
        if CONFIRM_WORDS.search(url) or CONFIRM_WORDS.search(label or ""):
            # Priorité aux liens dont le texte ou l'adresse dit explicitement « confirmer / vérifier »
            strong = bool(STRONG_CONFIRM.search(label or "")) * 2 + bool(STRONG_CONFIRM.search(url))
            out.append((strong, url))
    out.sort(key=lambda x: -x[0])
    return [u for _, u in out[:2]]


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlparse(newurl).hostname or ""
        if not _is_public(host):
            raise urllib.error.URLError(f"redirection vers une adresse interne refusée ({host})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def click(url: str) -> ClickResult:
    host = urllib.parse.urlparse(url).hostname or ""
    if not _is_public(host):
        return ClickResult(False, url, f"adresse non publique refusée ({host})")
    opener = urllib.request.build_opener(_SafeRedirect())
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    })
    try:
        with opener.open(req, timeout=20) as resp:
            status = resp.status
            page = resp.read(500_000).decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return ClickResult(False, url, f"le site a répondu {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return ClickResult(False, url, f"lien inaccessible : {getattr(e, 'reason', e)}")
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    title = " ".join(html.unescape(title.group(1)).split())[:120] if title else ""
    text = re.sub(r"<[^>]+>", " ", page)
    if 200 <= status < 300 and SUCCESS_RE.search(text):
        return ClickResult(True, url, f"page de confirmation affichée{f' (« {title} »)' if title else ''}")
    if ACTION_RE.search(page):
        return ClickResult(False, url, f"la page demande une action (bouton ou formulaire){f' : « {title} »' if title else ''}")
    return ClickResult(False, url, f"page ouverte (HTTP {status}) mais confirmation non détectée{f' : « {title} »' if title else ''}")
