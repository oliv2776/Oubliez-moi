"""Modèle de données (SQLite via SQLAlchemy)."""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --- Statuts d'une demande -------------------------------------------------
QUEUED = "queued"            # en file d'attente d'envoi
SENT = "sent"                # envoyée, en attente de réponse
REPLIED = "replied"          # réponse reçue, à examiner
NEEDS_ACTION = "needs_action"  # action manuelle requise (formulaire, identité, confirmation)
COMPLETED = "completed"      # données supprimées ou inexistantes
REFUSED = "refused"          # refus du courtier
OVERDUE = "overdue"          # aucune réponse après relance -> plainte CNIL possible
FAILED = "failed"            # échec d'envoi
CANCELLED = "cancelled"

STATUS_LABELS = {
    QUEUED: "En file d'attente",
    SENT: "Envoyée – en attente",
    REPLIED: "Réponse à examiner",
    NEEDS_ACTION: "Action requise",
    COMPLETED: "Terminée",
    REFUSED: "Refusée",
    OVERDUE: "Sans réponse",
    FAILED: "Échec d'envoi",
    CANCELLED: "Annulée",
}
OPEN_STATUSES = {QUEUED, SENT, REPLIED, NEEDS_ACTION, OVERDUE, FAILED}
FINAL_STATUSES = {COMPLETED, REFUSED, CANCELLED}


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    other_names: Mapped[list] = mapped_column(JSON, default=list)
    emails: Mapped[list] = mapped_column(JSON, default=list)
    phones: Mapped[list] = mapped_column(JSON, default=list)
    addresses: Mapped[list] = mapped_column(JSON, default=list)
    birth_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    language: Mapped[str] = mapped_column(String(5), default="auto")  # auto | fr | en
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    campaign_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    requests: Mapped[list["Request"]] = relationship(back_populates="profile", cascade="all, delete-orphan")

    @property
    def primary_email(self) -> str | None:
        return self.emails[0] if self.emails else None


class Broker(Base):
    __tablename__ = "brokers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(300), nullable=True)
    webform: Mapped[str | None] = mapped_column(String(500), nullable=True)
    web: Mapped[str | None] = mapped_column(String(500), nullable=True)
    postal_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    countries: Mapped[list] = mapped_column(JSON, default=list)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    needs_id: Mapped[bool] = mapped_column(Boolean, default=False)
    quality: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="custom")  # datenanfragen | custom
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled_overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    requests: Mapped[list["Request"]] = relationship(back_populates="broker", cascade="all, delete-orphan")

    @property
    def channel(self) -> str:
        return "email" if self.email else "webform"

    @property
    def account_risk(self) -> str | None:
        from .brokers import account_risk
        return account_risk(self.slug)


class Request(Base):
    __tablename__ = "requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    broker_id: Mapped[int] = mapped_column(ForeignKey("brokers.id", ondelete="CASCADE"), index=True)
    reference: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    cycle: Mapped[int] = mapped_column(Integer, default=1)
    channel: Mapped[str] = mapped_column(String(20), default="email")
    language: Mapped[str] = mapped_column(String(5), default="fr")
    status: Mapped[str] = mapped_column(String(20), default=QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    message_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reply_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplement_pending: Mapped[bool] = mapped_column(Boolean, default=False)

    profile: Mapped[Profile] = relationship(back_populates="requests")
    broker: Mapped[Broker] = relationship(back_populates="requests")
    events: Mapped[list["Event"]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="Event.at"
    )

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(30))  # sent, reminder, reply, status, note, error, info
    summary: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    request: Mapped[Request] = relationship(back_populates="events")


class KV(Base):
    """Petit stockage clé/valeur (état IMAP, date de dernière mise à jour…)."""
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


engine = create_engine(settings.db_url, connect_args={"check_same_thread": False, "timeout": 30})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# Colonnes ajoutées après la première version : (table, colonne, définition SQL).
# SQLAlchemy ne modifie pas les tables existantes, on les ajoute donc à la main.
MIGRATIONS = [
    ("requests", "supplement_pending", "BOOLEAN NOT NULL DEFAULT 0"),
]


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, column, ddl in MIGRATIONS:
            cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def kv_get(db, key: str, default: str | None = None) -> str | None:
    row = db.get(KV, key)
    return row.value if row else default


def kv_set(db, key: str, value: str) -> None:
    row = db.get(KV, key)
    if row:
        row.value = value
    else:
        db.add(KV(key=key, value=value))


_ALPHABET = string.ascii_uppercase + string.digits


def new_reference(db) -> str:
    while True:
        ref = f"{settings.reference_prefix}-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))
        if not db.scalar(select(Request.id).where(Request.reference == ref)):
            return ref


def add_event(db, req: Request, kind: str, summary: str, body: str | None = None) -> Event:
    ev = Event(request=req, kind=kind, summary=summary[:500], body=body)
    db.add(ev)
    return ev
