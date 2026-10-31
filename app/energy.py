"""Vérifications de sobriété énergétique : l'appli, l'IA locale (Ollama) et le serveur qui les héberge.

Tout est en lecture seule. Les réglages du serveur se lisent dans /sys (visible en lecture seule depuis
le conteneur) ; quand une information manque, la vérification l'indique au lieu de deviner. Les
corrections proposées sont des commandes à lancer soi-même sur le serveur.
"""
from __future__ import annotations

import glob
import json
import os
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from .config import settings
from .db import Mailbox
from .net import safe_urlopen

SYSFS = os.environ.get("SYSFS_ROOT", "/sys")
OK, WARN, INFO, UNKNOWN = "ok", "warn", "info", "unknown"

TMPFILES = "/etc/tmpfiles.d/sobriete.conf"


@dataclass
class Check:
    key: str
    title: str
    status: str
    detail: str
    fix: str = ""            # commande(s) à lancer sur le serveur
    link: tuple[str, str] | None = None  # (adresse, texte) d'un réglage de l'appli
    extra: dict = field(default_factory=dict)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _sys(*parts: str) -> str:
    return os.path.join(SYSFS, *parts)


def _selected(value: str | None) -> str | None:
    """« [default] performance powersave » -> « default »."""
    if not value:
        return None
    hit = re.search(r"\[([^\]]+)\]", value)
    return hit.group(1) if hit else value.split()[0]


# --------------------------------------------------------------- appli ----

def check_imap() -> Check:
    minutes = settings.imap_poll_minutes
    if not settings.imap_configured:
        return Check("imap", "Relève des réponses", OK, "Relève désactivée : aucun réveil périodique.")
    if minutes >= 15:
        return Check("imap", "Relève des réponses", OK,
                     f"Toutes les {minutes} min : largement suffisant face au délai légal d'un mois.")
    return Check("imap", "Relève des réponses", WARN,
                 f"Toutes les {minutes} min : chaque relève réveille le processeur et le réseau ({1440 // max(1, minutes)} fois "
                 "par jour). Les courtiers ont un mois pour répondre : 15 à 30 minutes suffisent.",
                 link=("/settings#releve", "Réglages › Relève des réponses"))


def check_bridge(db) -> Check:
    uses_proton = any(mb.provider == "proton" for mb in db.scalars(select(Mailbox)))
    try:
        socket.getaddrinfo("protonmail-bridge", 143)
        running = True
    except OSError:
        running = False
    if running and not uses_proton:
        return Check("bridge", "Proton Mail Bridge", WARN,
                     "Le Bridge tourne mais aucune boîte Proton n'est configurée : il synchronise votre compte en "
                     "permanence pour rien.", fix="docker compose stop protonmail-bridge")
    if running:
        return Check("bridge", "Proton Mail Bridge", INFO,
                     "Le Bridge tourne en permanence pour lire votre boîte Proton : c'est son fonctionnement normal.")
    return Check("bridge", "Proton Mail Bridge", OK, "Pas de Bridge en service.")


# ------------------------------------------------------------- IA locale ---

def ollama_state() -> dict | None:
    """Modèles chargés par Ollama, avec la part en mémoire graphique (None si Ollama ne répond pas)."""
    if not settings.llm_url:
        return None
    try:
        with safe_urlopen(settings.llm_url.rstrip("/") + "/api/ps", timeout=3, allow_private=True) as resp:
            models = json.loads(resp.read()).get("models", [])
    except (OSError, ValueError):
        return None
    return {"models": models}


def _expires_in(model: dict) -> float | None:
    raw = model.get("expires_at") or ""
    try:
        when = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", raw.replace("Z", "+00:00")))
    except ValueError:
        return None
    return (when - datetime.now(timezone.utc)).total_seconds()


