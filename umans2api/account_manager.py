import json
import threading
import time
import uuid
from datetime import datetime, timezone

from .db import get_conn, row_to_dict


def sanitize_cookies(raw_cookies):
    safe = {}
    for key, value in (raw_cookies or {}).items():
        if value is None:
            continue
        if not isinstance(value, str):
            value = str(value)
        value = value.strip()
        if not value:
            continue
        try:
            f"{key}={value}".encode("latin-1")
        except UnicodeEncodeError:
            continue
        safe[key] = value
    return safe


def parse_cookies_input(value):
    if isinstance(value, dict):
        return sanitize_cookies(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return {}
        data = json.loads(value)
        if not isinstance(data, dict):
            raise ValueError("cookies_json 必须是 JSON 对象")
        return sanitize_cookies(data)
    raise ValueError("cookies_json 格式错误")


def mask_cookie_preview(cookies: dict) -> str:
    keys = sorted((cookies or {}).keys())
    return ", ".join(keys[:3]) + (" ..." if len(keys) > 3 else "")


def _parse_expires_ts(value: str) -> float | None:
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


class AccountManager:
    def __init__(self, get_runtime_config):
        self._get_runtime_config = get_runtime_config
        self._lock = threading.RLock()
        self._robin_index = 0

    def _cfg(self):
        return self._get_runtime_config()

    def list_accounts(self) -> list[dict]:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY created_at ASC"
            ).fetchall()
            items = []
            for row in rows:
                item = row_to_dict(row)
                cookies = json.loads(item.get("cookies_json") or "{}")
                item["cookie_preview"] = mask_cookie_preview(cookies)
                item["cookie_count"] = len(cookies)
                item["has_password"] = bool(item.get("password"))
                item.pop("cookies_json", None)
                item.pop("password", None)
                items.append(item)
            return items
        finally:
            conn.close()

    def get_accounts_by_ids(self, account_ids: list[str], include_cookies: bool = False, include_password: bool = False) -> list[dict]:
        items = []
        for account_id in account_ids or []:
            acc = self.get_account(account_id, include_cookies=include_cookies, include_password=include_password)
            if acc:
                items.append(acc)
        return items

    def summary(self) -> dict:
        items = self.list_accounts()
        now = time.time()
        return {
            "total": len(items),
            "enabled": sum(1 for i in items if i.get("enabled")),
            "healthy": sum(1 for i in items if i.get("status") == "healthy"),
            "expiring": sum(1 for i in items if i.get("status") == "expiring"),
            "cooling": sum(1 for i in items if i.get("status") == "cooling"),
            "disabled": sum(1 for i in items if i.get("status") == "disabled"),
            "inflight": sum(int(i.get("inflight_count") or 0) for i in items),
            "active": sum(
                1
                for i in items
                if i.get("enabled") and float(i.get("cooldown_until") or 0) <= now
            ),
        }

    def get_account(self, account_id: str, include_cookies: bool = False, include_password: bool = False) -> dict | None:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            item = row_to_dict(row)
            if not item:
                return None
            cookies = json.loads(item.get("cookies_json") or "{}")
            if include_cookies:
                item["cookies"] = cookies
            item["cookie_preview"] = mask_cookie_preview(cookies)
            item["cookie_count"] = len(cookies)
            item["has_password"] = bool(item.get("password"))
            if not include_cookies:
                item.pop("cookies_json", None)
            if not include_password:
                item.pop("password", None)
            return item
        finally:
            conn.close()

    def add_account(
        self,
        *,
        name: str,
        email: str,
        cookies,
        password: str = "",
        allowed_model_prefix: str = "umans-",
        enabled: bool = True,
        register_source: str = "",
        auth_mode: str = "",
    ) -> dict:
        safe_cookies = parse_cookies_input(cookies)
        if not safe_cookies:
            raise ValueError("cookies_json 不能为空")
        now = time.time()
        account_id = uuid.uuid4().hex
        conn = get_conn()
        try:
            conn.execute(
                """
                INSERT INTO accounts(
                    id, name, email, cookies_json, plan, allowed_model_prefix,
                    session_expires_at, enabled, status, failures, inflight_count,
                    cooldown_until, last_keepalive_at, last_session_check_at,
                    last_chat_ok_at, last_selected_at, last_error, password,
                    register_source, auth_mode, last_registered_at, last_relogin_at,
                    last_relogin_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '', ?, '', ?, 'healthy', 0, 0, 0, 0, 0, 0, 0, '', ?, ?, ?, ?, 0, '', ?, ?)
                """,
                (
                    account_id,
                    (name or "").strip(),
                    (email or "").strip(),
                    json.dumps(safe_cookies, ensure_ascii=False),
                    (allowed_model_prefix or "umans-").strip(),
                    1 if enabled else 0,
                    (password or "").strip(),
                    (register_source or "").strip(),
                    (auth_mode or "").strip(),
                    now if register_source else 0,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_account(account_id)

    def update_account(self, account_id: str, **fields) -> dict | None:
        allowed = {
            "name",
            "email",
            "allowed_model_prefix",
            "enabled",
            "cookies",
            "cookies_json",
            "status",
            "plan",
            "last_error",
            "session_expires_at",
            "failures",
            "cooldown_until",
            "last_keepalive_at",
            "last_session_check_at",
            "last_chat_ok_at",
            "last_selected_at",
            "password",
            "register_source",
            "auth_mode",
            "last_registered_at",
            "last_relogin_at",
            "last_relogin_error",
        }
        updates = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {"cookies", "cookies_json"}:
                updates["cookies_json"] = json.dumps(
                    parse_cookies_input(value), ensure_ascii=False
                )
            elif key == "enabled":
                updates["enabled"] = 1 if value else 0
            else:
                updates[key] = value
        if not updates:
            return self.get_account(account_id)

        updates["updated_at"] = time.time()
        columns = ", ".join(f"{k} = ?" for k in updates.keys())
        params = list(updates.values()) + [account_id]
        conn = get_conn()
        try:
            conn.execute(
                f"UPDATE accounts SET {columns} WHERE id = ?",
                params,
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_account(account_id)

    def delete_account(self, account_id: str) -> bool:
        conn = get_conn()
        try:
            cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def set_enabled(self, account_id: str, enabled: bool) -> dict | None:
        if enabled:
            return self.update_account(
                account_id,
                enabled=True,
                status="healthy",
                failures=0,
                cooldown_until=0,
                last_error="",
            )
        return self.update_account(account_id, enabled=False, status="disabled")

    def batch_set_enabled(self, account_ids: list[str], enabled: bool) -> list[dict]:
        items = []
        for account_id in account_ids or []:
            acc = self.set_enabled(account_id, enabled)
            if acc:
                items.append(acc)
        return items

    def batch_delete(self, account_ids: list[str]) -> int:
        deleted = 0
        for account_id in account_ids or []:
            if self.delete_account(account_id):
                deleted += 1
        return deleted

    def _supports_model(self, account: dict, upstream_model: str) -> bool:
        prefix = (account.get("allowed_model_prefix") or "").strip()
        if not prefix:
            return True
        return upstream_model.startswith(prefix)

    def reserve_next(self, upstream_model: str, exclude_ids: set[str] | None = None) -> dict | None:
        exclude_ids = exclude_ids or set()
        cfg = self._cfg()
        fail_threshold = int(cfg.get("fail_threshold", 3) or 3)
        max_inflight = int(cfg.get("max_inflight", 2) or 2)
        now = time.time()
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE enabled = 1 ORDER BY created_at ASC"
            ).fetchall()
            accounts = [row_to_dict(r) for r in rows]
        finally:
            conn.close()

        candidates = [
            a
            for a in accounts
            if a["id"] not in exclude_ids
            and self._supports_model(a, upstream_model)
            and (a.get("status") != "disabled")
            and float(a.get("cooldown_until") or 0) <= now
            and int(a.get("failures") or 0) < fail_threshold
            and int(a.get("inflight_count") or 0) < max_inflight
        ]
        if not candidates:
            return None

        with self._lock:
            start = self._robin_index % len(candidates)
            selected = None
            for offset in range(len(candidates)):
                idx = (start + offset) % len(candidates)
                selected = candidates[idx]
                self._robin_index = idx + 1
                break
            if not selected:
                return None
            conn = get_conn()
            try:
                conn.execute(
                    """
                    UPDATE accounts
                    SET inflight_count = inflight_count + 1,
                        last_selected_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, selected["id"]),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_account(selected["id"], include_cookies=True)

    def release_reservation(self, account_id: str):
        conn = get_conn()
        try:
            conn.execute(
                """
                UPDATE accounts
                SET inflight_count = CASE
                    WHEN inflight_count > 0 THEN inflight_count - 1
                    ELSE 0
                END,
                    updated_at = ?
                WHERE id = ?
                """,
                (time.time(), account_id),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_ok(self, account_id: str):
        now = time.time()
        conn = get_conn()
        try:
            conn.execute(
                """
                UPDATE accounts
                SET failures = 0,
                    status = CASE WHEN enabled = 1 THEN 'healthy' ELSE status END,
                    last_chat_ok_at = ?,
                    last_error = '',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, account_id),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_fail(self, account_id: str, error: str = "", auth_invalid: bool = False):
        cfg = self._cfg()
        fail_threshold = int(cfg.get("fail_threshold", 3) or 3)
        cooldown_seconds = int(cfg.get("cooldown_seconds", 120) or 120)
        acc = self.get_account(account_id, include_cookies=True)
        if not acc:
            return
        failures = int(acc.get("failures") or 0) + 1
        now = time.time()
        enabled = int(acc.get("enabled") or 0)
        status = acc.get("status") or "healthy"
        cooldown_until = float(acc.get("cooldown_until") or 0)
        if auth_invalid:
            enabled = 0
            status = "disabled"
        elif failures >= fail_threshold:
            status = "cooling"
            cooldown_until = now + cooldown_seconds
        conn = get_conn()
        try:
            conn.execute(
                """
                UPDATE accounts
                SET failures = ?,
                    enabled = ?,
                    status = ?,
                    cooldown_until = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (failures, enabled, status, cooldown_until, error[:500], now, account_id),
            )
            conn.commit()
        finally:
            conn.close()

    def merge_response_cookies(self, account_id: str, response_cookies):
        if not response_cookies:
            return
        acc = self.get_account(account_id, include_cookies=True)
        if not acc:
            return
        cookies = dict(acc.get("cookies") or {})
        changed = False
        try:
            iterable = response_cookies.items()
        except Exception:
            iterable = []
        for key, value in iterable:
            if not isinstance(value, str):
                continue
            if cookies.get(key) != value:
                cookies[key] = value
                changed = True
        if changed:
            self.update_account(account_id, cookies=cookies)

    def update_session_info(
        self,
        account_id: str,
        *,
        plan: str = "",
        expires: str = "",
        name: str = "",
        email: str = "",
        ok: bool = True,
        error: str = "",
    ):
        now = time.time()
        updates = {
            "plan": plan or "",
            "session_expires_at": expires or "",
            "last_error": error[:500] if error else "",
        }
        if name:
            updates["name"] = name
        if email:
            updates["email"] = email

        status = "healthy"
        if not ok:
            status = "disabled"
        else:
            exp_ts = _parse_expires_ts(expires or "")
            if exp_ts and exp_ts - now < 20 * 60:
                status = "expiring"
        updates["status"] = status
        updates["updated_at"] = now

        conn = get_conn()
        try:
            conn.execute(
                """
                UPDATE accounts
                SET plan = ?,
                    session_expires_at = ?,
                    last_session_check_at = ?,
                    last_error = ?,
                    status = ?,
                    name = COALESCE(NULLIF(?, ''), name),
                    email = COALESCE(NULLIF(?, ''), email),
                    enabled = CASE WHEN ? THEN enabled ELSE 0 END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    updates["plan"],
                    updates["session_expires_at"],
                    now,
                    updates["last_error"],
                    updates["status"],
                    name,
                    email,
                    1 if ok else 0,
                    now,
                    account_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def count_active_accounts(self) -> int:
        return int(self.summary().get("active", 0) or 0)

    def update_relogin_result(self, account_id: str, *, ok: bool, error: str = ""):
        now = time.time()
        conn = get_conn()
        try:
            conn.execute(
                """
                UPDATE accounts
                SET last_relogin_at = ?,
                    last_relogin_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now if ok else float(now), (error or "")[:500], now, account_id),
            )
            conn.commit()
        finally:
            conn.close()

    def touch_keepalive(self, account_id: str):
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE accounts SET last_keepalive_at = ?, updated_at = ? WHERE id = ?",
                (time.time(), time.time(), account_id),
            )
            conn.commit()
        finally:
            conn.close()
