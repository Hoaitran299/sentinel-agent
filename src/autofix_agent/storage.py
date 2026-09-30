from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .database import MYSQL_SCHEMA, SQLITE_SCHEMA, Database, DatabaseUrl
from .log_reader import LogEvent


ACTIVE_STATUSES = ("accumulating", "queued", "running")
# A fix already exists for the fingerprint (PR open, merged but maybe not deployed, or PR
# closed by a reviewer). New occurrences are attached to it instead of starting another
# agent run, until an operator explicitly releases the incident.
# `reported` = threshold reached in report_only mode; a human decides whether to run the agent.
FIX_PENDING_STATUSES = ("waiting_for_review", "merged", "dismissed", "reported")
RELEASABLE_STATUSES = FIX_PENDING_STATUSES
REQUEUEABLE_STATUSES = ("failed", "reported")
# Columns added after the first schema version: (table, name, sqlite type, mysql type).
ADDED_COLUMNS = (
    ("incidents", "pull_request_url", "TEXT", "VARCHAR(1024) NULL"),
    ("incidents", "suppressed_count", "INTEGER NOT NULL DEFAULT 0", "INT NOT NULL DEFAULT 0"),
    ("incidents", "resolved_at", "REAL", "DOUBLE NULL"),
    ("cursors", "identity", "TEXT", "VARCHAR(512) NULL"),
)
IMPORT_TABLES = ("cursors", "incidents", "occurrences", "runs", "run_events", "settings")
MODES = ("report_and_fix", "fix_only", "report_only")


@dataclass(frozen=True)
class IncidentDecision:
    incident_id: str
    occurrence_count: int
    threshold: int
    should_trigger: bool
    suppressed: bool = False
    status: str = ""
    pull_request_url: Optional[str] = None
    suppressed_count: int = 0
    reported: bool = False


