import json
import sqlite3
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "umans2api.db"


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                cookies_json TEXT NOT NULL,
                plan TEXT NOT NULL DEFAULT '',
                allowed_model_prefix TEXT NOT NULL DEFAULT 'umans-',
                session_expires_at TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'healthy',
                failures INTEGER NOT NULL DEFAULT 0,
                inflight_count INTEGER NOT NULL DEFAULT 0,
                cooldown_until REAL NOT NULL DEFAULT 0,
                last_keepalive_at REAL NOT NULL DEFAULT 0,
                last_session_check_at REAL NOT NULL DEFAULT 0,
                last_chat_ok_at REAL NOT NULL DEFAULT 0,
                last_selected_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS request_logs (
                id TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                path TEXT NOT NULL DEFAULT '',
                api_format TEXT NOT NULL DEFAULT '',
                stream INTEGER NOT NULL DEFAULT 0,
                client_model TEXT NOT NULL DEFAULT '',
                upstream_model TEXT NOT NULL DEFAULT '',
                account_id TEXT NOT NULL DEFAULT '',
                account_name TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 0,
                status_code INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                finish_reason TEXT NOT NULL DEFAULT '',
                response_id TEXT NOT NULL DEFAULT '',
                tool_count INTEGER NOT NULL DEFAULT 0,
                reasoning_chars INTEGER NOT NULL DEFAULT 0,
                detail_json TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS configs (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        account_columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        account_required_columns = {
            "password": "TEXT NOT NULL DEFAULT ''",
            "register_source": "TEXT NOT NULL DEFAULT ''",
            "auth_mode": "TEXT NOT NULL DEFAULT ''",
            "last_registered_at": "REAL NOT NULL DEFAULT 0",
            "last_relogin_at": "REAL NOT NULL DEFAULT 0",
            "last_relogin_error": "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in account_required_columns.items():
            if name not in account_columns:
                conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {ddl}")
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(request_logs)").fetchall()}
        required_columns = {
            "api_format": "TEXT NOT NULL DEFAULT ''",
            "stream": "INTEGER NOT NULL DEFAULT 0",
            "finish_reason": "TEXT NOT NULL DEFAULT ''",
            "response_id": "TEXT NOT NULL DEFAULT ''",
            "tool_count": "INTEGER NOT NULL DEFAULT 0",
            "reasoning_chars": "INTEGER NOT NULL DEFAULT 0",
            "detail_json": "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in required_columns.items():
            if name not in existing_columns:
                conn.execute(f"ALTER TABLE request_logs ADD COLUMN {name} {ddl}")
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def list_request_logs(limit: int = 100) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM request_logs ORDER BY ts DESC LIMIT ?",
            (max(1, min(int(limit or 100), 500)),),
        ).fetchall()
        items = []
        for row in rows:
            item = row_to_dict(row)
            try:
                item["detail"] = json.loads(item.get("detail_json") or "{}")
            except Exception:
                item["detail"] = {}
            items.append(item)
        return items
    finally:
        conn.close()


def get_request_log(log_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM request_logs WHERE id = ?", (log_id,)).fetchone()
        if not row:
            return None
        item = row_to_dict(row)
        try:
            item["detail"] = json.loads(item.get("detail_json") or "{}")
        except Exception:
            item["detail"] = {}
        return item
    finally:
        conn.close()


def insert_request_log(
    *,
    path: str,
    api_format: str = "",
    stream: bool = False,
    client_model: str,
    upstream_model: str,
    account_id: str,
    account_name: str,
    ok: bool,
    status_code: int,
    duration_ms: int,
    error: str = "",
    finish_reason: str = "",
    response_id: str = "",
    tool_count: int = 0,
    reasoning_chars: int = 0,
    detail: dict | None = None,
):
    conn = get_conn()
    now = time.time()
    try:
        conn.execute(
            """
            INSERT INTO request_logs(
                id, ts, path, api_format, stream, client_model, upstream_model, account_id,
                account_name, ok, status_code, duration_ms, error, finish_reason,
                response_id, tool_count, reasoning_chars, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                now,
                path,
                api_format or "",
                1 if stream else 0,
                client_model,
                upstream_model,
                account_id,
                account_name,
                1 if ok else 0,
                int(status_code or 0),
                int(duration_ms or 0),
                error or "",
                finish_reason or "",
                response_id or "",
                int(tool_count or 0),
                int(reasoning_chars or 0),
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def account_count() -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()
        return int(row["c"] if row else 0)
    finally:
        conn.close()


def maybe_import_legacy_account(
    *,
    name: str,
    email: str,
    cookies: dict,
    allowed_model_prefix: str = "umans-",
):
    if not cookies or account_count() > 0:
        return False

    now = time.time()
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO accounts(
                id, name, email, cookies_json, plan, allowed_model_prefix,
                session_expires_at, enabled, status, failures, inflight_count,
                cooldown_until, last_keepalive_at, last_session_check_at,
                last_chat_ok_at, last_selected_at, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, '', ?, '', 1, 'healthy', 0, 0, 0, 0, 0, 0, 0, '', ?, ?)
            """,
            (
                uuid.uuid4().hex,
                name,
                email,
                json.dumps(cookies, ensure_ascii=False),
                allowed_model_prefix or "umans-",
                now,
                now,
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def seed_config_defaults(defaults: dict):
    if not defaults:
        return
    now = time.time()
    conn = get_conn()
    try:
        for key, value in defaults.items():
            if value is None:
                continue
            conn.execute(
                """
                INSERT INTO configs(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (key, json.dumps(value, ensure_ascii=False), now),
            )
        conn.commit()
    finally:
        conn.close()


def get_config_overrides() -> dict:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM configs").fetchall()
        data = {}
        for row in rows:
            try:
                data[row["key"]] = json.loads(row["value"])
            except Exception:
                data[row["key"]] = row["value"]
        return data
    finally:
        conn.close()


def upsert_config_values(values: dict):
    if not values:
        return
    now = time.time()
    conn = get_conn()
    try:
        for key, value in values.items():
            conn.execute(
                """
                INSERT INTO configs(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), now),
            )
        conn.commit()
    finally:
        conn.close()
