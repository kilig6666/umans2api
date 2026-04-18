import json
import random
import string
import threading
import time
from copy import deepcopy
from urllib.parse import urlsplit

from . import browser_auth, moemail

_logger = None
_get_config = None
_account_manager = None
_keepalive = None
_thread_local = threading.local()
_task_lock = threading.RLock()
_current_task = None
_background_lock = threading.Lock()
_auto_replenish_thread = None
_auto_replenish_stop = threading.Event()


class RegistrationStopped(RuntimeError):
    def __init__(self, message: str = "Registration stopped", partial: list[dict] | None = None):
        super().__init__(message)
        self.partial = partial or []


def configure(*, get_config, account_manager, keepalive, logger):
    global _get_config, _account_manager, _keepalive, _logger
    _get_config = get_config
    _account_manager = account_manager
    _keepalive = keepalive
    _logger = logger


def _cfg() -> dict:
    return dict(_get_config() or {}) if _get_config else {}


def set_thread_log_fn(log_fn):
    _thread_local.log_fn = log_fn


def _log(message: str, level: str = "INFO"):
    fn = getattr(_thread_local, "log_fn", None)
    if fn:
        fn(str(message), str(level or "INFO").upper())
        return
    if _logger:
        log_method = getattr(_logger, str(level or "INFO").lower(), None) or _logger.info
        log_method(str(message))


