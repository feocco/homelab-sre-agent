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


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class IncidentRecord:
    fingerprint: str
    service_name: str
    issue_repo: str
    issue_number: int | None
    issue_url: str | None
    count: int
    issue_create_claimed_at: str | None
    issue_create_failed_at: str | None
    last_comment_at: str | None
    pending_comment_count: int
    pending_comment_first_seen_at: str | None
    pending_comment_last_seen_at: str | None
    pending_comment_line: str | None
    pending_comment_bundle_reference: str | None


@dataclass(frozen=True)
class IssueClaim:
    action: str
    record: IncidentRecord


@dataclass(frozen=True)
class CommentRollup:
    count: int
    first_seen_at: str
    last_seen_at: str
    line: str
    bundle_reference: str


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
                  count INTEGER NOT NULL,
                  issue_create_claimed_at TEXT,
                  issue_create_failed_at TEXT,
                  last_comment_at TEXT,
                  pending_comment_count INTEGER NOT NULL DEFAULT 0,
                  pending_comment_first_seen_at TEXT,
                  pending_comment_last_seen_at TEXT,
                  pending_comment_line TEXT,
                  pending_comment_bundle_reference TEXT
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
            self._ensure_column("incidents", "issue_create_claimed_at", "TEXT")
            self._ensure_column("incidents", "issue_create_failed_at", "TEXT")
            self._ensure_column("incidents", "last_comment_at", "TEXT")
            self._ensure_column("incidents", "pending_comment_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("incidents", "pending_comment_first_seen_at", "TEXT")
            self._ensure_column("incidents", "pending_comment_last_seen_at", "TEXT")
            self._ensure_column("incidents", "pending_comment_line", "TEXT")
            self._ensure_column("incidents", "pending_comment_bundle_reference", "TEXT")
            self.connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        if any(str(row["name"]) == column for row in rows):
            return
        self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
                created = self.get_incident(fingerprint)
                assert created is not None
                return created

            self.connection.execute(
                "UPDATE incidents SET last_seen_at = ?, count = count + 1 WHERE fingerprint = ?",
                (timestamp, fingerprint),
            )
            self.connection.commit()
            updated = self.get_incident(fingerprint)
            assert updated is not None
            return updated

    def claim_issue_for_incident(
        self,
        *,
        fingerprint: str,
        service_name: str,
        issue_repo: str,
        now: datetime,
        claim_ttl_seconds: int,
    ) -> IssueClaim:
        with self._lock:
            timestamp = format_dt(now)
            existing = self.get_incident(fingerprint)
            if existing is None:
                self.connection.execute(
                    """
                    INSERT INTO incidents (
                      fingerprint,
                      service_name,
                      issue_repo,
                      first_seen_at,
                      last_seen_at,
                      count,
                      issue_create_claimed_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?)
                    """,
                    (fingerprint, service_name, issue_repo, timestamp, timestamp, timestamp),
                )
                self.connection.commit()
                created = self.get_incident(fingerprint)
                assert created is not None
                return IssueClaim("create_issue", created)

            self.connection.execute(
                """
                UPDATE incidents
                SET last_seen_at = ?, count = count + 1
                WHERE fingerprint = ?
                """,
                (timestamp, fingerprint),
            )
            self.connection.commit()
            updated = self.get_incident(fingerprint)
            assert updated is not None

            if updated.issue_number is not None:
                return IssueClaim("reuse_issue", updated)

            claimed_at = parse_dt(updated.issue_create_claimed_at)
            claim_expired = claimed_at is None or now - claimed_at >= timedelta(seconds=claim_ttl_seconds)
            if claim_expired:
                self.connection.execute(
                    """
                    UPDATE incidents
                    SET issue_create_claimed_at = ?, issue_create_failed_at = NULL
                    WHERE fingerprint = ?
                    """,
                    (timestamp, fingerprint),
                )
                self.connection.commit()
                claimed = self.get_incident(fingerprint)
                assert claimed is not None
                return IssueClaim("create_issue", claimed)

            return IssueClaim("issue_creation_in_flight", updated)

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

    def set_issue(self, *, fingerprint: str, issue_number: int, issue_url: str, now: datetime | None = None) -> None:
        timestamp = format_dt(now or utc_now())
        with self._lock:
            self.connection.execute(
                """
                UPDATE incidents
                SET issue_number = ?,
                    issue_url = ?,
                    issue_create_claimed_at = NULL,
                    issue_create_failed_at = NULL,
                    last_comment_at = COALESCE(last_comment_at, ?)
                WHERE fingerprint = ?
                """,
                (issue_number, issue_url, timestamp, fingerprint),
            )
            self.connection.commit()

    def fail_issue_claim(self, *, fingerprint: str, now: datetime) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE incidents
                SET issue_create_claimed_at = NULL, issue_create_failed_at = ?
                WHERE fingerprint = ?
                """,
                (format_dt(now), fingerprint),
            )
            self.connection.commit()

    def record_pending_comment(
        self,
        *,
        fingerprint: str,
        now: datetime,
        line: str,
        bundle_reference: str,
        cooldown_seconds: int,
    ) -> CommentRollup | None:
        timestamp = format_dt(now)
        with self._lock:
            self.connection.execute(
                """
                UPDATE incidents
                SET pending_comment_count = pending_comment_count + 1,
                    pending_comment_first_seen_at = COALESCE(pending_comment_first_seen_at, ?),
                    pending_comment_last_seen_at = ?,
                    pending_comment_line = ?,
                    pending_comment_bundle_reference = ?
                WHERE fingerprint = ?
                """,
                (timestamp, timestamp, line, bundle_reference, fingerprint),
            )
            self.connection.commit()
            record = self.get_incident(fingerprint)
            if record is None or record.issue_number is None:
                return None
            last_comment_at = parse_dt(record.last_comment_at)
            if last_comment_at is not None and now - last_comment_at < timedelta(seconds=cooldown_seconds):
                return None
            if record.pending_comment_count <= 0:
                return None
            return CommentRollup(
                count=record.pending_comment_count,
                first_seen_at=record.pending_comment_first_seen_at or timestamp,
                last_seen_at=record.pending_comment_last_seen_at or timestamp,
                line=record.pending_comment_line or line,
                bundle_reference=record.pending_comment_bundle_reference or bundle_reference,
            )

    def mark_comment_sent(self, *, fingerprint: str, now: datetime) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE incidents
                SET last_comment_at = ?,
                    pending_comment_count = 0,
                    pending_comment_first_seen_at = NULL,
                    pending_comment_last_seen_at = NULL,
                    pending_comment_line = NULL,
                    pending_comment_bundle_reference = NULL
                WHERE fingerprint = ?
                """,
                (format_dt(now), fingerprint),
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
        issue_create_claimed_at=str(row["issue_create_claimed_at"]) if row["issue_create_claimed_at"] else None,
        issue_create_failed_at=str(row["issue_create_failed_at"]) if row["issue_create_failed_at"] else None,
        last_comment_at=str(row["last_comment_at"]) if row["last_comment_at"] else None,
        pending_comment_count=int(row["pending_comment_count"] or 0),
        pending_comment_first_seen_at=str(row["pending_comment_first_seen_at"])
        if row["pending_comment_first_seen_at"]
        else None,
        pending_comment_last_seen_at=str(row["pending_comment_last_seen_at"]) if row["pending_comment_last_seen_at"] else None,
        pending_comment_line=str(row["pending_comment_line"]) if row["pending_comment_line"] else None,
        pending_comment_bundle_reference=str(row["pending_comment_bundle_reference"])
        if row["pending_comment_bundle_reference"]
        else None,
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