VULKAN_FIX = """# Ollama 0.12.11 ou plus récent ; pilotes Vulkan de Mesa :
sudo apt install mesa-vulkan-drivers
sudo usermod -aG render,video ollama
sudo systemctl edit ollama
#   puis ajouter ces deux lignes et enregistrer :
#   [Service]
#   Environment="OLLAMA_VULKAN=1"
sudo systemctl restart ollama
# Vérification : le journal doit mentionner « library=Vulkan »
journalctl -u ollama -n 50 | grep -i vulkan
# (Ollama en conteneur, profil « ia » : variable OLLAMA_VULKAN=1 et « devices: [/dev/dri] » dans docker-compose.yml)"""


def _amd_gpu() -> bool:
    return any((_read(os.path.join(c, "device/vendor")) or "") == "0x1002" for c in glob.glob(_sys("class/drm/card[0-9]")))


def _params(raw: str) -> str:
    """« 8.2B » -> « 8,2 milliards de paramètres »."""
    hit = re.match(r"^([\d.]+)\s*([BM])$", raw or "")
    if not hit:
        return f"{raw} paramètres" if raw else ""
    return f"{hit.group(1).replace('.', ',')} {'milliards' if hit.group(2) == 'B' else 'millions'} de paramètres"


def check_llm() -> list[Check]:
    if not settings.llm_enabled:
        return [Check("llm", "IA locale", OK, "IA désactivée : aucune consommation liée au modèle.")]
    state = ollama_state()
    if state is None:
        return [Check("llm", "IA locale", UNKNOWN, f"Ollama ne répond pas à {settings.llm_url} : impossible de vérifier.")]
    models = state["models"]
    if not models:
        return [Check("llm", "IA locale : processeur ou carte graphique ?", UNKNOWN,
                      "Aucun modèle chargé en ce moment (il est déchargé après quelques minutes sans usage, c'est bien). "
                      "Lancez le test pour savoir s'il tourne sur la carte graphique.", extra={"test": True})]
    out = []
    for m in models:
        name = m.get("name", "?")
        size, vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
        params = (m.get("details") or {}).get("parameter_size", "")
        share = vram / size if size else 0
        label = f"{name}{f' ({_params(params)})' if params else ''}"
        if share >= 0.9:
            out.append(Check("llm", "IA locale : carte graphique", OK,
                             f"{label} tourne sur la carte graphique : bien plus rapide et sobre par réponse analysée "
                             "qu'avec le processeur."))
        elif share > 0:
            out.append(Check("llm", "IA locale : en partie sur le processeur", WARN,
                             f"{label} ne tient qu'à {round(share * 100)} % dans la mémoire graphique : le reste tourne sur "
                             "le processeur, plus lent et plus gourmand. Un modèle plus petit tiendrait entièrement.",
                             link=("/settings#automatismes", "Réglages › Automatismes")))
        else:
            if _amd_gpu():
                why = ("Votre carte graphique AMD n'est pas utilisée : les Radeon anciennes (RX 5000 et avant) ne sont plus "
                       "prises en charge par ROCm, mais Ollama sait les utiliser via Vulkan, souvent plusieurs fois plus vite.")
            else:
                why = ("Si le serveur a une carte graphique AMD ou Intel, Ollama peut l'utiliser via Vulkan ; avec une carte "
                       "NVIDIA, vérifiez que son pilote est installé.")
            out.append(Check("llm", "IA locale : sur le processeur", WARN,
                             f"{label} tourne sur le processeur : chaque analyse le fait chauffer à pleine puissance. {why}",
                             fix=VULKAN_FIX))
        left = _expires_in(m)
        if left is not None and left > 3600:
            out.append(Check("keep_alive", "IA locale : mémoire libérée après usage", WARN,
                             f"{name} reste chargé en permanence (OLLAMA_KEEP_ALIVE). Il occupe la mémoire et garde la "
                             "carte graphique éveillée alors qu'il ne sert que quelques secondes par réponse reçue.",
                             fix="sudo systemctl edit ollama\n#   Environment=\"OLLAMA_KEEP_ALIVE=5m\"\nsudo systemctl restart ollama"))
        elif left is not None:
            out.append(Check("keep_alive", "IA locale : mémoire libérée après usage", OK,
                             f"{name} sera déchargé dans {max(0, round(left / 60))} min sans nouvelle analyse."))
    return out