class StateStore:
    def __init__(self, target: Union[Path, DatabaseUrl]):
        url = DatabaseUrl.sqlite(target) if isinstance(target, Path) else target
        self.db = Database(url)
        self._migrate()

    @property
    def dialect(self) -> str:
        return self.db.dialect

    def describe(self) -> str:
        return self.db.url.describe()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        for statement in MYSQL_SCHEMA if self.dialect == "mysql" else SQLITE_SCHEMA:
            self.db.execute(statement)
        columns: Dict[str, set] = {}
        for table, name, sqlite_type, mysql_type in ADDED_COLUMNS:
            existing = columns.setdefault(table, self.db.columns(table))
            if name not in existing:
                column_type = mysql_type if self.dialect == "mysql" else sqlite_type
                self.db.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, name, column_type))

    # A cursor is keyed by the log source URI (a file path for local logs; never contains
    # credentials). `identity` detects rotation: inode for files, object key for S3 prefixes.

    def cursor(self, key: Union[str, Path]) -> Optional[Tuple[Optional[str], int]]:
        row = self.db.fetchone("SELECT inode, identity, offset FROM cursors WHERE path = ?", (str(key),))
        if row is None:
            return None
        identity = row["identity"] if row["identity"] is not None else (None if row["inode"] is None else str(row["inode"]))
        return identity, int(row["offset"])

    def save_cursor(self, key: Union[str, Path], identity: Optional[str], offset: int) -> None:
        identity = None if identity is None else str(identity)
        inode = int(identity) if identity is not None and identity.isdigit() else None
        if self.dialect == "mysql":
            sql = """
                INSERT INTO cursors(path, inode, identity, offset, updated_at) VALUES (?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE inode=VALUES(inode), identity=VALUES(identity), offset=VALUES(offset), updated_at=VALUES(updated_at)
            """
        else:
            sql = """
                INSERT INTO cursors(path, inode, identity, offset, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET inode=excluded.inode, identity=excluded.identity, offset=excluded.offset, updated_at=excluded.updated_at
            """
        self.db.execute(sql, (str(key)[:512], inode, identity, offset, time.time()))

    def touch_cursor(self, key: Union[str, Path]) -> None:
        """Refresh the watcher heartbeat without moving the cursor (remote sources poll slower)."""
        self.db.execute("UPDATE cursors SET updated_at=? WHERE path=?", (time.time(), str(key)[:512]))

    def reset_cursor(self, key: Union[str, Path]) -> None:
        self.db.execute("DELETE FROM cursors WHERE path = ?", (str(key),))

    # Runtime settings editable from the dashboard. Missing keys fall back to config defaults.

    def settings(self) -> Dict[str, str]:
        return {row["name"]: row["value"] for row in self.db.fetchall("SELECT name, value FROM settings")}

    def save_settings(self, values: Dict[str, Any]) -> None:
        now = time.time()
        with self.db.transaction():
            for name, value in values.items():
                if self.dialect == "mysql":
                    sql = "INSERT INTO settings(name, value, updated_at) VALUES (?, ?, ?) ON DUPLICATE KEY UPDATE value=VALUES(value), updated_at=VALUES(updated_at)"
                else:
                    sql = "INSERT INTO settings(name, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at"
                self.db.execute(sql, (name, str(value), now))

    def record_event(
        self,
        event: LogEvent,
        source_commit: str,
        threshold: int,
        window_seconds: int,
        observed_at: Optional[float] = None,
        mode: str = "report_and_fix",
    ) -> IncidentDecision:
        # Reaching the threshold queues an agent run, except in report_only mode where the
        # incident is parked as `reported` until a human starts the fix.
        reached = "reported" if mode == "report_only" else "queued"
        now = time.time() if observed_at is None else observed_at
        with self.db.transaction():
            pending = self.db.fetchone(
                """
                SELECT id, status, occurrence_count, threshold, pull_request_url, suppressed_count FROM incidents
                WHERE fingerprint = ? AND status IN ('waiting_for_review', 'merged', 'dismissed', 'reported')
                ORDER BY last_seen_at DESC LIMIT 1
                """,
                (event.fingerprint,),
            )
            if pending is not None:
                count = int(pending["occurrence_count"]) + 1
                self.db.execute(
                    "UPDATE incidents SET occurrence_count=?, suppressed_count=suppressed_count+1, last_seen_at=? WHERE id=?",
                    (count, now, pending["id"]),
                )
                self.db.execute(
                    "INSERT INTO occurrences(incident_id, observed_at, log_timestamp, excerpt) VALUES (?, ?, ?, ?)",
                    (pending["id"], now, event.timestamp, event.sanitized_excerpt),
                )
                return IncidentDecision(
                    pending["id"],
                    count,
                    int(pending["threshold"]),
                    should_trigger=False,
                    suppressed=True,
                    status=pending["status"],
                    pull_request_url=pending["pull_request_url"],
                    suppressed_count=int(pending["suppressed_count"] or 0) + 1,
                )

            row = self.db.fetchone(
                """
                SELECT * FROM incidents
                WHERE fingerprint = ? AND source_commit = ? AND status IN ('accumulating', 'queued', 'running')
                ORDER BY window_started_at DESC LIMIT 1
                """,
                (event.fingerprint, source_commit),
            )
            if row is not None and row["status"] == "accumulating" and now - row["window_started_at"] > window_seconds:
                row = None

            if row is None:
                incident_id = uuid.uuid4().hex
                count = 1
                status = reached if threshold == 1 else "accumulating"
                transitioned = status == reached
                self.db.execute(
                    """
                    INSERT INTO incidents(
                        id, fingerprint, source_commit, status, occurrence_count, threshold,
                        window_started_at, last_seen_at, log_excerpt, message, top_application_frame
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident_id,
                        event.fingerprint,
                        source_commit,
                        status,
                        count,
                        threshold,
                        now,
                        now,
                        event.sanitized_excerpt,
                        event.message,
                        event.top_application_frame,
                    ),
                )
            else:
                incident_id = row["id"]
                count = int(row["occurrence_count"]) + 1
                accumulating = row["status"] == "accumulating"
                # The current threshold setting also applies to incidents still accumulating.
                transitioned = accumulating and count >= threshold
                status = reached if transitioned else row["status"]
                row_threshold = threshold if accumulating else int(row["threshold"])
                self.db.execute(
                    "UPDATE incidents SET occurrence_count=?, last_seen_at=?, status=?, threshold=? WHERE id=?",
                    (count, now, status, row_threshold, incident_id),
                )
                threshold = row_threshold

            self.db.execute(
                "INSERT INTO occurrences(incident_id, observed_at, log_timestamp, excerpt) VALUES (?, ?, ?, ?)",
                (incident_id, now, event.timestamp, event.sanitized_excerpt),
            )
        return IncidentDecision(
            incident_id,
            count,
            threshold,
            should_trigger=transitioned and status == "queued",
            status=status,
            reported=transitioned and status == "reported",
        )

    def claim(self, incident_id: str) -> Optional[str]:
        run_id = uuid.uuid4().hex
        with self.db.transaction():
            changed = self.db.execute(
                "UPDATE incidents SET status='running' WHERE id=? AND status='queued'",
                (incident_id,),
            ).rowcount
            if changed != 1:
                return None
            self.db.execute(
                "INSERT INTO runs(id, incident_id, status, started_at) VALUES (?, ?, 'analyzing', ?)",
                (run_id, incident_id, time.time()),
            )
        return run_id

    def incident(self, incident_id: str) -> Dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if row is None:
            raise KeyError("incident not found: {}".format(incident_id))
        return row

    def update_run(self, run_id: str, status: str, **values: Any) -> None:
        allowed = {"analysis", "verification", "failure_reason", "finished_at"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("unsupported run fields: {}".format(sorted(unknown)))
        encoded = {key: json.dumps(value, sort_keys=True) if key in {"analysis", "verification"} else value for key, value in values.items()}
        assignments = ["status=?"] + ["{}=?".format(key) for key in encoded]
        parameters: List[Any] = [status] + list(encoded.values()) + [run_id]
        self.db.execute("UPDATE runs SET {} WHERE id=?".format(", ".join(assignments)), parameters)

    def complete_incident(
        self,
        incident_id: str,
        branch: str,
        commit_sha: str,
        report_path: str,
        pull_request_url: Optional[str],
    ) -> None:
        self.db.execute(
            "UPDATE incidents SET status='waiting_for_review', branch_name=?, commit_sha=?, report_path=?, pull_request_url=? WHERE id=?",
            (branch, commit_sha, report_path, pull_request_url, incident_id),
        )

    def fail_incident(
        self,
        incident_id: str,
        reason: str,
        report_path: Optional[str] = None,
        branch_name: Optional[str] = None,
    ) -> None:
        self.db.execute(
            "UPDATE incidents SET status='failed', failure_reason=?, report_path=COALESCE(?, report_path), branch_name=COALESCE(?, branch_name) WHERE id=?",
            (reason[:1000], report_path, branch_name, incident_id),
        )

    def requeue_failed(self, incident_id: str) -> bool:
        """Queue a failed incident, or a `reported` one a human decided to fix."""
        changed = self.db.execute(
            "UPDATE incidents SET status='queued', failure_reason=NULL WHERE id=? AND status IN ('failed', 'reported')",
            (incident_id,),
        ).rowcount
        return changed == 1

    def open_pull_requests(self) -> List[Dict[str, Any]]:
        return self.db.fetchall(
            "SELECT id, pull_request_url FROM incidents WHERE status='waiting_for_review' AND pull_request_url IS NOT NULL"
        )

    def set_review_outcome(self, incident_id: str, status: str) -> bool:
        if status not in {"merged", "dismissed"}:
            raise ValueError("unsupported review outcome: {}".format(status))
        changed = self.db.execute(
            "UPDATE incidents SET status=?, resolved_at=? WHERE id=? AND status='waiting_for_review'",
            (status, time.time(), incident_id),
        ).rowcount
        return changed == 1

    def release(self, incident_id: str) -> bool:
        """Stop suppressing the fingerprint so the next occurrences may start a new fix."""
        changed = self.db.execute(
            "UPDATE incidents SET status='closed', resolved_at=COALESCE(resolved_at, ?) WHERE id=? AND status IN ('waiting_for_review', 'merged', 'dismissed', 'reported')",
            (time.time(), incident_id),
        ).rowcount
        return changed == 1

    def queued(self, limit: int = 10) -> List[str]:
        rows = self.db.fetchall(
            "SELECT id FROM incidents WHERE status='queued' ORDER BY last_seen_at LIMIT ?",
            (limit,),
        )
        return [row["id"] for row in rows]

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.db.fetchall(
            """
            SELECT id, status, occurrence_count, threshold, message, top_application_frame, last_seen_at,
                   window_started_at, branch_name, commit_sha, report_path, pull_request_url, failure_reason,
                   suppressed_count, resolved_at
            FROM incidents ORDER BY last_seen_at DESC LIMIT ?
            """,
            (limit,),
        )

    def resolve_prefix(self, prefix: str) -> str:
        if not prefix or any(character not in "0123456789abcdef" for character in prefix):
            raise ValueError("incident id must be hexadecimal")
        rows = self.db.fetchall("SELECT id FROM incidents WHERE id LIKE ? LIMIT 2", (prefix + "%",))
        if len(rows) != 1:
            raise KeyError("incident prefix must match exactly one incident")
        return rows[0]["id"]

    # Timeline events written by console.stage while the watcher runs.

    def add_event(
        self,
        stage: str,
        message: str,
        context: Dict[str, Any],
        incident_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO run_events(incident_id, run_id, stage, message, context, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                incident_id,
                run_id,
                stage[:16],
                message[:4000],
                json.dumps({key: str(value) for key, value in context.items()}, sort_keys=True),
                time.time(),
            ),
        )

    def events(
        self,
        incident_id: Optional[str] = None,
        after_id: int = 0,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        where = ["id > ?"]
        parameters: List[Any] = [after_id]
        if incident_id is not None:
            where.append("incident_id = ?")
            parameters.append(incident_id)
        rows = self.db.fetchall(
            "SELECT * FROM run_events WHERE {} ORDER BY id DESC LIMIT ?".format(" AND ".join(where)),
            parameters + [limit],
        )
        for row in rows:
            row["context"] = json.loads(row["context"] or "{}")
        return list(reversed(rows))

    def occurrences(self, incident_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self.db.fetchall(
            "SELECT id, observed_at, log_timestamp, excerpt FROM occurrences WHERE incident_id=? ORDER BY id DESC LIMIT ?",
            (incident_id, limit),
        )

    def runs(self, incident_id: str) -> List[Dict[str, Any]]:
        rows = self.db.fetchall("SELECT * FROM runs WHERE incident_id=? ORDER BY started_at DESC", (incident_id,))
        for row in rows:
            for key in ("analysis", "verification"):
                row[key] = json.loads(row[key]) if row[key] else None
        return rows

    def status_counts(self) -> Dict[str, int]:
        rows = self.db.fetchall("SELECT status, COUNT(*) AS total FROM incidents GROUP BY status")
        return {row["status"]: int(row["total"]) for row in rows}

    def watcher_heartbeat(self) -> Optional[float]:
        row = self.db.fetchone("SELECT MAX(updated_at) AS updated_at FROM cursors")
        return None if row is None or row["updated_at"] is None else float(row["updated_at"])

    def snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
        return {table: self.db.fetchall("SELECT * FROM {}".format(table)) for table in IMPORT_TABLES}

    def clear(self) -> Dict[str, int]:
        """Delete incident history. Log cursors are kept so old log lines are not re-read."""
        deleted: Dict[str, int] = {}
        with self.db.transaction():
            running = self.db.fetchone("SELECT COUNT(*) AS total FROM incidents WHERE status='running'")
            if running and int(running["total"]) > 0:
                raise RuntimeError("an agent run is in progress; wait for it to finish before clearing")
            for table in ("run_events", "occurrences", "runs", "incidents"):
                deleted[table] = self.db.execute("DELETE FROM {}".format(table)).rowcount
        return deleted

    def import_from(self, source: "StateStore") -> Dict[str, int]:
        copied: Dict[str, int] = {}
        with self.db.transaction():
            for table in IMPORT_TABLES:
                rows = source.db.fetchall("SELECT * FROM {}".format(table))
                for row in rows:
                    self.db.insert_ignore(table, row)
                copied[table] = len(rows)
        return copied
