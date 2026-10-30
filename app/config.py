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
    # Identifiant exact du serveur de confiance dans Authentication-Results (ex. mx.example.net).
    imap_authserv_id: str = field(default_factory=lambda: _str("IMAP_AUTHSERV_ID").lower())
    # Rangement des e-mails des courtiers dans la boîte : off | move | label (libellé Gmail)
    imap_file_mode: str = field(default_factory=lambda: _str("IMAP_FILE_MODE", "off").lower())
    imap_file_folder: str = field(default_factory=lambda: _str("IMAP_FILE_FOLDER", "Oubliez-moi"))
    imap_mark_read: bool = field(default_factory=lambda: _bool("IMAP_MARK_READ", False))
    imap_file_own_copies: bool = field(default_factory=lambda: _bool("IMAP_FILE_OWN_COPIES", True))

    # Automatismes
    auto_replies: bool = field(default_factory=lambda: _bool("AUTO_REPLIES", True))
    auto_formal_notice: bool = field(default_factory=lambda: _bool("AUTO_FORMAL_NOTICE", True))
    formal_notice_wait_days: int = field(default_factory=lambda: _int("FORMAL_NOTICE_WAIT_DAYS", 15))
    auto_confirm_links: bool = field(default_factory=lambda: _bool("AUTO_CONFIRM_LINKS", True))
    auto_supplements: bool = field(default_factory=lambda: _bool("AUTO_SUPPLEMENTS", True))
    residence_country: str = field(default_factory=lambda: _str("RESIDENCE_COUNTRY", "France"))

    # IA locale (Ollama) pour les réponses ambiguës
    llm_url: str = field(default_factory=lambda: _str("LLM_URL"))
    llm_model: str = field(default_factory=lambda: _str("LLM_MODEL", "tev1:4b"))
    # decision : modèle de décision via /v1/systemone (Tev1, Nimble) ; chat : modèle conversationnel via /api/chat
    llm_mode: str = field(default_factory=lambda: _str("LLM_MODE", "decision").lower())

    # Sauvegardes (dossier monté depuis le pool ZFS, cf. docker-compose.yml)
    backup_dir: Path = field(default_factory=lambda: Path(_str("BACKUP_DIR", "/backups")))
    backup_schedule: str = field(default_factory=lambda: _str("BACKUP_SCHEDULE", "daily").lower())  # off | daily | weekly
    backup_keep: int = field(default_factory=lambda: _int("BACKUP_KEEP", 14))
    backup_encrypt: bool = field(default_factory=lambda: _bool("BACKUP_ENCRYPT", True))

    # Notifications
    app_url: str = field(default_factory=lambda: _str("APP_URL"))
    notify_ntfy_url: str = field(default_factory=lambda: _str("NOTIFY_NTFY_URL"))
    notify_ntfy_token: str = field(default_factory=lambda: _str("NOTIFY_NTFY_TOKEN"))
    notify_gotify_url: str = field(default_factory=lambda: _str("NOTIFY_GOTIFY_URL"))
    notify_gotify_token: str = field(default_factory=lambda: _str("NOTIFY_GOTIFY_TOKEN"))
    notify_email_digest: bool = field(default_factory=lambda: _bool("NOTIFY_EMAIL_DIGEST", False))

    # Migration des comptes vers des alias Proton Pass / SimpleLogin
    simplelogin_api_key: str = field(default_factory=lambda: _str("SIMPLELOGIN_API_KEY"))
    simplelogin_url: str = field(default_factory=lambda: _str("SIMPLELOGIN_URL", "https://app.simplelogin.io"))

    # Comportement
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    send_per_hour: int = field(default_factory=lambda: max(1, _int("SEND_PER_HOUR", 30)))
    response_days: int = field(default_factory=lambda: _int("RESPONSE_DAYS", 30))
    auto_reminder: bool = field(default_factory=lambda: _bool("AUTO_REMINDER", True))
    reminder_wait_days: int = field(default_factory=lambda: _int("REMINDER_WAIT_DAYS", 15))
    resend_interval_days: int = field(default_factory=lambda: _int("RESEND_INTERVAL_DAYS", 90))
    # Nombre maximal de cycles automatiques, cycle initial compris.
    max_automatic_resend_cycles: int = field(default_factory=lambda: max(1, _int("MAX_AUTOMATIC_RESEND_CYCLES", 3)))
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
    def llm_enabled(self) -> bool:
        return bool(self.llm_url and self.llm_model)

    @property
    def notify_enabled(self) -> bool:
        return bool(self.notify_ntfy_url or (self.notify_gotify_url and self.notify_gotify_token))

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
    "imap_file_mode": str,
    "imap_file_folder": str,
    "imap_mark_read": bool,
    "imap_file_own_copies": bool,
    "imap_authserv_id": str,
    "auto_replies": bool,
    "auto_formal_notice": bool,
    "auto_confirm_links": bool,
    "auto_supplements": bool,
    "residence_country": str,
    "llm_url": str,
    "llm_model": str,
    "llm_mode": str,
    "app_url": str,
    "notify_ntfy_url": str,
    "notify_ntfy_token": str,
    "notify_gotify_url": str,
    "notify_gotify_token": str,
    "simplelogin_api_key": str,
    "simplelogin_url": str,
    "notify_email_digest": bool,
    "backup_schedule": str,
    "backup_keep": int,
    "backup_encrypt": bool,
}
SECRET_KEYS = {"smtp_password", "imap_password", "notify_ntfy_token", "notify_gotify_token", "simplelogin_api_key"}


settings = Settings()