# --------------------------------------------------------------- serveur ---

def check_cpu() -> Check:
    govs = {v for v in (_read(p) for p in glob.glob(_sys("devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor"))) if v}
    epps = {v for v in (_read(p) for p in glob.glob(_sys("devices/system/cpu/cpu[0-9]*/cpufreq/energy_performance_preference"))) if v}
    if not govs:
        return Check("cpu", "Processeur : profil d'énergie", UNKNOWN, "Réglage du processeur illisible depuis le conteneur.")
    fix = (f"echo powersave | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor\n"
           f"echo balance_power | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference\n"
           f"# Pour le garder au redémarrage :\n"
           f"echo 'w /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor - - - - powersave' | sudo tee -a {TMPFILES}\n"
           f"echo 'w /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference - - - - balance_power' | sudo tee -a {TMPFILES}")
    gov = ", ".join(sorted(govs))
    if "performance" in govs:
        return Check("cpu", "Processeur : profil d'énergie", WARN,
                     f"Gouverneur « {gov} » : le processeur reste à haute fréquence même au repos. « powersave » "
                     "(avec intel_pstate) monte en fréquence à la demande et suffit largement pour un NAS.", fix=fix)
    if epps and epps & {"performance", "balance_performance"}:
        return Check("cpu", "Processeur : profil d'énergie", INFO,
                     f"Gouverneur « {gov} », préférence « {', '.join(sorted(epps))} ». « balance_power » privilégie "
                     "l'économie avec une réactivité qui reste très correcte pour un serveur.", fix=fix)
    return Check("cpu", "Processeur : profil d'énergie", OK,
                 f"Gouverneur « {gov} »" + (f", préférence « {', '.join(sorted(epps))} »" if epps else "") + ".")


def check_gpu() -> Check | None:
    cards = [c for c in glob.glob(_sys("class/drm/card[0-9]")) if (_read(os.path.join(c, "device/vendor")) or "") == "0x1002"]
    if not cards:
        return None
    levels = {(_read(os.path.join(c, "device/power_dpm_force_performance_level")) or "?") for c in cards}
    if levels & {"high", "manual", "profile_peak", "profile_standard"}:
        return Check("gpu", "Carte graphique AMD : gestion d'énergie", WARN,
                     f"Niveau forcé à « {', '.join(sorted(levels))} » : la carte ne redescend pas au repos.",
                     fix=("echo auto | sudo tee /sys/class/drm/card*/device/power_dpm_force_performance_level\n"
                          f"echo 'w /sys/class/drm/card*/device/power_dpm_force_performance_level - - - - auto' | sudo tee -a {TMPFILES}"))
    if levels <= {"auto", "low"}:
        return Check("gpu", "Carte graphique AMD : gestion d'énergie", OK,
                     f"Niveau « {', '.join(sorted(levels))} » : la carte baisse sa fréquence quand elle ne sert pas.")
    return Check("gpu", "Carte graphique AMD : gestion d'énergie", UNKNOWN, "Réglage de la carte illisible.")


def check_aspm() -> Check:
    policy = _selected(_read(_sys("module/pcie_aspm/parameters/policy")))
    fix = ("# Dans /etc/default/grub, ajouter à GRUB_CMDLINE_LINUX_DEFAULT : pcie_aspm.policy=powersupersave\n"
           "sudo update-grub && sudo reboot\n# Si un périphérique devient instable, revenez en arrière.")
    if policy is None:
        return Check("aspm", "PCI Express : mise en veille des liaisons", UNKNOWN, "Réglage illisible depuis le conteneur.")
    if policy == "performance":
        return Check("aspm", "PCI Express : mise en veille des liaisons", WARN,
                     "ASPM désactivé (« performance ») : carte graphique, SSD NVMe et réseau gardent leurs liaisons "
                     "éveillées en permanence.", fix=fix)
    if policy == "default":
        return Check("aspm", "PCI Express : mise en veille des liaisons", INFO,
                     "Politique « default » : c'est le BIOS qui décide, souvent sans économie. « powersupersave » "
                     "endort les liaisons inactives (vérifiez aussi que l'ASPM est activé dans le BIOS).", fix=fix)
    return Check("aspm", "PCI Express : mise en veille des liaisons", OK, f"Politique « {policy} ».")


