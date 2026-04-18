import html
import json
import random
import re
import string
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests

_VERIFY_URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.I)
_TOKEN_RE = re.compile(r'[?&]token=([^&\s"\'>]+)', re.I)
_rr_lock = threading.Lock()
_rr_cursor = 0


class MoeMailError(RuntimeError):
    pass


def _mail_proxy(cfg: dict) -> str | None:
    if not bool(cfg.get("MAIL_USE_PROXY")):
        return None
    value = str(cfg.get("REGISTER_PROXY") or "").strip()
    return value or None


def _request(method: str, url: str, *, headers=None, json_body=None, proxy: str | None = None, timeout: int = 20, verify: bool = True):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    response = requests.request(
        method,
        url,
        headers=headers or {},
        json=json_body,
        timeout=timeout,
        verify=verify,
        proxies=proxies,
    )
    response.raise_for_status()
    return response


def parse_channels(raw: str) -> tuple[list[dict], str]:
    text = str(raw or "").strip()
    if not text:
        return [], ""
    try:
        data = json.loads(text)
    except Exception as exc:
        return [], str(exc)
    if not isinstance(data, list):
        return [], "MOEMAIL_CHANNELS_JSON must be a JSON array"
    items = []
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "id": str(item.get("id") or f"moemail-{idx}").strip() or f"moemail-{idx}",
                "name": str(item.get("name") or f"MoeMail {idx}").strip() or f"MoeMail {idx}",
                "enabled": bool(item.get("enabled", True)),
                "api_key": str(item.get("api_key") or "").strip(),
                "api_base": str(item.get("api_base") or "").strip().rstrip("/"),
            }
        )
    return items, ""


def get_channel_stats(cfg: dict) -> dict:
    channels, parse_error = parse_channels(cfg.get("MOEMAIL_CHANNELS_JSON") or "")
    enabled = [item for item in channels if item["enabled"] and item["api_key"] and item["api_base"]]
    legacy_ready = bool(str(cfg.get("MOEMAIL_API_KEY") or "").strip() and str(cfg.get("MOEMAIL_API_BASE") or "").strip())
    return {
        "configured": bool(enabled) or legacy_ready,
        "using_json": bool(channels),
        "total": len(channels) if channels else (1 if legacy_ready else 0),
        "enabled": len(enabled) if channels else (1 if legacy_ready else 0),
        "parse_error": parse_error,
    }


def _legacy_channel(cfg: dict) -> dict | None:
    api_key = str(cfg.get("MOEMAIL_API_KEY") or "").strip()
    api_base = str(cfg.get("MOEMAIL_API_BASE") or "").strip().rstrip("/")
    if not api_key or not api_base:
        return None
    return {
        "id": "legacy-moemail",
        "name": "Legacy MoeMail",
        "enabled": True,
        "api_key": api_key,
        "api_base": api_base,
    }


def get_channel_candidates(cfg: dict) -> list[dict]:
    global _rr_cursor
    channels, _ = parse_channels(cfg.get("MOEMAIL_CHANNELS_JSON") or "")
    enabled = [item for item in channels if item["enabled"] and item["api_key"] and item["api_base"]]
    if enabled:
        with _rr_lock:
            start = _rr_cursor % len(enabled)
            ordered = enabled[start:] + enabled[:start]
            _rr_cursor = (_rr_cursor + 1) % len(enabled)
        return ordered
    legacy = _legacy_channel(cfg)
    return [legacy] if legacy else []


def _headers(api_key: str) -> dict:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _get_domains(api_base: str, api_key: str, cfg: dict) -> list[str]:
    response = _request(
        "GET",
        f"{api_base}/api/config",
        headers=_headers(api_key),
        proxy=_mail_proxy(cfg),
        verify=not bool(cfg.get("DISABLE_SSL_VERIFY")),
        timeout=10,
    )
    data = response.json() or {}
    domains = [item.strip() for item in str(data.get("emailDomains") or "").split(",") if item.strip()]
    if not domains:
        raise MoeMailError("MoeMail create failed: no domains available")
    return domains


