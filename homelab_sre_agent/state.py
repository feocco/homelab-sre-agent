from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import sqlite3
import threading


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class IncidentRecord:
    fingerprint: str
    service_name: str
    issue_repo: str
    issue_number: int | None
    issue_url: str | None
    count: int


@dataclass(frozen=True)
class PhoneApprovalToken:
    token: str
    fingerprint: str
    service_name: str
    issue_repo: str
    issue_number: int
    issue_url: str
    created_at: str


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                  fingerprint TEXT PRIMARY KEY,
                  service_name TEXT NOT NULL,
                  issue_repo TEXT NOT NULL,
                  issue_number INTEGER,
                  issue_url TEXT,
                  first_seen_at TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL,
                  count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dispatches (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_repo TEXT NOT NULL,
                  service_name TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS phone_approval_tokens (
                  token TEXT PRIMARY KEY,
                  fingerprint TEXT NOT NULL,
                  service_name TEXT NOT NULL,
                  issue_repo TEXT NOT NULL,
                  issue_number INTEGER NOT NULL,
                  issue_url TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  used_at TEXT
                );
                """
            )
            self.connection.commit()

    def record_seen(self, *, fingerprint: str, service_name: str, issue_repo: str, now: datetime) -> IncidentRecord:
        with self._lock:
            existing = self.get_incident(fingerprint)
            timestamp = format_dt(now)
            if existing is None:
                self.connection.execute(
                    """
                    INSERT INTO incidents (
                      fingerprint, service_name, issue_repo, first_seen_at, last_seen_at, count
                    ) VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (fingerprint, service_name, issue_repo, timestamp, timestamp),
                )
                self.connection.commit()
                return IncidentRecord(fingerprint, service_name, issue_repo, None, None, 1)

            self.connection.execute(
                "UPDATE incidents SET last_seen_at = ?, count = count + 1 WHERE fingerprint = ?",
                (timestamp, fingerprint),
            )
            self.connection.commit()
            updated = self.get_incident(fingerprint)
            assert updated is not None
            return updated

    def get_incident(self, fingerprint: str) -> IncidentRecord | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM incidents WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            return incident_from_row(row)

    def get_incident_by_issue(self, *, issue_repo: str, issue_number: int) -> IncidentRecord | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM incidents
                WHERE issue_repo = ? AND issue_number = ?
                ORDER BY last_seen_at DESC
                LIMIT 1
                """,
                (issue_repo, issue_number),
            ).fetchone()
            return incident_from_row(row)

    def recent_incident_for_service(
        self,
        *,
        service_name: str,
        issue_repo: str,
        since: datetime,
    ) -> IncidentRecord | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM incidents
                WHERE service_name = ? AND issue_repo = ? AND last_seen_at >= ?
                ORDER BY last_seen_at DESC
                LIMIT 1
                """,
                (service_name, issue_repo, format_dt(since)),
            ).fetchone()
            return incident_from_row(row)

    def set_issue(self, *, fingerprint: str, issue_number: int, issue_url: str) -> None:
        with self._lock:
            self.connection.execute(
                "UPDATE incidents SET issue_number = ?, issue_url = ? WHERE fingerprint = ?",
                (issue_number, issue_url, fingerprint),
            )
            self.connection.commit()

    def dispatch_count(self, *, source_repo: str | None = None, since: datetime) -> int:
        with self._lock:
            if source_repo is None:
                row = self.connection.execute(
                    "SELECT COUNT(*) AS count FROM dispatches WHERE created_at >= ?",
                    (format_dt(since),),
                ).fetchone()
            else:
                row = self.connection.execute(
                    "SELECT COUNT(*) AS count FROM dispatches WHERE source_repo = ? AND created_at >= ?",
                    (source_repo, format_dt(since)),
                ).fetchone()
            return int(row["count"])

    def recent_dispatch_exists(self, *, fingerprint: str, since: datetime) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM dispatches WHERE fingerprint = ? AND created_at >= ? LIMIT 1",
                (fingerprint, format_dt(since)),
            ).fetchone()
            return row is not None

    def record_dispatch(self, *, source_repo: str, service_name: str, fingerprint: str, now: datetime) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT INTO dispatches (source_repo, service_name, fingerprint, created_at) VALUES (?, ?, ?, ?)",
                (source_repo, service_name, fingerprint, format_dt(now)),
            )
            self.connection.commit()

    def create_phone_approval_token(
        self,
        *,
        fingerprint: str,
        service_name: str,
        issue_repo: str,
        issue_number: int,
        issue_url: str,
        now: datetime,
    ) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO phone_approval_tokens (
                  token, fingerprint, service_name, issue_repo, issue_number, issue_url, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (token, fingerprint, service_name, issue_repo, issue_number, issue_url, format_dt(now)),
            )
            self.connection.commit()
        return token

    def consume_phone_approval_token(
        self,
        token: str,
        *,
        now: datetime,
        max_age_seconds: int,
    ) -> PhoneApprovalToken | None:
        cutoff = format_dt(now - timedelta(seconds=max_age_seconds))
        timestamp = format_dt(now)
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM phone_approval_tokens
                WHERE token = ? AND used_at IS NULL AND created_at >= ?
                LIMIT 1
                """,
                (token, cutoff),
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                "UPDATE phone_approval_tokens SET used_at = ? WHERE token = ?",
                (timestamp, token),
            )
            self.connection.commit()
            return phone_approval_token_from_row(row)


def incident_from_row(row: sqlite3.Row | None) -> IncidentRecord | None:
    if row is None:
        return None
    return IncidentRecord(
        fingerprint=str(row["fingerprint"]),
        service_name=str(row["service_name"]),
        issue_repo=str(row["issue_repo"]),
        issue_number=int(row["issue_number"]) if row["issue_number"] is not None else None,
        issue_url=str(row["issue_url"]) if row["issue_url"] else None,
        count=int(row["count"]),
    )


def phone_approval_token_from_row(row: sqlite3.Row) -> PhoneApprovalToken:
    return PhoneApprovalToken(
        token=str(row["token"]),
        fingerprint=str(row["fingerprint"]),
        service_name=str(row["service_name"]),
        issue_repo=str(row["issue_repo"]),
        issue_number=int(row["issue_number"]),
        issue_url=str(row["issue_url"]),
        created_at=str(row["created_at"]),
    )
