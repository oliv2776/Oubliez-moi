"""Normalisation des numéros de téléphone (06…, +33 6…, 0033 6…, +33 (0)6…).

Les courtiers stockent les numéros sous des formes variées : on les écrit dans
les courriers à la fois au format national et au format international pour
maximiser les chances qu'ils retrouvent la personne.
"""
from __future__ import annotations

from dataclasses import dataclass

import phonenumbers
from phonenumbers import PhoneNumberFormat

from .config import settings


@dataclass
class Phone:
    raw: str
    valid: bool
    national: str = ""
    international: str = ""
    e164: str = ""

    @property
    def stored(self) -> str:
        """Forme enregistrée dans le profil : internationale si le numéro est valide."""
        return self.international if self.valid else self.raw

    @property
    def for_letter(self) -> str:
        if not self.valid:
            return self.raw
        return f"{self.national} / {self.e164}"


def parse(raw: str, region: str | None = None) -> Phone:
    raw = (raw or "").strip()
    try:
        num = phonenumbers.parse(raw, (region or settings.phone_region).upper())
    except phonenumbers.NumberParseException:
        return Phone(raw, False)
    if not phonenumbers.is_valid_number(num):
        return Phone(raw, False)
    return Phone(
        raw,
        True,
        national=phonenumbers.format_number(num, PhoneNumberFormat.NATIONAL),
        international=phonenumbers.format_number(num, PhoneNumberFormat.INTERNATIONAL),
        e164=phonenumbers.format_number(num, PhoneNumberFormat.E164),
    )


def normalize_list(values: list[str]) -> tuple[list[str], list[str]]:
    """Renvoie (numéros normalisés sans doublons, numéros non reconnus)."""
    out, invalid, seen = [], [], set()
    for v in values:
        p = parse(v)
        key = p.e164 or p.raw
        if key in seen:
            continue
        seen.add(key)
        out.append(p.stored)
        if not p.valid:
            invalid.append(p.raw)
    return out, invalid