def create_mailbox(cfg: dict) -> dict:
    candidates = get_channel_candidates(cfg)
    if not candidates:
        raise MoeMailError("MoeMail create failed: no enabled channel configured")
    verify = not bool(cfg.get("DISABLE_SSL_VERIFY"))
    proxy = _mail_proxy(cfg)
    last_error = None
    for channel in candidates:
        try:
            domains = _get_domains(channel["api_base"], channel["api_key"], cfg)
            prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(8, 13)))
            response = _request(
                "POST",
                f"{channel['api_base']}/api/emails/generate",
                headers=_headers(channel["api_key"]),
                json_body={"name": prefix, "domain": random.choice(domains), "expiryTime": 0},
                proxy=proxy,
                verify=verify,
                timeout=15,
            )
            data = response.json() or {}
            email = str(data.get("email") or "").strip()
            auth_credential = str(data.get("id") or "").strip()
            if not email or not auth_credential:
                raise MoeMailError(f"MoeMail create failed: {data}")
            return {
                "email": email,
                "auth_credential": auth_credential,
                "provider": "moemail",
                "api_key": channel["api_key"],
                "api_base": channel["api_base"],
                "channel_id": channel["id"],
                "channel_name": channel["name"],
            }
        except Exception as exc:
            last_error = exc
    raise MoeMailError(f"MoeMail create failed across {len(candidates)} channel(s): {last_error}")


def extract_verify_url(content: str, *, site_base_url: str = "https://app.umans.ai") -> str | None:
    text = html.unescape(str(content or ""))
    for match in _VERIFY_URL_RE.finditer(text):
        url = match.group(0).strip().rstrip(').,;')
        if "verify-email" in url or "token=" in url:
            return url
    token_match = _TOKEN_RE.search(text)
    if token_match:
        return urljoin(site_base_url.rstrip("/") + "/", f"verify-email?token={token_match.group(1)}")
    return None


def extract_verify_token(content: str) -> str | None:
    text = html.unescape(str(content or ""))
    match = _TOKEN_RE.search(text)
    if match:
        return match.group(1)
    url = extract_verify_url(text)
    if not url:
        return None
    parsed = urlsplit(url)
    query = parsed.query or ""
    token_match = _TOKEN_RE.search(f"?{query}")
    return token_match.group(1) if token_match else None


def poll_verification_email(mailbox: dict, *, timeout: int = 180, interval: float = 5.0, site_base_url: str = "https://app.umans.ai") -> dict:
    api_key = str(mailbox.get("api_key") or "").strip()
    api_base = str(mailbox.get("api_base") or "").strip().rstrip("/")
    auth_credential = str(mailbox.get("auth_credential") or "").strip()
    if not api_key or not api_base or not auth_credential:
        raise MoeMailError("MoeMail mailbox metadata incomplete")

    seen = set()
    started = time.time()
    headers = _headers(api_key)
    verify = not bool(mailbox.get("disable_ssl_verify"))
    proxy = mailbox.get("proxy") or None
    while time.time() - started < timeout:
        try:
            messages_response = _request(
                "GET",
                f"{api_base}/api/emails/{auth_credential}",
                headers=headers,
                proxy=proxy,
                verify=verify,
                timeout=15,
            )
            messages = (messages_response.json() or {}).get("messages") or []
            for item in messages:
                if not isinstance(item, dict):
                    continue
                message_id = str(item.get("id") or "").strip()
                if not message_id or message_id in seen:
                    continue
                seen.add(message_id)
                detail_response = _request(
                    "GET",
                    f"{api_base}/api/emails/{auth_credential}/{message_id}",
                    headers=headers,
                    proxy=proxy,
                    verify=verify,
                    timeout=15,
                )
                detail = detail_response.json() or {}
                message = detail.get("message") if isinstance(detail.get("message"), dict) else {}
                subject = str(message.get("subject") or detail.get("subject") or item.get("subject") or "").strip()
                content = message.get("content") or message.get("html") or detail.get("text") or detail.get("html") or ""
                verify_url = extract_verify_url(content, site_base_url=site_base_url)
                verify_token = extract_verify_token(content)
                if verify_url or verify_token:
                    return {
                        "subject": subject,
                        "content": content,
                        "verify_url": verify_url or urljoin(site_base_url.rstrip("/") + "/", f"verify-email?token={verify_token}"),
                        "verify_token": verify_token,
                        "message_id": message_id,
                    }
        except Exception:
            pass
        time.sleep(interval)
    raise MoeMailError("Timed out waiting for verification email")
