import threading
import time
import uuid
from urllib.parse import urlsplit

import requests


class KeepAliveService:
    def __init__(self, account_manager, get_runtime_config, logger, user_agent: str):
        self.account_manager = account_manager
        self.get_runtime_config = get_runtime_config
        self.log = logger
        self.user_agent = user_agent
        self._thread = None
        self._stop = threading.Event()

    def _cfg(self):
        return self.get_runtime_config()

    def _site_base_url(self) -> str:
        upstream = self._cfg().get("upstream_url", "")
        parsed = urlsplit(upstream)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _session_url(self) -> str:
        return self._site_base_url() + "/api/auth/session"

    def _home_url(self) -> str:
        return self._site_base_url() + "/"

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="umans-keepalive",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _headers(self, accept: str = "application/json") -> dict:
        return {
            "Accept": accept,
            "User-Agent": self.user_agent,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def _needs_refresh(self, expires: str) -> bool:
        if not expires:
            return True
        try:
            from datetime import datetime, timezone

            dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            remaining = dt.timestamp() - time.time()
            threshold = int(self._cfg().get("keepalive_expiring_minutes", 20) or 20) * 60
            return remaining < threshold
        except Exception:
            return True

    def _fetch_session(self, account_id: str):
        acc = self.account_manager.get_account(account_id, include_cookies=True, include_password=True)
        if not acc:
            raise RuntimeError("account not found")
        response = requests.get(
            self._session_url(),
            headers=self._headers(),
            cookies=acc.get("cookies") or {},
            allow_redirects=False,
            timeout=30,
        )
        self.account_manager.merge_response_cookies(account_id, response.cookies)
        if response.status_code != 200:
            raise RuntimeError(f"session http {response.status_code}")
        data = response.json()
        user = data.get("user") or {}
        if not user:
            raise RuntimeError("session missing user")
        self.account_manager.update_session_info(
            account_id,
            plan=user.get("plan") or "",
            expires=data.get("expires") or user.get("expires") or "",
            name=user.get("name") or "",
            email=user.get("email") or "",
            ok=True,
        )
        return data

    def refresh_account(self, account_id: str) -> dict:
        acc = self.account_manager.get_account(account_id, include_cookies=True, include_password=True)
        if not acc:
            raise RuntimeError("account not found")
        last_error = ""

        try:
            response = requests.get(
                self._home_url(),
                headers=self._headers("text/html,application/xhtml+xml"),
                cookies=acc.get("cookies") or {},
                allow_redirects=False,
                timeout=30,
            )
            self.account_manager.merge_response_cookies(account_id, response.cookies)
            if response.status_code not in (200, 304):
                raise RuntimeError(f"refresh http {response.status_code}")
            self.account_manager.touch_keepalive(account_id)
            data = self._fetch_session(account_id)
            expires = (data.get("expires") or "").strip()
            if not self._needs_refresh(expires):
                return data
        except Exception as e:
            last_error = str(e)

        if not bool(self._cfg().get("keepalive_chat_fallback_enabled", True)):
            raise RuntimeError(last_error or "refresh failed")

        return self._chat_refresh(account_id)

    def _chat_refresh(self, account_id: str) -> dict:
        acc = self.account_manager.get_account(account_id, include_cookies=True)
        if not acc:
            raise RuntimeError("account not found")
        model = self._cfg().get("default_model") or "umans-coding-model"
        chat_id = str(uuid.uuid4())
        payload = {
            "selectedChatModel": model,
            "id": chat_id,
            "messages": [
                {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hi"}],
                    "id": str(uuid.uuid4()),
                }
            ],
            "knowledgeBaseId": None,
        }
        headers = self._headers("*/*")
        headers.update(
            {
                "Content-Type": "application/json",
                "Origin": self._site_base_url(),
                "Referer": f"{self._site_base_url()}/chat/{chat_id}",
            }
        )
        response = requests.post(
            self._site_base_url() + "/api/chat",
            headers=headers,
            cookies=acc.get("cookies") or {},
            json=payload,
            allow_redirects=False,
            stream=True,
            timeout=30,
        )
        self.account_manager.merge_response_cookies(account_id, response.cookies)
        if response.status_code != 200:
            raise RuntimeError(f"chat refresh http {response.status_code}")
        # 读到首个有效事件即可，避免长时间占住连接
        got_event = False
        for raw in response.iter_lines():
            if not raw:
                continue
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", errors="replace")
            else:
                line = str(raw)
            if line.startswith("data:"):
                got_event = True
                break
        if not got_event:
            raise RuntimeError("chat refresh no sse event")
        self.account_manager.touch_keepalive(account_id)
        return self._fetch_session(account_id)

    def relogin_account(self, account_id: str, browser_mode: str = "headless") -> dict:
        acc = self.account_manager.get_account(account_id, include_cookies=True)
        if not acc:
            raise RuntimeError("account not found")
        email = str(acc.get("email") or "").strip()
        password = str(acc.get("password") or "").strip()
        if not email or not password:
            raise RuntimeError("account missing email/password")

        from . import browser_auth

        proxy = str(self._cfg().get("REGISTER_PROXY") or "").strip() or None
        try:
            result = browser_auth.login_and_export_cookies(
                email,
                password,
                mode=browser_mode or "headless",
                proxy=proxy,
                site_base_url=self._site_base_url(),
                session_url=self._session_url(),
            )
            cookies = result.get("cookies") or {}
            if not cookies:
                raise RuntimeError("relogin returned empty cookies")
            self.account_manager.update_account(
                account_id,
                cookies=cookies,
                enabled=True,
                status="healthy",
                failures=0,
                cooldown_until=0,
                last_error="",
            )
            self.account_manager.update_relogin_result(account_id, ok=True, error="")
            data = result.get("session") or {}
            user = data.get("user") or {}
            self.account_manager.update_session_info(
                account_id,
                plan=user.get("plan") or "",
                expires=data.get("expires") or user.get("expires") or "",
                name=user.get("name") or "",
                email=user.get("email") or email,
                ok=True,
            )
            return {"ok": True, "data": data}
        except Exception as e:
            self.account_manager.update_relogin_result(account_id, ok=False, error=str(e))
            raise

    def check_account(self, account_id: str) -> dict:
        try:
            data = self._fetch_session(account_id)
            expires = (data.get("expires") or "").strip()
            if self._needs_refresh(expires):
                data = self.refresh_account(account_id)
            return {"ok": True, "data": data}
        except Exception as e:
            message = str(e)
            auth_invalid = any(x in message for x in ["http 401", "http 403", "http 302", "missing user"])
            if auth_invalid and bool(self._cfg().get("AUTO_RELOGIN_ENABLED", True)):
                try:
                    relogin = self.relogin_account(account_id, browser_mode="headless")
                    data = self._fetch_session(account_id)
                    return {"ok": True, "data": data or relogin.get("data") or {}}
                except Exception as relogin_error:
                    message = f"{message}; relogin failed: {relogin_error}"
            self.account_manager.mark_fail(account_id, message, auth_invalid=auth_invalid)
            self.account_manager.update_session_info(account_id, ok=False, error=message)
            return {"ok": False, "error": message}

    def run_once(self) -> dict:
        items = self.account_manager.list_accounts()
        checked = 0
        success = 0
        failed = 0
        for item in items:
            if not item.get("enabled"):
                continue
            checked += 1
            result = self.check_account(item["id"])
            if result.get("ok"):
                success += 1
            else:
                failed += 1
        return {"checked": checked, "success": success, "failed": failed}

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                self.log.warning("保活循环异常: %s", e)
            interval = int(self._cfg().get("keepalive_interval_seconds", 900) or 900)
            self._stop.wait(max(30, interval))
