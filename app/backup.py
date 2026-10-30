"""Sauvegardes de la base vers un dossier (typiquement un dataset de votre pool ZFS).

- copie cohérente faite avec l'API de sauvegarde de SQLite, même pendant que
  l'application écrit ;
- chiffrée par défaut avec la clé de chiffrement de l'application
  (fichier « .db.enc ») : une sauvegarde volée est illisible ;
- rotation : seules les N dernières sauvegardes automatiques sont gardées.

Restauration (application arrêtée) :
    docker compose stop oubliez-moi
    docker compose run --rm oubliez-moi python -m app.backup restore /backups/<fichier>
    docker compose start oubliez-moi
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import crypto
from .config import settings

log = logging.getLogger(__name__)
DB_PATH = settings.data_dir / "oubliez-moi.db"
NAME_RE = re.compile(r"^oubliez-moi-(\d{4}-\d{2}-\d{2}_\d{6})(?:-(\d+))?(-manuelle)?\.db(\.enc)?$")


class BackupError(Exception):
    pass


def _snapshot(dest: Path) -> None:
    src = sqlite3.connect(DB_PATH)
    try:
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()


def create(manual: bool = False) -> Path:
    """Crée une sauvegarde et renvoie son chemin."""
    target_dir = settings.backup_dir
    if not target_dir.is_dir():
        raise BackupError(f"Le dossier de sauvegarde {target_dir} n'existe pas : montez-le dans docker-compose.yml (BACKUP_DIR).")
    if not os.access(target_dir, os.W_OK):
        raise BackupError(f"Le dossier {target_dir} n'est pas accessible en écriture : sur le serveur, "
                          f"lancez « chown 10001 <dossier> » (utilisateur du conteneur).")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    suffix = ("-manuelle" if manual else "") + ".db" + (".enc" if settings.backup_encrypt else "")
    final, n = target_dir / f"oubliez-moi-{stamp}{suffix}", 1
    while final.exists():  # deux sauvegardes dans la même seconde : on ne remplace jamais un fichier
        n += 1
        final = target_dir / f"oubliez-moi-{stamp}-{n}{suffix}"
    with tempfile.TemporaryDirectory(dir=settings.data_dir) as tmp:
        snap = Path(tmp) / "snapshot.db"
        _snapshot(snap)
        data = snap.read_bytes()
    if settings.backup_encrypt:
        data = crypto.encrypt_bytes(data)
    partial = final.with_suffix(final.suffix + ".part")
    partial.write_bytes(data)
    partial.chmod(0o600)
    partial.rename(final)  # écriture atomique : jamais de sauvegarde à moitié écrite
    log.info("Sauvegarde créée : %s (%d Ko)", final, len(data) // 1024)
    prune()
    return final


def list_backups() -> list[dict]:
    d = settings.backup_dir
    if not d.is_dir():
        return []
    out = []
    for f in d.iterdir():
        m = NAME_RE.match(f.name)
        if m and f.is_file():
            st = f.stat()
            out.append({
                "name": f.name, "path": f, "size": st.st_size, "encrypted": bool(m.group(4)), "manual": bool(m.group(3)),
                "at": datetime.strptime(m.group(1), "%Y-%m-%d_%H%M%S"), "seq": int(m.group(2) or 1),
            })
    return sorted(out, key=lambda b: (b["at"], b["seq"]), reverse=True)


def prune() -> int:
    """Garde les N dernières sauvegardes automatiques ; les sauvegardes manuelles ne sont jamais supprimées."""
    keep = max(1, settings.backup_keep)
    autos = [b for b in list_backups() if not b["manual"]]
    removed = 0
    for b in autos[keep:]:
        try:
            b["path"].unlink()
            removed += 1
        except OSError as e:
            log.warning("Impossible de supprimer l'ancienne sauvegarde %s : %s", b["name"], e)
    return removed


def scheduled() -> str | None:
    """Appelé chaque nuit : sauvegarde selon la fréquence choisie."""
    if settings.backup_schedule not in ("daily", "weekly"):
        return None
    last = next((b for b in list_backups() if not b["manual"]), None)
    min_days = 1 if settings.backup_schedule == "daily" else 7
    if last and (datetime.now() - last["at"]).total_seconds() < min_days * 86400 - 3600:
        return None
    return f"sauvegarde {create().name}"


def read_backup(path: Path, key: str | None = None) -> bytes:
    from cryptography.fernet import Fernet, InvalidToken
    data = path.read_bytes()
    if path.name.endswith(".enc"):
        try:
            data = Fernet(crypto._from_passphrase(key)).decrypt(data) if key else crypto.decrypt_bytes(data)
        except (InvalidToken, crypto.DecryptionError) as e:
            raise BackupError("Impossible de déchiffrer la sauvegarde : vérifiez et restaurez la clé de chiffrement d'origine") from e
    if not data.startswith(b"SQLite format 3"):
        raise BackupError("Le fichier n'est pas une base SQLite valide (mauvaise clé ?)")
    return data


def restore(path: Path, key: str | None = None) -> Path:
    """Remplace la base actuelle par une sauvegarde (l'actuelle est conservée à côté)."""
    data = read_backup(path, key)
    if DB_PATH.exists():
        keep = DB_PATH.with_name(f"oubliez-moi.avant-restauration-{datetime.now():%Y%m%d-%H%M%S}.db")
        DB_PATH.rename(keep)
        for ext in ("-wal", "-shm"):
            p = DB_PATH.with_name(DB_PATH.name + ext)
            if p.exists():
                p.unlink()
    DB_PATH.write_bytes(data)
    return DB_PATH


if __name__ == "__main__":  # python -m app.backup create | list | restore <fichier> [clé]
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    try:
        if cmd == "create":
            print(create(manual=True))
        elif cmd == "list":
            for b in list_backups():
                print(f"{b['at']:%Y-%m-%d %H:%M}  {b['size'] // 1024:>8} Ko  {b['name']}")
        elif cmd == "restore" and len(sys.argv) >= 3:
            p = Path(sys.argv[2])
            if not p.is_absolute():
                p = settings.backup_dir / p
            print(f"Base restaurée depuis {p} → {restore(p, sys.argv[3] if len(sys.argv) > 3 else None)}")
            print("Redémarrez le conteneur pour prendre la restauration en compte.")
        else:
            print(__doc__)
            sys.exit(2)
    except (BackupError, OSError, ValueError) as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)
