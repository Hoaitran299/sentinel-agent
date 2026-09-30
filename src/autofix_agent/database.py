from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse


LOCK_NAME = "ai_fix_orchestrator_state"


@dataclass(frozen=True)
class DatabaseUrl:
    dialect: str
    host: str = ""
    port: int = 3306
    user: str = ""
    password: str = ""
    database: str = ""
    path: Optional[Path] = None

    @classmethod
    def parse(cls, value: str) -> "DatabaseUrl":
        parsed = urlparse(value)
        if parsed.scheme in {"mysql", "mysql+pymysql"}:
            database = parsed.path.lstrip("/")
            if not database:
                raise ValueError("MySQL database URL must include a database name")
            return cls(
                dialect="mysql",
                host=parsed.hostname or "127.0.0.1",
                port=parsed.port or 3306,
                user=unquote(parsed.username or ""),
                password=unquote(parsed.password or ""),
                database=database,
            )
        if parsed.scheme == "sqlite":
            return cls(dialect="sqlite", path=Path(unquote(parsed.path)))
        raise ValueError("database URL must use mysql:// or sqlite://")

    @classmethod
    def sqlite(cls, path: Path) -> "DatabaseUrl":
        return cls(dialect="sqlite", path=path)

    def describe(self) -> str:
        if self.dialect == "sqlite":
            return "sqlite:{}".format(self.path)
        return "mysql://{}@{}:{}/{}".format(self.user, self.host, self.port, self.database)


