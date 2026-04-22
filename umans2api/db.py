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

            CREATE TABLE IF NOT EXISTS response_cache (
                cache_key TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL DEFAULT '',
                api_format TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                prompt_hash TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '',
                hit_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                last_hit_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_response_cache_expires_at
            ON response_cache(expires_at);

            CREATE INDEX IF NOT EXISTS idx_response_cache_last_hit_at
            ON response_cache(last_hit_at);
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
            "input_tokens": "INTEGER NOT NULL DEFAULT 0",
            "output_tokens": "INTEGER NOT NULL DEFAULT 0",
            "total_tokens": "INTEGER NOT NULL DEFAULT 0",
            "reasoning_tokens": "INTEGER NOT NULL DEFAULT 0",
            "cache_hit": "INTEGER NOT NULL DEFAULT 0",
            "cache_read_input_tokens": "INTEGER NOT NULL DEFAULT 0",
            "cache_creation_input_tokens": "INTEGER NOT NULL DEFAULT 0",
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
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    reasoning_tokens: int = 0,
    cache_hit: bool = False,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
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
                response_id, tool_count, reasoning_chars, input_tokens, output_tokens,
                total_tokens, reasoning_tokens, cache_hit, cache_read_input_tokens,
                cache_creation_input_tokens, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                int(input_tokens or 0),
                int(output_tokens or 0),
                int(total_tokens or 0),
                int(reasoning_tokens or 0),
                1 if cache_hit else 0,
                int(cache_read_input_tokens or 0),
                int(cache_creation_input_tokens or 0),
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def summarize_request_logs() -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_requests,
                SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_requests,
                SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_requests,
                SUM(duration_ms) AS total_duration_ms,
                AVG(duration_ms) AS avg_duration_ms,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(total_tokens) AS total_tokens,
                SUM(reasoning_tokens) AS reasoning_tokens,
                SUM(cache_read_input_tokens) AS cache_read_input_tokens,
                SUM(cache_creation_input_tokens) AS cache_creation_input_tokens,
                SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) AS cache_hits
            FROM request_logs
            """
        ).fetchone()
        payload = row_to_dict(row) or {}
        total_requests = int(payload.get("total_requests") or 0)
        avg_duration_ms = payload.get("avg_duration_ms")
        return {
            "total_requests": total_requests,
            "ok_requests": int(payload.get("ok_requests") or 0),
            "failed_requests": int(payload.get("failed_requests") or 0),
            "avg_duration_ms": round(float(avg_duration_ms or 0), 2) if total_requests else 0,
            "input_tokens": int(payload.get("input_tokens") or 0),
            "output_tokens": int(payload.get("output_tokens") or 0),
            "total_tokens": int(payload.get("total_tokens") or 0),
            "reasoning_tokens": int(payload.get("reasoning_tokens") or 0),
            "cache_read_input_tokens": int(payload.get("cache_read_input_tokens") or 0),
            "cache_creation_input_tokens": int(payload.get("cache_creation_input_tokens") or 0),
            "cache_hits": int(payload.get("cache_hits") or 0),
            "cache_hit_rate": round((int(payload.get("cache_hits") or 0) / total_requests) * 100, 2) if total_requests else 0,
        }
    finally:
        conn.close()


def get_response_cache(cache_key: str) -> dict | None:
    now = time.time()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM response_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        item = row_to_dict(row)
        if not item:
            return None
        if float(item.get("expires_at") or 0) <= now:
            conn.execute("DELETE FROM response_cache WHERE cache_key = ?", (cache_key,))
            conn.commit()
            return None
        conn.execute(
            "UPDATE response_cache SET hit_count = hit_count + 1, last_hit_at = ? WHERE cache_key = ?",
            (now, cache_key),
        )
        conn.commit()
        try:
            item["payload"] = json.loads(item.get("payload_json") or "{}")
        except Exception:
            item["payload"] = {}
        return item
    finally:
        conn.close()


def upsert_response_cache(
    *,
    cache_key: str,
    scope_key: str,
    api_format: str,
    model: str,
    prompt_hash: str,
    payload: dict,
    ttl_seconds: int,
):
    now = time.time()
    expires_at = now + max(1, int(ttl_seconds or 1))
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO response_cache(
                cache_key, scope_key, api_format, model, prompt_hash, payload_json,
                hit_count, created_at, last_hit_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                scope_key = excluded.scope_key,
                api_format = excluded.api_format,
                model = excluded.model,
                prompt_hash = excluded.prompt_hash,
                payload_json = excluded.payload_json,
                last_hit_at = excluded.last_hit_at,
                expires_at = excluded.expires_at
            """,
            (
                cache_key,
                scope_key or "",
                api_format or "",
                model or "",
                prompt_hash or "",
                json.dumps(payload or {}, ensure_ascii=False),
                now,
                now,
                expires_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def prune_response_cache(max_entries: int = 1000):
    max_entries = max(1, int(max_entries or 1000))
    now = time.time()
    conn = get_conn()
    try:
        conn.execute("DELETE FROM response_cache WHERE expires_at <= ?", (now,))
        rows = conn.execute(
            "SELECT cache_key FROM response_cache ORDER BY last_hit_at DESC, created_at DESC"
        ).fetchall()
        extra = rows[max_entries:]
        if extra:
            conn.executemany(
                "DELETE FROM response_cache WHERE cache_key = ?",
                [(row["cache_key"],) for row in extra],
            )
        conn.commit()
    finally:
        conn.close()


def response_cache_stats() -> dict:
    now = time.time()
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS entries,
                SUM(hit_count) AS hits
            FROM response_cache
            WHERE expires_at > ?
            """,
            (now,),
        ).fetchone()
        item = row_to_dict(row) or {}
        return {
            "entries": int(item.get("entries") or 0),
            "hits": int(item.get("hits") or 0),
        }
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
