import time
from dataclasses import dataclass
from urllib.parse import urlsplit


class BrowserDependencyError(RuntimeError):
    pass


class AuthFlowError(RuntimeError):
    pass


class ManualInterventionRequired(AuthFlowError):
    pass


@dataclass
class BrowserSession:
    playwright: object
    browser: object
    context: object
    page: object
    mode: str
    site_base_url: str
    session_url: str

    def close(self):
        for item in (self.context, self.browser, self.playwright):
            try:
                close = getattr(item, "close", None) or getattr(item, "stop", None)
                if close:
                    close()
            except Exception:
                pass


def _load_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise BrowserDependencyError(
            "缺少 playwright Python 依赖，请先安装 playwright 并执行 playwright install chromium"
        ) from exc
    return sync_playwright, PlaywrightTimeoutError


def is_available() -> tuple[bool, str]:
    try:
        _load_playwright()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _normalize_mode(mode: str) -> str:
    return "visible" if str(mode or "").strip().lower() == "visible" else "headless"


def _build_proxy(proxy: str | None) -> dict | None:
    value = str(proxy or "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return {"server": value}
    payload = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}" if parsed.hostname and parsed.port else value}
    if parsed.username:
        payload["username"] = parsed.username
    if parsed.password:
        payload["password"] = parsed.password
    return payload


def _site_base_url(site_base_url: str | None, session_url: str | None) -> str:
    if site_base_url:
        return site_base_url.rstrip("/")
    if session_url:
        parsed = urlsplit(session_url)
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://app.umans.ai"


def _session_url(site_base_url: str, session_url: str | None) -> str:
    return (session_url or f"{site_base_url}/api/auth/session").rstrip("/")


def _find_submit_button(page):
    candidates = [
        'button[type="submit"]',
        'form button',
        'button:has-text("Create account")',
        'button:has-text("Sign up")',
        'button:has-text("Continue")',
        'button:has-text("Log in")',
        'button:has-text("Login")',
    ]
    for selector in candidates:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                return locator
        except Exception:
            continue
    raise AuthFlowError("未找到提交按钮")


def _challenge_text(page) -> str:
    texts = [
        "verify you are human",
        "captcha",
        "cloudflare",
        "challenge",
        "browser integrity",
        "are you human",
    ]
    try:
        body = (page.text_content("body") or "").lower()
    except Exception:
        body = ""
    for item in texts:
        if item in body:
            return item
    return ""


def _wait_for_verification_state(page, timeout_ms: int, visible_mode: bool, timeout_error_cls):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        url = page.url or ""
        if "verify-email" in url:
            return
        body = (page.text_content("body") or "").lower()
        if "verification email sent" in body or "check your email" in body:
            return
        challenge = _challenge_text(page)
        if challenge:
            if visible_mode:
                time.sleep(2)
                continue
            raise ManualInterventionRequired(f"检测到 challenge: {challenge}")
        time.sleep(0.5)
    raise timeout_error_cls("等待进入 verify-email 状态超时")


def _fetch_session_data(page, session_url: str):
    return page.evaluate(
        """
        async (url) => {
          const res = await fetch(url, { credentials: 'include', cache: 'no-store' });
          const text = await res.text();
          let data = {};
          try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
          return { ok: res.ok, status: res.status, data };
        }
        """,
        session_url,
    )


def _wait_for_logged_in(page, session_url: str, timeout_ms: int):
    deadline = time.time() + timeout_ms / 1000
    last = None
    while time.time() < deadline:
        last = _fetch_session_data(page, session_url)
        user = ((last or {}).get("data") or {}).get("user") or {}
        if last.get("ok") and user:
            return last
        time.sleep(1)
    raise AuthFlowError(f"登录态未建立: {last}")


def _cookies_to_dict(context) -> dict:
    cookies = {}
    for item in context.cookies():
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name and value:
            cookies[name] = value
    return cookies


def start_registration(email: str, password: str, *, mode: str = "visible", proxy: str | None = None,
                       site_base_url: str | None = None, session_url: str | None = None, timeout_ms: int = 60000) -> BrowserSession:
    sync_playwright, timeout_error_cls = _load_playwright()
    normalized_mode = _normalize_mode(mode)
    base_url = _site_base_url(site_base_url, session_url)
    real_session_url = _session_url(base_url, session_url)
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=normalized_mode != "visible", proxy=_build_proxy(proxy))
    context = browser.new_context(ignore_https_errors=True)
    page = context.new_page()
    page.set_default_timeout(timeout_ms)
    page.goto(f"{base_url}/register", wait_until="domcontentloaded")
    page.locator("input#email").fill(email)
    page.locator("input#password").fill(password)
    _find_submit_button(page).click()
    _wait_for_verification_state(page, timeout_ms, normalized_mode == "visible", timeout_error_cls)
    return BrowserSession(playwright=playwright, browser=browser, context=context, page=page, mode=normalized_mode, site_base_url=base_url, session_url=real_session_url)


def complete_registration(session: BrowserSession, verify_url: str, *, timeout_ms: int = 120000) -> dict:
    session.page.goto(verify_url, wait_until="domcontentloaded")
    session_data = _wait_for_logged_in(session.page, session.session_url, timeout_ms)
    return {
        "cookies": _cookies_to_dict(session.context),
        "session": session_data.get("data") or {},
    }


def register_and_export_cookies(email: str, password: str, verify_url: str, mode: str = "visible", proxy: str | None = None,
                                *, site_base_url: str | None = None, session_url: str | None = None,
                                timeout_ms: int = 60000, verify_timeout_ms: int = 120000) -> dict:
    session = start_registration(
        email,
        password,
        mode=mode,
        proxy=proxy,
        site_base_url=site_base_url,
        session_url=session_url,
        timeout_ms=timeout_ms,
    )
    try:
        return complete_registration(session, verify_url, timeout_ms=verify_timeout_ms)
    finally:
        session.close()


def login_and_export_cookies(email: str, password: str, mode: str = "headless", proxy: str | None = None,
                             *, site_base_url: str | None = None, session_url: str | None = None,
                             timeout_ms: int = 60000) -> dict:
    sync_playwright, _ = _load_playwright()
    normalized_mode = _normalize_mode(mode)
    base_url = _site_base_url(site_base_url, session_url)
    real_session_url = _session_url(base_url, session_url)
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=normalized_mode != "visible", proxy=_build_proxy(proxy))
    context = browser.new_context(ignore_https_errors=True)
    page = context.new_page()
    page.set_default_timeout(timeout_ms)
    try:
        page.goto(f"{base_url}/login", wait_until="domcontentloaded")
        page.locator("input#email").fill(email)
        page.locator("input#password").fill(password)
        _find_submit_button(page).click()
        challenge = _challenge_text(page)
        if challenge and normalized_mode != "visible":
            raise ManualInterventionRequired(f"检测到 challenge: {challenge}")
        session_data = _wait_for_logged_in(page, real_session_url, timeout_ms)
        return {
            "cookies": _cookies_to_dict(context),
            "session": session_data.get("data") or {},
        }
    finally:
        for item in (context, browser, playwright):
            try:
                close = getattr(item, "close", None) or getattr(item, "stop", None)
                if close:
                    close()
            except Exception:
                pass