class Database:
    """Thin DB-API wrapper so StateStore can use one SQL style for MySQL and SQLite.

    Queries are written with `?` placeholders; they are rewritten to `%s` for PyMySQL.
    """

    def __init__(self, url: DatabaseUrl):
        self.url = url
        self.dialect = url.dialect
        self._in_transaction = False
        if self.dialect == "sqlite":
            assert url.path is not None
            url.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(url.path), timeout=30, isolation_level=None)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
        else:
            self.connection = self._connect_mysql(url)

    @staticmethod
    def _connect_mysql(url: DatabaseUrl) -> Any:
        try:
            import pymysql
            import pymysql.cursors
        except ImportError as error:
            raise RuntimeError("MySQL state store requires PyMySQL; run `make install`") from error

        options = dict(
            host=url.host,
            port=url.port,
            user=url.user,
            password=url.password,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
        )
        try:
            return pymysql.connect(database=url.database, **options)
        except pymysql.err.OperationalError as error:
            if error.args[0] != 1049:  # Unknown database
                raise
        bootstrap = pymysql.connect(**options)
        try:
            with bootstrap.cursor() as cursor:
                cursor.execute(
                    "CREATE DATABASE IF NOT EXISTS `{}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci".format(
                        url.database.replace("`", "")
                    )
                )
        finally:
            bootstrap.close()
        return pymysql.connect(database=url.database, **options)

    def close(self) -> None:
        self.connection.close()

    def _ensure_connected(self) -> None:
        # The watcher is long-lived; MySQL drops idle connections after wait_timeout.
        try:
            self.connection.ping(False)
        except Exception:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = self._connect_mysql(self.url)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "mysql" else sql

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        if self.dialect == "mysql" and not self._in_transaction:
            self._ensure_connected()
        cursor = self.connection.cursor()
        cursor.execute(self._sql(sql), tuple(parameters))
        return cursor

    def fetchone(self, sql: str, parameters: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        row = self.execute(sql, parameters).fetchone()
        return None if row is None else dict(row)

    def fetchall(self, sql: str, parameters: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.execute(sql, parameters).fetchall()]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialized write transaction (SQLite IMMEDIATE lock / MySQL named lock)."""
        locked = False
        if self.dialect == "mysql":
            self._ensure_connected()
            row = self.fetchone("SELECT GET_LOCK(?, 30) AS acquired", (LOCK_NAME,))
            if not row or row["acquired"] != 1:
                raise RuntimeError("could not acquire orchestrator state lock")
            locked = True
            self.execute("START TRANSACTION")
        else:
            self.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
            self.execute("COMMIT")
        except BaseException:
            self.execute("ROLLBACK")
            raise
        finally:
            self._in_transaction = False
            if locked:
                self.fetchone("SELECT RELEASE_LOCK(?) AS released", (LOCK_NAME,))

    def columns(self, table: str) -> set:
        if self.dialect == "sqlite":
            return {row["name"] for row in self.fetchall("PRAGMA table_info({})".format(table))}
        rows = self.fetchall(
            "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?",
            (table,),
        )
        return {row["name"] for row in rows}

    def insert_ignore(self, table: str, row: Dict[str, Any]) -> None:
        verb = "INSERT IGNORE" if self.dialect == "mysql" else "INSERT OR IGNORE"
        names = list(row)
        self.execute(
            "{} INTO {}({}) VALUES ({})".format(verb, table, ", ".join(names), ", ".join("?" for _ in names)),
            [row[name] for name in names],
        )


SQLITE_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS cursors (
        path TEXT PRIMARY KEY,
        inode INTEGER,
        offset INTEGER NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS incidents (
        id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        source_commit TEXT NOT NULL,
        status TEXT NOT NULL,
        occurrence_count INTEGER NOT NULL,
        threshold INTEGER NOT NULL,
        window_started_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        log_excerpt TEXT NOT NULL,
        message TEXT NOT NULL,
        top_application_frame TEXT,
        branch_name TEXT,
        commit_sha TEXT,
        report_path TEXT,
        pull_request_url TEXT,
        failure_reason TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS incidents_lookup
        ON incidents(fingerprint, source_commit, status, last_seen_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS occurrences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
        observed_at REAL NOT NULL,
        log_timestamp TEXT NOT NULL,
        excerpt TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        started_at REAL NOT NULL,
        finished_at REAL,
        analysis TEXT,
        verification TEXT,
        failure_reason TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id TEXT,
        run_id TEXT,
        stage TEXT NOT NULL,
        message TEXT NOT NULL,
        context TEXT,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS run_events_incident ON run_events(incident_id, id)",
    """
    CREATE TABLE IF NOT EXISTS settings (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
]

MYSQL_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS cursors (
        path VARCHAR(512) NOT NULL PRIMARY KEY,
        inode BIGINT NULL,
        offset BIGINT NOT NULL,
        updated_at DOUBLE NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS incidents (
        id VARCHAR(64) NOT NULL PRIMARY KEY,
        fingerprint CHAR(64) NOT NULL,
        source_commit VARCHAR(64) NOT NULL,
        status VARCHAR(32) NOT NULL,
        occurrence_count INT NOT NULL,
        threshold INT NOT NULL,
        window_started_at DOUBLE NOT NULL,
        last_seen_at DOUBLE NOT NULL,
        log_excerpt MEDIUMTEXT NOT NULL,
        message TEXT NOT NULL,
        top_application_frame VARCHAR(512) NULL,
        branch_name VARCHAR(255) NULL,
        commit_sha VARCHAR(64) NULL,
        report_path VARCHAR(1024) NULL,
        pull_request_url VARCHAR(1024) NULL,
        failure_reason TEXT NULL,
        KEY incidents_lookup (fingerprint, source_commit, status, last_seen_at),
        KEY incidents_recent (last_seen_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS occurrences (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        incident_id VARCHAR(64) NOT NULL,
        observed_at DOUBLE NOT NULL,
        log_timestamp VARCHAR(64) NOT NULL,
        excerpt MEDIUMTEXT NOT NULL,
        KEY occurrences_incident (incident_id, id),
        CONSTRAINT occurrences_incident_fk FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id VARCHAR(64) NOT NULL PRIMARY KEY,
        incident_id VARCHAR(64) NOT NULL,
        status VARCHAR(32) NOT NULL,
        started_at DOUBLE NOT NULL,
        finished_at DOUBLE NULL,
        analysis LONGTEXT NULL,
        verification LONGTEXT NULL,
        failure_reason TEXT NULL,
        KEY runs_incident (incident_id, started_at),
        CONSTRAINT runs_incident_fk FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS run_events (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        incident_id VARCHAR(64) NULL,
        run_id VARCHAR(64) NULL,
        stage VARCHAR(16) NOT NULL,
        message TEXT NOT NULL,
        context TEXT NULL,
        created_at DOUBLE NOT NULL,
        KEY run_events_incident (incident_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        name VARCHAR(64) NOT NULL PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at DOUBLE NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]