def check_sata() -> Check | None:
    pols = {v for v in (_read(p) for p in glob.glob(_sys("class/scsi_host/host*/link_power_management_policy"))) if v}
    if not pols:
        return None
    if "max_performance" in pols:
        return Check("sata", "Disques SATA : veille de la liaison", WARN,
                     "Au moins un port SATA en « max_performance » : la liaison avec le disque ne se met jamais en veille.",
                     fix=("echo med_power_with_dipm | sudo tee /sys/class/scsi_host/host*/link_power_management_policy\n"
                          f"echo 'w /sys/class/scsi_host/host*/link_power_management_policy - - - - med_power_with_dipm' | sudo tee -a {TMPFILES}"))
    return Check("sata", "Disques SATA : veille de la liaison", OK, f"Politique « {', '.join(sorted(pols))} ».")


def _block_device_of(path: str) -> str | None:
    """Disque (sda, nvme0n1…) qui porte ce dossier, d'après /sys/dev/block (None si inconnu)."""
    try:
        dev = os.stat(path).st_dev
    except OSError:
        return None
    link = _sys("dev/block", f"{os.major(dev)}:{os.minor(dev)}")
    real = os.path.realpath(link)
    if not os.path.exists(real):
        return None
    # Partition (…/sda/sda1) : on remonte au disque ; volumes logiques (dm-*) : on regarde leurs disques sous-jacents.
    if os.path.exists(os.path.join(real, "partition")):
        real = os.path.dirname(real)
    return real


def check_data_disk() -> Check | None:
    real = _block_device_of(str(settings.data_dir))
    if real is None or os.path.basename(real).startswith(("vd", "xvd", "loop", "zram")):
        return None  # disque virtuel (machine virtuelle) : le noyau le déclare souvent « rotatif » à tort
    paths = [real] + [os.path.realpath(p) for p in glob.glob(os.path.join(real, "slaves", "*"))]
    rotational = [_read(os.path.join(p, "queue/rotational")) for p in paths]
    if "1" in rotational:
        return Check("disk", "Disque des données de l'appli", WARN,
                     f"Les données sont sur un disque dur ({os.path.basename(real)}) : les écritures de l'appli (relève toutes "
                     f"les {settings.imap_poll_minutes} min, journal) l'empêchent de s'arrêter. Sur un SSD, ces écritures ne "
                     "coûtent presque rien.",
                     fix="# Déplacer le dossier de Docker (et ses volumes) vers le SSD système, ou monter le volume\n"
                         "# oubliez-moi-data sur un dossier du SSD (voir docker-compose.yml, section volumes).")
    if rotational and all(r == "0" for r in rotational if r is not None):
        return Check("disk", "Disque des données de l'appli", OK,
                     f"Données sur un SSD ({os.path.basename(real)}) : les disques durs du NAS peuvent s'endormir.")
    return None


# --------------------------------------------------------------- ensemble ---

def checks(db) -> list[Check]:
    out = [check_imap(), check_bridge(db), *check_llm(), check_cpu(), check_gpu(), check_aspm(), check_sata(), check_data_disk()]
    return [c for c in out if c is not None]


def warm_up_model() -> str:
    """Charge le modèle avec une requête minuscule, pour savoir où il tourne. Renvoie un message d'erreur, ou ""."""
    from . import llm
    try:
        llm._post("/api/generate", {"model": settings.llm_model, "prompt": "ok", "stream": False, "keep_alive": "2m",
                                    "options": {"num_predict": 1}}, 180)
    except Exception as e:  # noqa: BLE001
        return str(e)[:200] or "Ollama n'a pas répondu"
    return ""
