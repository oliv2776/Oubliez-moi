"""Configuration lue depuis les variables d'environnement (fichier .env avec Docker Compose)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "oui", "on"}


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _list(name: str, default: str) -> list[str]:
    return [x.strip().lower() for x in _str(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_str("DATA_DIR", "/data")))
    secret_key: str = field(default_factory=lambda: _str("SECRET_KEY"))
    admin_user: str = field(default_factory=lambda: _str("ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: _str("ADMIN_PASSWORD"))
    timezone: str = field(default_factory=lambda: _str("TZ", "Europe/Paris"))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", False))

    # Envoi (SMTP)
    smtp_host: str = field(default_factory=lambda: _str("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_security: str = field(default_factory=lambda: _str("SMTP_SECURITY", "starttls").lower())  # starttls | ssl | none
    smtp_user: str = field(default_factory=lambda: _str("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _str("SMTP_PASSWORD"))
    smtp_from: str = field(default_factory=lambda: _str("SMTP_FROM"))
    smtp_bcc_self: bool = field(default_factory=lambda: _bool("SMTP_BCC_SELF", False))

    # Réception (IMAP)
    imap_host: str = field(default_factory=lambda: _str("IMAP_HOST"))
    imap_port: int = field(default_factory=lambda: _int("IMAP_PORT", 993))
    imap_security: str = field(default_factory=lambda: _str("IMAP_SECURITY", "ssl").lower())  # ssl | starttls | none
    imap_user: str = field(default_factory=lambda: _str("IMAP_USER"))
    imap_password: str = field(default_factory=lambda: _str("IMAP_PASSWORD"))
    imap_folders: list[str] = field(
        default_factory=lambda: [f.strip() for f in _str("IMAP_FOLDERS", "INBOX").split(",") if f.strip()]
    )
    imap_poll_minutes: int = field(default_factory=lambda: _int("IMAP_POLL_MINUTES", 10))

    # Comportement
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    send_per_hour: int = field(default_factory=lambda: max(1, _int("SEND_PER_HOUR", 30)))
    response_days: int = field(default_factory=lambda: _int("RESPONSE_DAYS", 30))
    auto_reminder: bool = field(default_factory=lambda: _bool("AUTO_REMINDER", True))
    reminder_wait_days: int = field(default_factory=lambda: _int("REMINDER_WAIT_DAYS", 15))
    resend_interval_days: int = field(default_factory=lambda: _int("RESEND_INTERVAL_DAYS", 90))
    auto_enroll_new_brokers: bool = field(default_factory=lambda: _bool("AUTO_ENROLL_NEW_BROKERS", True))
    auto_classify: bool = field(default_factory=lambda: _bool("AUTO_CLASSIFY", True))
    broker_countries: list[str] = field(default_factory=lambda: _list("BROKER_COUNTRIES", "fr,all"))
    broker_auto_update_days: int = field(default_factory=lambda: _int("BROKER_AUTO_UPDATE_DAYS", 30))
    phone_region: str = field(default_factory=lambda: _str("PHONE_REGION", "FR").upper())
    reference_prefix: str = field(default_factory=lambda: _str("REFERENCE_PREFIX", "OUBLI").upper())
    disable_scheduler: bool = field(default_factory=lambda: _bool("DISABLE_SCHEDULER", False))

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.secret_key:
            # Clé persistée pour que les sessions survivent aux redémarrages.
            key_file = self.data_dir / ".secret_key"
            if key_file.exists():
                self.secret_key = key_file.read_text().strip()
            else:
                self.secret_key = secrets.token_urlsafe(48)
                key_file.write_text(self.secret_key)
                key_file.chmod(0o600)

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'oubliez-moi.db'}"

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def imap_login(self) -> tuple[str, str]:
        """Identifiants IMAP ; à défaut, ceux du SMTP (cas le plus courant : même compte)."""
        return (self.imap_user or self.smtp_user, self.imap_password or self.smtp_password)

    @property
    def imap_configured(self) -> bool:
        user, password = self.imap_login
        return bool(self.imap_host and user and password)


# Réglages modifiables depuis l'interface web (stockés en base, prioritaires sur le .env).
EDITABLE: dict[str, type] = {
    "dry_run": bool,
    "send_per_hour": int,
    "smtp_host": str,
    "smtp_port": int,
    "smtp_security": str,
    "smtp_user": str,
    "smtp_password": str,
    "smtp_from": str,
    "smtp_bcc_self": bool,
    "imap_host": str,
    "imap_port": int,
    "imap_security": str,
    "imap_user": str,
    "imap_password": str,
    "imap_folders": list,
    "imap_poll_minutes": int,
}
SECRET_KEYS = {"smtp_password", "imap_password"}


settings = Settings()