def _site_base_url() -> str:
    upstream_url = str(_cfg().get("upstream_url") or "https://app.umans.ai/api/chat")
    parsed = urlsplit(upstream_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _session_url() -> str:
    return _site_base_url().rstrip("/") + "/api/auth/session"


def _manual_browser_mode() -> str:
    return "visible" if str(_cfg().get("AUTO_REGISTER_BROWSER_MODE_MANUAL") or "visible").strip().lower() == "visible" else "headless"


def _background_browser_mode() -> str:
    return "headless" if str(_cfg().get("AUTO_REGISTER_BROWSER_MODE_BACKGROUND") or "headless").strip().lower() != "visible" else "visible"


def _normalize_workers(workers, browser_mode: str) -> int:
    try:
        value = int(workers)
    except Exception:
        value = 1
    value = max(1, value)
    if browser_mode == "visible":
        return 1
    return min(value, 2)


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "@#_-!"
    return "".join(random.choice(chars) for _ in range(max(12, length)))


def _password_from_config() -> str:
    value = str(_cfg().get("AUTO_REGISTER_PASSWORD") or "").strip()
    return value or _random_password()


def _mailbox_for_poll(mailbox: dict) -> dict:
    item = dict(mailbox or {})
    item["disable_ssl_verify"] = bool(_cfg().get("DISABLE_SSL_VERIFY"))
    if bool(_cfg().get("MAIL_USE_PROXY")):
        item["proxy"] = str(_cfg().get("REGISTER_PROXY") or "").strip() or None
    return item


def _task_public(task: dict | None, *, include_logs: bool = False, include_results: bool = False) -> dict | None:
    if not task:
        return None
    result = {
        "id": task["id"],
        "status": task["status"],
        "requested": int(task.get("requested") or 0),
        "registered": int(task.get("success_count") or 0),
        "success_count": int(task.get("success_count") or 0),
        "failed_count": int(task.get("failed_count") or 0),
        "remaining_count": max(0, int(task.get("requested") or 0) - int(task.get("success_count") or 0) - int(task.get("failed_count") or 0)),
        "active_workers": int(task.get("active_workers") or 0),
        "workers": int(task.get("workers") or 1),
        "browser_mode": task.get("browser_mode") or _manual_browser_mode(),
        "stop_requested": bool(task.get("stop_requested")),
        "created_at": task.get("created_at") or 0,
        "updated_at": task.get("updated_at") or 0,
        "error": task.get("error") or "",
        "accounts": [dict(item) for item in task.get("accounts") or []],
    }
    if include_logs:
        result["logs"] = [dict(item) for item in task.get("logs") or []]
    if include_results:
        result["results"] = [dict(item) for item in task.get("results") or []]
    return result


def get_current_task(task_id: str | None = None, *, include_logs: bool = False, include_results: bool = False) -> dict | None:
    with _task_lock:
        if not _current_task:
            return None
        if task_id and _current_task.get("id") != task_id:
            return None
        return _task_public(_current_task, include_logs=include_logs, include_results=include_results)


def _set_task_status(task_id: str, status: str, **updates):
    with _task_lock:
        global _current_task
        if not _current_task or _current_task.get("id") != task_id:
            return
        _current_task["status"] = status
        _current_task["updated_at"] = time.time()
        for key, value in updates.items():
            _current_task[key] = value


def _append_task_log(task_id: str, message: str, level: str = "INFO"):
    text = str(message or "")
    level_name = str(level or "INFO").upper()
    with _task_lock:
        global _current_task
        if not _current_task or _current_task.get("id") != task_id:
            return
        _current_task["log_seq"] += 1
        _current_task["updated_at"] = time.time()
        _current_task.setdefault("logs", []).append(
            {
                "seq": _current_task["log_seq"],
                "ts": _current_task["updated_at"],
                "level": level_name,
                "message": text,
            }
        )
        if len(_current_task["logs"]) > 1000:
            _current_task["logs"] = _current_task["logs"][-1000:]
    if _logger:
        log_method = getattr(_logger, level_name.lower(), None) or _logger.info
        log_method(f"[auto-register:{task_id[:8]}] {text}")


def _append_task_result(task_id: str, result: dict):
    with _task_lock:
        global _current_task
        if not _current_task or _current_task.get("id") != task_id:
            return
        _current_task["result_seq"] += 1
        _current_task["updated_at"] = time.time()
        payload = dict(result or {})
        payload["seq"] = _current_task["result_seq"]
        _current_task.setdefault("results", []).append(payload)
        _current_task.setdefault("accounts", []).append(payload)
        _current_task["success_count"] = int(_current_task.get("success_count") or 0) + 1


def _mark_task_failure(task_id: str, error: str = ""):
    with _task_lock:
        global _current_task
        if not _current_task or _current_task.get("id") != task_id:
            return
        _current_task["updated_at"] = time.time()
        _current_task["failed_count"] = int(_current_task.get("failed_count") or 0) + 1
        if error:
            _current_task["error"] = str(error)[:500]


def _set_active_workers(task_id: str, count: int):
    with _task_lock:
        global _current_task
        if not _current_task or _current_task.get("id") != task_id:
            return
        _current_task["updated_at"] = time.time()
        _current_task["active_workers"] = max(0, int(count or 0))


def _should_stop(task_id: str | None) -> bool:
    if not task_id:
        return False
    with _task_lock:
        return bool(_current_task and _current_task.get("id") == task_id and _current_task.get("stop_requested"))


def stop(task_id: str | None = None) -> dict | None:
    with _task_lock:
        global _current_task
        if not _current_task:
            return None
        if task_id and _current_task.get("id") != task_id:
            return None
        if _current_task.get("status") in {"completed", "failed", "stopped"}:
            return _task_public(_current_task)
        _current_task["stop_requested"] = True
        _current_task["status"] = "stopping"
        _current_task["updated_at"] = time.time()
        task_snapshot = _task_public(_current_task)
    _append_task_log(task_snapshot["id"], "⛔ 收到停止请求，正在安全收尾当前步骤", "WARNING")
    return task_snapshot


def _ensure_ready():
    if not _account_manager or not _keepalive:
        raise RuntimeError("auto register service not configured")


def check_config() -> dict:
    cfg = _cfg()
    browser_ready, browser_error = browser_auth.is_available()
    moe_stats = moemail.get_channel_stats(cfg)
    mail_provider_default = str(cfg.get("MAIL_PROVIDER_DEFAULT") or "moemail").strip() or "moemail"
    missing = []
    if mail_provider_default != "moemail":
        missing.append("MAIL_PROVIDER_DEFAULT=moemail")
    if not moe_stats["configured"]:
        if str(cfg.get("MOEMAIL_CHANNELS_JSON") or "").strip():
            missing.append("MOEMAIL_CHANNELS_JSON(valid)")
        else:
            if not str(cfg.get("MOEMAIL_API_KEY") or "").strip():
                missing.append("MOEMAIL_API_KEY")
            if not str(cfg.get("MOEMAIL_API_BASE") or "").strip():
                missing.append("MOEMAIL_API_BASE")
    if not browser_ready:
        missing.append("playwright")
    return {
        "ready": not missing,
        "missing": missing,
        "mail_provider_default": mail_provider_default,
        "moemail_configured": bool(moe_stats["configured"]),
        "manual_browser_modes": ["visible", "headless"],
        "background_browser_mode": _background_browser_mode(),
        "auto_register_enabled": bool(cfg.get("AUTO_REGISTER_ENABLED")),
        "auto_relogin_enabled": bool(cfg.get("AUTO_RELOGIN_ENABLED", True)),
        "active_accounts": _account_manager.count_active_accounts() if _account_manager else 0,
        "min_active": int(cfg.get("AUTO_REGISTER_MIN_ACTIVE") or 0),
        "browser_ready": browser_ready,
        "browser_error": browser_error,
        "channels_total": moe_stats.get("total", 0),
        "channels_enabled": moe_stats.get("enabled", 0),
        "channels_parse_error": moe_stats.get("parse_error", ""),
        "current_task": get_current_task(),
    }


def _register_one_impl(register_source: str, browser_mode: str, task_id: str | None = None) -> dict:
    _ensure_ready()
    cfg = _cfg()
    password = _password_from_config()
    proxy = str(cfg.get("REGISTER_PROXY") or "").strip() or None
    site_base_url = _site_base_url()
    session_url = _session_url()

    _log("📮 正在从 MoeMail 创建邮箱...", "INFO")
    mailbox = moemail.create_mailbox(cfg)
    email = mailbox["email"]
    _log(f"📨 邮箱已创建: {email}（channel={mailbox.get('channel_name') or mailbox.get('channel_id') or 'MoeMail'}）", "INFO")
    _log(f"🌐 浏览器模式: {browser_mode}", "INFO")

    session = browser_auth.start_registration(
        email,
        password,
        mode=browser_mode,
        proxy=proxy,
        site_base_url=site_base_url,
        session_url=session_url,
    )
    try:
        if task_id and _should_stop(task_id):
            raise RegistrationStopped(partial=[])
        _log("📧 正在轮询验证邮件...", "INFO")
        verify_mail = moemail.poll_verification_email(
            _mailbox_for_poll(mailbox),
            timeout=180,
            site_base_url=site_base_url,
        )
        verify_url = verify_mail.get("verify_url")
        if not verify_url:
            raise RuntimeError("验证邮件中未找到 verify 链接")
        _log("🔗 已拿到验证链接，正在浏览器内完成验证...", "INFO")
        auth_result = browser_auth.complete_registration(session, verify_url, email=email, password=password)
    finally:
        session.close()

    cookies = auth_result.get("cookies") or {}
    if not cookies:
        raise RuntimeError("注册完成但未导出到 cookies")

    account = _account_manager.add_account(
        name=email.split("@")[0],
        email=email,
        password=password,
        cookies=cookies,
        allowed_model_prefix="umans-",
        enabled=True,
        register_source=register_source,
        auth_mode="browser_email_password",
    )
    keepalive_result = _keepalive.check_account(account["id"])
    if not keepalive_result.get("ok"):
        raise RuntimeError(f"账号已入库，但首轮验活失败: {keepalive_result.get('error') or 'unknown error'}")
    account = _account_manager.get_account(account["id"]) or account
    return {
        "account_id": account["id"],
        "account_name": account.get("name") or email.split("@")[0],
        "email": email,
        "register_source": register_source,
        "browser_mode": browser_mode,
        "has_password": True,
        "keepalive_ok": True,
    }


def register_one(*, register_source: str = "manual", browser_mode: str | None = None, task_id: str | None = None) -> dict:
    mode = browser_mode or (_manual_browser_mode() if register_source == "manual" else _background_browser_mode())
    return _register_one_impl(register_source=register_source, browser_mode=mode, task_id=task_id)


def _worker_loop(task_id: str, pending: list[int], register_source: str, browser_mode: str):
    current = 0
    while True:
        with _task_lock:
            if not pending:
                break
            current = pending.pop(0)
        if _should_stop(task_id):
            break
        try:
            result = register_one(register_source=register_source, browser_mode=browser_mode, task_id=task_id)
            _append_task_result(task_id, result)
            _append_task_log(task_id, f"✅ 注册成功: {result['email']}", "SUCCESS")
        except RegistrationStopped:
            break
        except Exception as exc:
            _mark_task_failure(task_id, str(exc))
            _append_task_log(task_id, f"❌ 第 {current} 个账号注册失败: {exc}", "ERROR")


def _run_task(task_id: str, count: int, workers: int, browser_mode: str):
    pending = list(range(1, count + 1))
    active = 0

    def worker():
        nonlocal active
        set_thread_log_fn(lambda message, level="INFO": _append_task_log(task_id, message, level))
        with _task_lock:
            active += 1
            _set_active_workers(task_id, active)
        try:
            _worker_loop(task_id, pending, "manual", browser_mode)
        finally:
            set_thread_log_fn(None)
            with _task_lock:
                active -= 1
                _set_active_workers(task_id, active)

    try:
        _append_task_log(task_id, f"🚀 手动注册任务已启动：数量={count} workers={workers} mode={browser_mode}", "INFO")
        threads = []
        for _ in range(workers):
            item = threading.Thread(target=worker, daemon=True, name=f"umans-auto-register-{task_id[:8]}")
            item.start()
            threads.append(item)
        for item in threads:
            item.join()
        if _should_stop(task_id):
            _set_task_status(task_id, "stopped", active_workers=0)
            _append_task_log(task_id, "⛔ 手动注册已停止", "WARNING")
        else:
            _set_task_status(task_id, "completed", active_workers=0, error="")
            _append_task_log(task_id, "✅ 手动注册任务完成", "SUCCESS")
    except Exception as exc:
        _set_task_status(task_id, "failed", active_workers=0, error=str(exc))
        _append_task_log(task_id, f"❌ 手动注册任务异常: {exc}", "ERROR")


def start(*, count: int, workers: int, browser_mode: str) -> tuple[dict | None, str | None]:
    cfg_state = check_config()
    if not cfg_state["ready"]:
        return None, ", ".join(cfg_state["missing"])
    normalized_mode = "visible" if str(browser_mode or _manual_browser_mode()).strip().lower() == "visible" else "headless"
    normalized_workers = _normalize_workers(workers, normalized_mode)
    with _task_lock:
        global _current_task
        if _current_task and _current_task.get("status") in {"running", "stopping"}:
            return _task_public(_current_task), "busy"
        task_id = f"task-{int(time.time() * 1000)}"
        _current_task = {
            "id": task_id,
            "status": "running",
            "requested": max(1, int(count or 1)),
            "workers": normalized_workers,
            "browser_mode": normalized_mode,
            "stop_requested": False,
            "created_at": time.time(),
            "updated_at": time.time(),
            "log_seq": 0,
            "result_seq": 0,
            "success_count": 0,
            "failed_count": 0,
            "active_workers": 0,
            "logs": [],
            "results": [],
            "accounts": [],
            "error": "",
        }
    threading.Thread(
        target=_run_task,
        args=(task_id, max(1, int(count or 1)), normalized_workers, normalized_mode),
        daemon=True,
        name=f"umans-register-{task_id[-6:]}",
    ).start()
    return get_current_task(task_id), None


def _background_batch(count: int, workers: int):
    with _background_lock:
        pending = list(range(1, count + 1))
        pending_lock = threading.Lock()

        def worker():
            while True:
                with pending_lock:
                    if not pending:
                        return
                    index = pending.pop(0)
                try:
                    result = register_one(register_source="auto_replenish", browser_mode=_background_browser_mode())
                    _log(f"✅ 自动补号成功: {result['email']} ({index}/{count})", "INFO")
                except Exception as exc:
                    _log(f"❌ 自动补号失败: {exc}", "ERROR")
                    return

        thread_list = []
        for _ in range(max(1, workers)):
            item = threading.Thread(target=worker, daemon=True, name="umans-auto-replenish-worker")
            item.start()
            thread_list.append(item)
        for item in thread_list:
            item.join()


def _auto_replenish_loop():
    while not _auto_replenish_stop.is_set():
        try:
            cfg = _cfg()
            if not bool(cfg.get("AUTO_REGISTER_ENABLED")):
                _auto_replenish_stop.wait(30)
                continue
            if get_current_task() and get_current_task().get("status") in {"running", "stopping"}:
                _auto_replenish_stop.wait(30)
                continue
            if _background_lock.locked():
                _auto_replenish_stop.wait(30)
                continue
            active = _account_manager.count_active_accounts() if _account_manager else 0
            threshold = max(0, int(cfg.get("AUTO_REGISTER_MIN_ACTIVE") or 0))
            if active >= threshold:
                _auto_replenish_stop.wait(30)
                continue
            batch = max(1, int(cfg.get("AUTO_REGISTER_BATCH") or 1))
            workers = _normalize_workers(cfg.get("AUTO_REGISTER_MAX_WORKERS") or 1, _background_browser_mode())
            _log(f"🔁 触发自动补号：active={active} < threshold={threshold}，准备新增 {batch} 个账号", "INFO")
            _background_batch(batch, workers)
        except Exception as exc:
            _log(f"⚠️ 自动补号循环异常: {exc}", "WARNING")
        _auto_replenish_stop.wait(30)


def start_auto_replenish():
    global _auto_replenish_thread
    if _auto_replenish_thread and _auto_replenish_thread.is_alive():
        return
    _auto_replenish_stop.clear()
    _auto_replenish_thread = threading.Thread(target=_auto_replenish_loop, daemon=True, name="umans-auto-replenish")
    _auto_replenish_thread.start()
