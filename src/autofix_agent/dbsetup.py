from __future__ import annotations

import getpass
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote, unquote, urlparse

from .database import DatabaseUrl


IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,64}$")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
GRANTS = "SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES"
ENV_KEY = "AI_FIX_DATABASE_URL"


@dataclass(frozen=True)
class AdminLogin:
    host: str
    port: int
    user: str
    password: str

    @classmethod
    def parse(cls, value: str) -> "AdminLogin":
        parsed = urlparse(value)
        if parsed.scheme not in {"mysql", "mysql+pymysql"}:
            raise ValueError("admin URL must look like mysql://root:password@127.0.0.1:3306")
        return cls(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=unquote(parsed.username or "root"),
            password=unquote(parsed.password or ""),
        )


def build_url(user: str, password: str, host: str, port: int, database: str) -> str:
    return "mysql://{}:{}@{}:{}/{}".format(quote(user, safe=""), quote(password, safe=""), host, port, database)


def generated_target(host: str = "127.0.0.1", port: int = 3306, database: str = "ai_fix_orchestrator", user: str = "aifix") -> str:
    return build_url(user, secrets.token_urlsafe(24), host, port, database)


def user_hosts(target: DatabaseUrl) -> List[str]:
    # A TCP login from 127.0.0.1 may be matched as 'localhost' or '127.0.0.1' depending on
    # the server's name resolution, so create both for local servers.
    override = os.environ.get("AI_FIX_DB_USER_HOST", "").strip()
    if override:
        return [override]
    return ["127.0.0.1", "localhost"] if target.host in LOOPBACK else ["%"]


def init_database(admin: AdminLogin, target: DatabaseUrl, hosts: Optional[List[str]] = None) -> List[str]:
    """Create the orchestrator database/user and grant it access. Safe to run repeatedly."""
    if target.dialect != "mysql":
        raise ValueError("{} must be a mysql:// URL".format(ENV_KEY))
    for name, value in (("database", target.database), ("user", target.user)):
        if not IDENTIFIER.match(value):
            raise ValueError("{} name may only contain letters, digits and underscore".format(name))

    import pymysql

    connection = pymysql.connect(
        host=admin.host,
        port=admin.port,
        user=admin.user,
        password=admin.password,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
    )
    actions: List[str] = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE DATABASE IF NOT EXISTS `{}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci".format(target.database)
            )
            actions.append("database `{}` ready".format(target.database))
            if target.user == admin.user:
                actions.append("using admin account `{}`; no separate user created".format(admin.user))
                return actions
            for host in hosts or user_hosts(target):
                cursor.execute("CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s", (target.user, host, target.password))
                # Keep the password in sync with .env when the user already existed.
                cursor.execute("ALTER USER %s@%s IDENTIFIED BY %s", (target.user, host, target.password))
                cursor.execute("GRANT {} ON `{}`.* TO %s@%s".format(GRANTS, target.database), (target.user, host))
                actions.append("user `{}`@`{}` granted on `{}`".format(target.user, host, target.database))
    finally:
        connection.close()
    return actions


def ensure_env_url(env_path: Path, url: str) -> bool:
    """Append AI_FIX_DATABASE_URL to .env when it is missing or empty. Returns True if written."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    for index, line in enumerate(lines):
        key, _, value = line.partition("=")
        if key.strip() == ENV_KEY:
            if value.strip():
                return False
            lines[index] = "{}={}".format(ENV_KEY, url)
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["# MySQL state store của orchestrator (tạo bởi `make db-init`).", "{}={}".format(ENV_KEY, url)])
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    return True


def prompt_admin(host: str = "127.0.0.1", port: int = 3306) -> AdminLogin:
    configured = os.environ.get("AI_FIX_DB_ADMIN_URL", "").strip()
    if configured:
        return AdminLogin.parse(configured)
    print("Cần tài khoản MySQL có quyền tạo database/user trên {}:{}.".format(host, port))
    user = input("MySQL admin user [root]: ").strip() or "root"
    password = getpass.getpass("MySQL admin password (để trống nếu không có): ")
    return AdminLogin(host=host, port=port, user=user, password=password)
