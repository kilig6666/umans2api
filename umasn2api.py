#!/usr/bin/env python3
"""
umans2api: 把 umans.ai 的私有 chat 接口转换为 Anthropic /v1/messages 兼容接口。
适配 Claude Code (ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN)。

特性:
- Claude 型号自动映射到 umans 上游 (claude-opus-4-7 → coding-model-large 等)
- 通过 prompt 注入 + JSON 解析模拟 tool_use，兼容 Claude Code 的工具调用协议
- 同时暴露 OpenAI /v1/chat/completions 兼容端点
"""
import json
import os
import re
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None
import time
import threading
import uuid
import logging
import hashlib
from copy import deepcopy
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, Response, jsonify, render_template, request, session, stream_with_context

from umans2api.account_manager import AccountManager
from umans2api import auto_register
from umans2api.db import (
    DB_PATH,
    get_request_log,
    get_config_overrides,
    init_db,
    insert_request_log,
    list_request_logs,
    maybe_import_legacy_account,
    get_response_cache,
    prune_response_cache,
    response_cache_stats,
    seed_config_defaults,
    summarize_request_logs,
    upsert_response_cache,
    upsert_config_values,
)
from umans2api.keepalive import KeepAliveService

# ---------- 配置 ----------
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
BACKGROUND_THREADS_ENABLED = os.getenv("UMANS2API_DISABLE_BACKGROUND_THREADS", "0") != "1"
CFG = {}
HOST = "127.0.0.1"
PORT = 8787
API_KEY = ""
ADMIN_TOKEN = ""
UPSTREAM_URL = ""
SITE_BASE_URL = ""
DEFAULT_MODEL = "coding-model"
AVAILABLE_MODELS = []
CONFIGURED_AVAILABLE_MODELS = []
CLAUDE_MODEL_MAP = {}
CLAUDE_KEYWORD_MAP = {}
RAW_COOKIES = {}
APP_SECRET = ""
UPSTREAM_MODEL_CATALOG = []
UPSTREAM_MODEL_ALIAS_MAP = {}
UPSTREAM_MODEL_REFRESHED_AT = 0.0
UPSTREAM_MODEL_REFRESH_TTL = 900
UPSTREAM_MODEL_LOCK = threading.Lock()
_BACKGROUND_LOCK_HANDLE = None
_BACKGROUND_OWNER = False

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("umans2api")

app = Flask(__name__, template_folder=str(ROOT / "templates"))


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


FILE_CONFIG = load_config()
CONFIG_DEFAULTS = {
    "api_key": FILE_CONFIG.get("api_key", ""),
    "admin_token": FILE_CONFIG.get("admin_token") or FILE_CONFIG.get("api_key", ""),
    "default_model": FILE_CONFIG.get("default_model", "umans-coding-model"),
    "keepalive_interval_seconds": FILE_CONFIG.get("keepalive_interval_seconds", 900),
    "keepalive_expiring_minutes": FILE_CONFIG.get("keepalive_expiring_minutes", 20),
    "keepalive_chat_fallback_enabled": FILE_CONFIG.get("keepalive_chat_fallback_enabled", True),
    "fail_threshold": FILE_CONFIG.get("fail_threshold", 3),
    "max_inflight": FILE_CONFIG.get("max_inflight", 2),
    "cooldown_seconds": FILE_CONFIG.get("cooldown_seconds", 120),
    "AUTO_REGISTER_ENABLED": FILE_CONFIG.get("AUTO_REGISTER_ENABLED", False),
    "AUTO_REGISTER_MIN_ACTIVE": FILE_CONFIG.get("AUTO_REGISTER_MIN_ACTIVE", 1),
    "AUTO_REGISTER_BATCH": FILE_CONFIG.get("AUTO_REGISTER_BATCH", 1),
    "AUTO_REGISTER_MAX_WORKERS": FILE_CONFIG.get("AUTO_REGISTER_MAX_WORKERS", 2),
    "AUTO_REGISTER_PASSWORD": FILE_CONFIG.get("AUTO_REGISTER_PASSWORD", ""),
    "AUTO_REGISTER_BROWSER_MODE_MANUAL": FILE_CONFIG.get("AUTO_REGISTER_BROWSER_MODE_MANUAL", "visible"),
    "AUTO_REGISTER_BROWSER_MODE_BACKGROUND": FILE_CONFIG.get("AUTO_REGISTER_BROWSER_MODE_BACKGROUND", "headless"),
    "AUTO_RELOGIN_ENABLED": FILE_CONFIG.get("AUTO_RELOGIN_ENABLED", True),
    "REGISTER_PROXY": FILE_CONFIG.get("REGISTER_PROXY", ""),
    "MAIL_USE_PROXY": FILE_CONFIG.get("MAIL_USE_PROXY", False),
    "MAIL_PROVIDER_DEFAULT": FILE_CONFIG.get("MAIL_PROVIDER_DEFAULT", "moemail"),
    "MOEMAIL_API_KEY": FILE_CONFIG.get("MOEMAIL_API_KEY", ""),
    "MOEMAIL_API_BASE": FILE_CONFIG.get("MOEMAIL_API_BASE", ""),
    "MOEMAIL_CHANNELS_JSON": FILE_CONFIG.get("MOEMAIL_CHANNELS_JSON", ""),
    "RESPONSE_CACHE_ENABLED": FILE_CONFIG.get("RESPONSE_CACHE_ENABLED", True),
    "RESPONSE_CACHE_TTL_SECONDS": FILE_CONFIG.get("RESPONSE_CACHE_TTL_SECONDS", 300),
    "RESPONSE_CACHE_MAX_ENTRIES": FILE_CONFIG.get("RESPONSE_CACHE_MAX_ENTRIES", 1000),
}
CONFIG_DB_KEYS = set(CONFIG_DEFAULTS.keys())


# ---------- 工具函数 ----------
def sanitize_cookies(raw_cookies):
    """
    过滤掉 requests/urllib3 无法编码的 cookie 值，避免请求阶段直接抛 500。
    常见场景是 config.json 里先放了中文占位文本。
    """
    safe = {}
    for key, value in (raw_cookies or {}).items():
        if value is None:
            continue
        if not isinstance(value, str):
            value = str(value)
        if not value:
            continue
        try:
            f"{key}={value}".encode("latin-1")
        except UnicodeEncodeError:
            log.warning("跳过非法 cookie（包含非 latin-1 字符）: %s", key)
            continue
        safe[key] = value
    return safe


_HTTP_LOCAL = threading.local()


def get_http_session() -> requests.Session:
    session_client = getattr(_HTTP_LOCAL, "http_session", None)
    if session_client is not None:
        return session_client
    session_client = requests.Session()
    adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
    session_client.mount("https://", adapter)
    session_client.mount("http://", adapter)
    _HTTP_LOCAL.http_session = session_client
    return session_client


def acquire_background_owner() -> bool:
    global _BACKGROUND_LOCK_HANDLE, _BACKGROUND_OWNER
    if _BACKGROUND_OWNER:
        return True
    if not BACKGROUND_THREADS_ENABLED:
        return False
    if fcntl is None:
        _BACKGROUND_OWNER = True
        return True
    lock_path = Path('/tmp/umans2api.background.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, 'a+')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        log.info('后台线程锁已被其他 worker 占用，当前进程跳过 keepalive/auto-register/model refresh')
        return False
    _BACKGROUND_LOCK_HANDLE = handle
    _BACKGROUND_OWNER = True
    log.info('当前进程获得后台线程锁：%s', lock_path)
    return True


def reload_runtime_config():
    global CFG, HOST, PORT, API_KEY, ADMIN_TOKEN, UPSTREAM_URL
    global SITE_BASE_URL, DEFAULT_MODEL, AVAILABLE_MODELS
    global CONFIGURED_AVAILABLE_MODELS, CLAUDE_MODEL_MAP, CLAUDE_KEYWORD_MAP, RAW_COOKIES, APP_SECRET

    file_cfg = load_config()
    overrides = get_config_overrides()
    CFG = dict(file_cfg)
    CFG.update(overrides)
    HOST = CFG.get("host", "127.0.0.1")
    PORT = int(CFG.get("port", 8787))
    API_KEY = CFG.get("api_key", "")
    ADMIN_TOKEN = CFG.get("admin_token") or API_KEY
    UPSTREAM_URL = CFG["upstream_url"]
    DEFAULT_MODEL = CFG.get("default_model", "coding-model")
    CONFIGURED_AVAILABLE_MODELS = list(CFG.get("available_models", [DEFAULT_MODEL]))
    AVAILABLE_MODELS = list(CONFIGURED_AVAILABLE_MODELS)
    CLAUDE_MODEL_MAP = CFG.get("claude_model_map", {})
    CLAUDE_KEYWORD_MAP = CFG.get("claude_keyword_map", {})
    RAW_COOKIES = CFG.get("cookies", {})
    APP_SECRET = CFG.get("app_secret") or API_KEY or "umans2api-local-secret"
    parsed = urlsplit(UPSTREAM_URL)
    SITE_BASE_URL = f"{parsed.scheme}://{parsed.netloc}"


init_db()
seed_config_defaults(CONFIG_DEFAULTS)
reload_runtime_config()
app.secret_key = APP_SECRET
COOKIES = sanitize_cookies(RAW_COOKIES)
maybe_import_legacy_account(
    name="legacy-default",
    email="",
    cookies=COOKIES,
    allowed_model_prefix=CFG.get("default_account_model_prefix", "umans-"),
)
ACCOUNT_MANAGER = AccountManager(lambda: CFG)
KEEPALIVE = KeepAliveService(ACCOUNT_MANAGER, lambda: CFG, log, UA)
auto_register.configure(
    get_config=lambda: CFG,
    account_manager=ACCOUNT_MANAGER,
    keepalive=KEEPALIVE,
    logger=log,
)
if acquire_background_owner():
    KEEPALIVE.start()
    auto_register.start_auto_replenish()


def gen_uuid() -> str:
    return str(uuid.uuid4())


def merge_unique(*groups):
    items = []
    seen = set()
    for group in groups:
        for item in group or []:
            if not item or item in seen:
                continue
            seen.add(item)
            items.append(item)
    return items


def check_auth() -> bool:
    """校验 Anthropic 风格鉴权头"""
    if not API_KEY:
        return True
    header = (
        request.headers.get("x-api-key")
        or request.headers.get("X-Api-Key")
        or ""
    )
    if header == API_KEY:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[len("Bearer "):] == API_KEY:
        return True
    return False


def get_upstream_probe_cookies() -> dict:
    cookies = sanitize_cookies(RAW_COOKIES)
    if cookies:
        return cookies
    try:
        for item in ACCOUNT_MANAGER.list_accounts():
            if not item.get("enabled"):
                continue
            acc = ACCOUNT_MANAGER.get_account(item["id"], include_cookies=True) or {}
            cookies = sanitize_cookies(acc.get("cookies") or {})
            if cookies:
                return cookies
    except Exception as e:
        log.warning("读取账号 cookie 失败，无法探测上游模型: %s", e)
    return {}


def extract_upstream_models_from_js(js_text: str) -> list[dict]:
    pattern = re.compile(
        r'\{id:"([^"]+)",name:"([^"]+)",description:"([^"]*)",provider:"([^"]+)",providerDisplayName:"([^"]+)"\}'
    )
    items = []
    seen = set()
    for match in pattern.finditer(js_text or ""):
        item = {
            "id": match.group(1),
            "name": match.group(2),
            "description": match.group(3),
            "provider": match.group(4),
            "providerDisplayName": match.group(5),
        }
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        items.append(item)
    return items


def fetch_upstream_model_catalog() -> list[dict]:
    cookies = get_upstream_probe_cookies()
    if not cookies or not SITE_BASE_URL:
        return []
    session_client = get_http_session()
    domain = urlsplit(SITE_BASE_URL).hostname or "app.umans.ai"
    for key, value in cookies.items():
        session_client.cookies.set(key, value, domain=domain)
    html = session_client.get(
        SITE_BASE_URL + "/",
        timeout=20,
        headers={"User-Agent": UA},
    ).text
    scripts = re.findall(r'src="([^"]+_next/static/[^"]+\.js)"', html)
    for src in merge_unique(scripts):
        try:
            js_text = session_client.get(
                urljoin(SITE_BASE_URL, src),
                timeout=20,
                headers={"User-Agent": UA},
            ).text
        except requests.RequestException:
            continue
        models = extract_upstream_models_from_js(js_text)
        if models:
            return models
    return []


def build_upstream_model_alias_map(models: list[dict]) -> dict:
    alias_map = {}
    for item in models or []:
        model_id = (item.get("id") or "").strip()
        if not model_id:
            continue
        if model_id.startswith("umans-"):
            alias_map.setdefault(model_id[len("umans-"):], model_id)
    return alias_map


def refresh_upstream_model_catalog(force: bool = False) -> list[dict]:
    global AVAILABLE_MODELS, UPSTREAM_MODEL_CATALOG, UPSTREAM_MODEL_ALIAS_MAP, UPSTREAM_MODEL_REFRESHED_AT
    now = time.time()
    if not force and UPSTREAM_MODEL_CATALOG and (now - UPSTREAM_MODEL_REFRESHED_AT) < UPSTREAM_MODEL_REFRESH_TTL:
        return UPSTREAM_MODEL_CATALOG
    with UPSTREAM_MODEL_LOCK:
        now = time.time()
        if not force and UPSTREAM_MODEL_CATALOG and (now - UPSTREAM_MODEL_REFRESHED_AT) < UPSTREAM_MODEL_REFRESH_TTL:
            return UPSTREAM_MODEL_CATALOG
        try:
            models = fetch_upstream_model_catalog()
            if models:
                UPSTREAM_MODEL_CATALOG = models
                UPSTREAM_MODEL_ALIAS_MAP = build_upstream_model_alias_map(models)
        except Exception as e:
            log.warning("刷新上游模型目录失败: %s", e)
        AVAILABLE_MODELS = merge_unique(
            [
                item.get("id")
                for item in UPSTREAM_MODEL_CATALOG
                if (item.get("id") or "").startswith("umans-")
            ],
            CONFIGURED_AVAILABLE_MODELS,
        )
        UPSTREAM_MODEL_REFRESHED_AT = now
    return UPSTREAM_MODEL_CATALOG


def build_model_compare() -> dict:
    upstream_ids = [item.get("id") for item in UPSTREAM_MODEL_CATALOG if item.get("id")]
    upstream_umans_ids = [item for item in upstream_ids if item.startswith("umans-")]
    configured_ids = list(CONFIGURED_AVAILABLE_MODELS)
    return {
        "configured_available_models": configured_ids,
        "upstream_available_models": upstream_ids,
        "upstream_umans_models": upstream_umans_ids,
        "effective_available_models": list(AVAILABLE_MODELS),
        "upstream_only": [item for item in upstream_ids if item not in configured_ids],
        "configured_only": [item for item in configured_ids if item not in upstream_ids],
        "short_alias_map": UPSTREAM_MODEL_ALIAS_MAP,
        "claude_map_targets_missing": {
            key: value
            for key, value in CLAUDE_MODEL_MAP.items()
            if value not in upstream_ids
        },
    }


def resolve_model(req_model: str) -> str:
    """
    把客户端传入的 model 转成 umans 上游名字。
    顺序:
        1. 精确命中 available_models → 透传
        2. 精确命中上游短别名（如 glm-5.1 → umans-glm-5.1）
        3. 精确命中 claude_model_map
        4. 名字里含 opus/sonnet/haiku → claude_keyword_map
        5. default
    """
    if not req_model:
        return DEFAULT_MODEL
    refresh_upstream_model_catalog()
    if req_model in AVAILABLE_MODELS:
        return req_model
    if req_model in UPSTREAM_MODEL_ALIAS_MAP:
        return UPSTREAM_MODEL_ALIAS_MAP[req_model]
    if req_model in CLAUDE_MODEL_MAP:
        return CLAUDE_MODEL_MAP[req_model]
    low = req_model.lower()
    for kw, up in CLAUDE_KEYWORD_MAP.items():
        if kw in low:
            return up
    return DEFAULT_MODEL


# ---------- tool_use 协议模拟 ----------
TOOL_SYSTEM_TEMPLATE = """You are connected to a client application through a tool-calling API. The client has registered the tools below and will execute them for you when you request a call. You do not have direct access to the user's environment — only these tools can act on it.

Protocol:

When a tool is the right way to answer, write your reply as exactly one block and nothing else:

<tool_call>
{"name": "<tool_name>", "input": { ... arguments matching the tool's schema ... }}
</tool_call>

The client parses this block, runs the tool, and sends the result back as a tool_result in the next turn. Then you can continue the conversation naturally.

A few notes:

- One tool call per response. Wait for the tool_result before planning the next step.
- Keep the JSON strict (double quotes, no trailing commas, no code fences around it).
- If the question is purely conversational, just reply in plain text — no <tool_call> needed.
- Prefer the registered tools over describing what you would do; the user only sees tool_result output, not narration about tool calls.

Registered tools:

__TOOLS_JSON__
"""

# 识别 <tool_call>{...}</tool_call>，兼容 ```json 代码块包裹
TOOL_CALL_RE = re.compile(
    r"<\s*tool_call\s*>\s*(?:```(?:json)?\s*)?(\{.*?\})\s*(?:```\s*)?<\s*/\s*tool_call\s*>",
    re.DOTALL | re.IGNORECASE,
)
# 兜底：如果模型没加 <tool_call>，但整段就是一个严格 {"name":...,"input":...} JSON，也视为工具调用
TOOL_CALL_BARE_RE = re.compile(
    r'^\s*(\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"input"\s*:\s*\{.*?\}\s*\})\s*$',
    re.DOTALL,
)
ACTION_BLOCK_RE = re.compile(
    r"```json\s+action\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)
ACTION_BARE_RE = re.compile(
    r'^\s*(\{\s*"tool"\s*:\s*"[^"]+"\s*,\s*"parameters"\s*:\s*\{.*?\}\s*\})\s*$',
    re.DOTALL,
)
THINKING_OPEN = "<thinking>"
THINKING_CLOSE = "</thinking>"
RESPONSES_STATE = {}
RESPONSES_STATE_LOCK = threading.Lock()


def build_tools_prompt(tools, tool_choice=None):
    """把工具列表序列化成 system 片段，并带上最小 tool_choice 约束"""
    if not tools:
        return None
    simplified = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        simplified.append(
            {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema", {}),
            }
        )
    if not simplified:
        return None
    suffix = ""
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        if choice_type == "any":
            suffix = "\n\nTool choice constraint: you must call at least one tool in your next response."
        elif choice_type == "tool" and tool_choice.get("name"):
            suffix = f"\n\nTool choice constraint: you must call the tool named {tool_choice['name']} in your next response."
    return TOOL_SYSTEM_TEMPLATE.replace(
        "__TOOLS_JSON__",
        json.dumps(simplified, ensure_ascii=False, indent=2),
    ) + suffix


def _normalize_tool_call_obj(obj):
    if not isinstance(obj, dict):
        return None
    if "name" in obj:
        name = obj.get("name")
        tool_input = obj.get("input", {})
    elif "tool" in obj:
        name = obj.get("tool")
        tool_input = obj.get("parameters", obj.get("input", {}))
    else:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(tool_input, dict):
        tool_input = {}
    return {"name": name.strip(), "input": tool_input}


def parse_tool_calls(text):
    """从文本里找出 1~N 个工具调用，返回 (calls, rest_text)"""
    if not text:
        return [], ""
    matches = []
    for regex in (TOOL_CALL_RE, ACTION_BLOCK_RE):
        for m in regex.finditer(text):
            matches.append((m.start(), m.end(), m.group(1).strip()))
    matches.sort(key=lambda item: item[0])

    calls = []
    rest_parts = []
    cursor = 0
    for start, end, raw_json in matches:
        if start > cursor:
            rest_parts.append(text[cursor:start])
        cursor = end
        try:
            obj = json.loads(raw_json)
        except json.JSONDecodeError:
            rest_parts.append(text[start:end])
            continue
        normalized = _normalize_tool_call_obj(obj)
        if normalized:
            calls.append(normalized)
        else:
            rest_parts.append(text[start:end])
    if cursor < len(text):
        rest_parts.append(text[cursor:])

    if calls:
        return calls, "".join(rest_parts).strip()

    for regex in (TOOL_CALL_BARE_RE, ACTION_BARE_RE):
        m = regex.match(text)
        if not m:
            continue
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        normalized = _normalize_tool_call_obj(obj)
        if normalized:
            return [normalized], ""
    return [], text.strip()


def parse_tool_call(text):
    """兼容旧调用方，只返回第一条工具调用。"""
    calls, rest = parse_tool_calls(text)
    if not calls:
        return None
    first = calls[0]
    return first["name"], first["input"], rest


def extract_thinking_blocks(text: str):
    """提取前导 <thinking>...</thinking>，优先匹配最后一个闭合标签避免提前截断。"""
    if not text:
        return "", ""
    remaining = text.lstrip()
    if not remaining.startswith(THINKING_OPEN):
        return "", text
    end = remaining.rfind(THINKING_CLOSE)
    if end == -1:
        return remaining[len(THINKING_OPEN):].strip(), ""
    return remaining[len(THINKING_OPEN):end].strip(), remaining[end + len(THINKING_CLOSE):].lstrip()


def thinking_prompt(enabled: bool) -> str | None:
    if not enabled:
        return None
    return (
        "Extended reasoning is enabled for this request.\n\n"
        "Before your final answer, think step by step inside <thinking>...</thinking> tags. "
        "After the closing tag, provide the user-facing answer or the tool call output. "
        "Do not leak the thinking tags into the final answer."
    )


def normalize_openai_tools(tools):
    items = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            items.append(
                {
                    "name": fn.get("name"),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
                }
            )
            continue
        items.append(
            {
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema") or tool.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return [x for x in items if x.get("name")]


def normalize_openai_tool_choice(tool_choice):
    if tool_choice in (None, "", "auto"):
        return None
    if tool_choice == "required":
        return {"type": "any"}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function" and isinstance(tool_choice.get("function"), dict):
            name = tool_choice["function"].get("name")
            if name:
                return {"type": "tool", "name": name}
    return None


# ---------- Anthropic → 纯文本 ----------
def anthropic_messages_to_text(system, messages, extra_system=None):
    """
    把 Anthropic /v1/messages 的 messages + system 拍扁成单条 user 文本。
    umans.ai 有自己的强制 system prompt，直接盖不掉；用温和措辞伪装成
    "客户端集成说明" 而不是 "规则 / MUST / FORBIDDEN"，避免被识别成 prompt injection。
    """
    parts = []

    if extra_system:
        parts.append(
            "(Client integration notes — please read before responding.)\n\n"
            + extra_system
        )

    # 用户 system
    sys_parts = []
    if isinstance(system, str) and system.strip():
        sys_parts.append(system.strip())
    elif isinstance(system, list):
        for blk in system:
            if isinstance(blk, dict) and blk.get("type") == "text":
                sys_parts.append(str(blk.get("text", "")))
    if sys_parts:
        parts.append(
            "(Caller's system prompt)\n\n" + "\n\n".join(s for s in sys_parts if s)
        )

    history = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            buf = []
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                t = blk.get("type")
                if t == "text":
                    buf.append(str(blk.get("text", "")))
                elif t == "tool_use":
                    buf.append(
                        "<tool_call>\n"
                        + json.dumps(
                            {"name": blk.get("name"), "input": blk.get("input", {})},
                            ensure_ascii=False,
                        )
                        + "\n</tool_call>"
                    )
                elif t == "tool_result":
                    res = blk.get("content", "")
                    if isinstance(res, list):
                        res = "\n".join(
                            str(x.get("text", "")) if isinstance(x, dict) else str(x)
                            for x in res
                        )
                    tool_use_id = blk.get("tool_use_id", "")
                    buf.append(
                        f"<tool_result id=\"{tool_use_id}\">\n{res}\n</tool_result>"
                    )
                elif t == "image":
                    buf.append("[image omitted]")
            text = "\n".join(buf)
        else:
            text = str(content)

        tag = {
            "user": "User",
            "assistant": "Assistant",
            "system": "System",
            "tool": "Tool",
        }.get(role, role.capitalize())
        history.append(f"[{tag}]\n{text}")

    if history:
        parts.append("\n\n".join(history))

    return "\n\n".join(parts).strip() or "hi"


def openai_content_to_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        buf = []
        for blk in content:
            if not isinstance(blk, dict):
                buf.append(str(blk))
                continue
            if blk.get("type") in {"text", "input_text", "output_text"}:
                buf.append(str(blk.get("text", "")))
            elif blk.get("type") == "image_url":
                buf.append("[image omitted]")
            elif blk.get("type") == "input_image":
                buf.append("[image omitted]")
            elif blk.get("type") == "function_call":
                raw_args = blk.get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    parsed_args = {"_raw": raw_args}
                buf.append(
                    "<tool_call>\n"
                    + json.dumps(
                        {"name": blk.get("name"), "input": parsed_args},
                        ensure_ascii=False,
                    )
                    + "\n</tool_call>"
                )
        return "\n".join(x for x in buf if x)
    if content is None:
        return ""
    return str(content)


def openai_messages_to_anthropic(system_messages, messages):
    system_parts = [x for x in system_messages if x]
    anth_messages = []
    for msg in messages or []:
        role = msg.get("role", "user")
        if role in {"system", "developer"}:
            text = openai_content_to_text(msg.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            anth_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_call_id", ""),
                            "content": openai_content_to_text(msg.get("content")),
                        }
                    ],
                }
            )
            continue
        if role == "assistant":
            blocks = []
            text = openai_content_to_text(msg.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    parsed_args = {"_raw": raw_args}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:24]),
                        "name": fn.get("name"),
                        "input": parsed_args if isinstance(parsed_args, dict) else {"_raw": raw_args},
                    }
                )
            anth_messages.append({"role": "assistant", "content": blocks or text or ""})
            continue
        anth_messages.append({"role": "user", "content": openai_content_to_text(msg.get("content"))})
    return "\n\n".join(system_parts).strip(), anth_messages


def build_upstream_payload(model: str, prompt_text: str):
    chat_id = gen_uuid()
    msg_id = gen_uuid()
    payload = {
        "selectedChatModel": model,
        "id": chat_id,
        "messages": [
            {
                "role": "user",
                "parts": [{"type": "text", "text": prompt_text}],
                "id": msg_id,
            }
        ],
        "knowledgeBaseId": None,
    }
    return payload, chat_id


def build_upstream_headers(chat_id: str):
    return {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache",
        "Origin": "https://app.umans.ai",
        "Referer": f"https://app.umans.ai/chat/{chat_id}",
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Pragma": "no-cache",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }


def iter_upstream_events(resp):
    """逐行解析 SSE 数据"""
    for raw in resp.iter_lines():
        if raw is None:
            continue
        if isinstance(raw, bytes):
            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                continue
        else:
            line = raw.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            yield {"__done__": True}
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            log.warning("跳过无法解析的 SSE 行: %s", data[:200])


# ---------- Anthropic SSE 输出 ----------
def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def collect_full_text(upstream_resp):
    """
    把所有 text-delta 拼起来；同时收集上游原生 tool_use 事件。
    返回 (full_text, usage_in, usage_out, native_tool_calls)
    native_tool_calls: [{"id": ..., "name": ..., "input": {...}}]
    """
    chunks = []
    usage_in = 0
    usage_out = 0
    # 按 toolCallId 汇总
    tool_acc = {}  # id -> {"name": str, "input_text": str, "input": dict}
    tool_order = []
    try:
        for ev in iter_upstream_events(upstream_resp):
            if ev.get("__done__"):
                break
            t = ev.get("type")
            if t == "text-delta":
                chunks.append(ev.get("delta", ""))
            elif t == "tool-input-start":
                tid = ev.get("toolCallId") or ev.get("toolCallID") or ev.get("id")
                if tid and tid not in tool_acc:
                    tool_acc[tid] = {
                        "name": ev.get("toolName") or ev.get("name") or "",
                        "input_text": "",
                        "input": None,
                    }
                    tool_order.append(tid)
            elif t == "tool-input-delta":
                tid = ev.get("toolCallId") or ev.get("toolCallID") or ev.get("id")
                if tid in tool_acc:
                    tool_acc[tid]["input_text"] += ev.get("inputTextDelta", "")
            elif t == "tool-input-available":
                tid = ev.get("toolCallId") or ev.get("toolCallID") or ev.get("id")
                if tid in tool_acc:
                    tool_acc[tid]["input"] = ev.get("input") or {}
                    if not tool_acc[tid]["name"]:
                        tool_acc[tid]["name"] = ev.get("toolName") or ""
            elif t == "finish":
                meta = ev.get("messageMetadata", {}) or {}
                usage = meta.get("usage", {}) or {}
                usage_in = int(usage.get("inputTokens", 0) or 0)
                usage_out = int(usage.get("outputTokens", 0) or 0)
    except requests.exceptions.RequestException as e:
        log.warning("收集上游响应时流被提前中断，已使用已拿到的片段继续返回: %s", e)

    native_tools = []
    for tid in tool_order:
        t = tool_acc[tid]
        inp = t["input"]
        if inp is None and t["input_text"]:
            try:
                inp = json.loads(t["input_text"])
            except json.JSONDecodeError:
                inp = {"_raw": t["input_text"]}
        if inp is None:
            inp = {}
        native_tools.append({"id": tid, "name": t["name"], "input": inp})

    return "".join(chunks), usage_in, usage_out, native_tools


def build_tool_use_blocks(full_text, native_tools=None, wants_thinking=False):
    """
    若上游原生 tool_use 存在，优先用原生的；
    否则回退到从文本里提取 <tool_call>。
    返回 (stop_reason, content_blocks, reasoning_text).
    """
    reasoning_text, visible_text = extract_thinking_blocks(full_text)
    blocks = []
    if wants_thinking and reasoning_text:
        blocks.append({"type": "thinking", "thinking": reasoning_text, "signature": ""})
    if native_tools:
        if visible_text.strip():
            blocks.append({"type": "text", "text": visible_text})
        for t in native_tools:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": t["id"] or ("toolu_" + uuid.uuid4().hex[:24]),
                    "name": t["name"],
                    "input": t["input"] or {},
                }
            )
        return "tool_use", blocks, reasoning_text

    parsed_calls, rest = parse_tool_calls(visible_text)
    if parsed_calls:
        if rest:
            blocks.append({"type": "text", "text": rest})
        for call in parsed_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": "toolu_" + uuid.uuid4().hex[:24],
                    "name": call["name"],
                    "input": call["input"],
                }
            )
        return "tool_use", blocks, reasoning_text

    if visible_text:
        blocks.append({"type": "text", "text": visible_text})
    return "end_turn", blocks or [{"type": "text", "text": ""}], reasoning_text


def anthropic_stream(upstream_resp, model_for_output: str, has_tools: bool, wants_thinking: bool = False, capture: dict | None = None):
    """
    把 umans SSE 转成 Anthropic 流式格式。
    如果声明了 tools，先收齐全部文本再判断是否是 tool_call，
    这样可以保证 JSON 不被截断到中途。
    """
    msg_id = "msg_" + uuid.uuid4().hex[:24]

    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model_for_output,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    # ----- 有 tools: 先缓存 -----
    if has_tools or wants_thinking:
        full, usage_in, usage_out, native_tools = collect_full_text(upstream_resp)
        stop_reason, blocks, _reasoning = build_tool_use_blocks(full, native_tools, wants_thinking=wants_thinking)
        if capture is not None:
            capture.update(
                {
                    "full_text": full,
                    "usage_in": usage_in,
                    "usage_out": usage_out or estimate_text_tokens(full),
                    "stop_reason": stop_reason,
                    "content_blocks": deepcopy(blocks),
                    "reasoning_text": _reasoning or "",
                }
            )

        for idx, blk in enumerate(blocks):
            if blk["type"] == "thinking":
                yield sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                    },
                )
                if blk["thinking"]:
                    yield sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": idx,
                            "delta": {"type": "thinking_delta", "thinking": blk["thinking"]},
                        },
                    )
                yield sse("content_block_stop", {"type": "content_block_stop", "index": idx})
            elif blk["type"] == "text":
                yield sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                if blk["text"]:
                    yield sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": idx,
                            "delta": {"type": "text_delta", "text": blk["text"]},
                        },
                    )
                yield sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": idx},
                )
            elif blk["type"] == "tool_use":
                yield sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": blk["id"],
                            "name": blk["name"],
                            "input": {},
                        },
                    },
                )
                yield sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(blk["input"], ensure_ascii=False),
                        },
                    },
                )
                yield sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": idx},
                )

        yield sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": usage_out or max(1, len(full) // 4)},
            },
        )
        yield sse("message_stop", {"type": "message_stop"})
        return

    # ----- 无 tools: 实时流 -----
    block_open = False
    output_text_len = 0
    usage_out = 0
    stop_reason = "end_turn"
    text_parts = []

    try:
        for ev in iter_upstream_events(upstream_resp):
            if ev.get("__done__"):
                break
            t = ev.get("type")
            if t == "text-start":
                if not block_open:
                    yield sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    block_open = True
            elif t == "text-delta":
                delta = ev.get("delta", "")
                if not delta:
                    continue
                if not block_open:
                    yield sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    block_open = True
                output_text_len += len(delta)
                text_parts.append(delta)
                yield sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": delta},
                    },
                )
            elif t == "text-end":
                if block_open:
                    yield sse(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": 0},
                    )
                    block_open = False
            elif t == "finish":
                meta = ev.get("messageMetadata", {}) or {}
                usage = meta.get("usage", {}) or {}
                usage_out = int(usage.get("outputTokens", 0) or 0)
            elif t == "error":
                err = ev.get("errorText") or ev.get("error") or "upstream error"
                if not block_open:
                    yield sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    block_open = True
                yield sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": f"\n[upstream error] {err}"},
                    },
                )
    except (requests.exceptions.RequestException, GeneratorExit) as e:
        log.warning("流式中断: %s", e)

    if block_open:
        yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})

    yield sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": usage_out or max(1, output_text_len // 4)},
        },
    )
    yield sse("message_stop", {"type": "message_stop"})
    if capture is not None:
        visible_text = "".join(text_parts)
        capture.update(
            {
                "full_text": visible_text,
                "usage_in": 0,
                "usage_out": usage_out or max(1, output_text_len // 4),
                "stop_reason": stop_reason,
                "content_blocks": [{"type": "text", "text": visible_text}] if visible_text else [{"type": "text", "text": ""}],
                "reasoning_text": "",
            }
        )


def check_admin_auth() -> bool:
    if session.get("admin_authed"):
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[len("Bearer "):] == ADMIN_TOKEN:
        return True
    return False


def admin_error(message: str, status: int = 401):
    return jsonify({"ok": False, "message": message}), status


def ensure_admin():
    if not check_admin_auth():
        return admin_error("unauthorized", 401)
    return None


def build_runtime_info():
    refresh_upstream_model_catalog()
    return {
        "service": "umans2api",
        "ok": True,
        "host": HOST,
        "port": PORT,
        "db_path": str(DB_PATH),
        "upstream_url": UPSTREAM_URL,
        "site_base_url": SITE_BASE_URL,
        "default_model": DEFAULT_MODEL,
        "available_models": AVAILABLE_MODELS,
        "configured_available_models": CONFIGURED_AVAILABLE_MODELS,
        "claude_model_map": CLAUDE_MODEL_MAP,
        "upstream_model_catalog": UPSTREAM_MODEL_CATALOG,
        "model_compare": build_model_compare(),
        "summary": ACCOUNT_MANAGER.summary(),
    }


def get_response_state(response_id: str):
    if not response_id:
        return None
    with RESPONSES_STATE_LOCK:
        return deepcopy(RESPONSES_STATE.get(response_id))


def update_response_state(response_id: str, messages):
    if not response_id:
        return
    with RESPONSES_STATE_LOCK:
        RESPONSES_STATE[response_id] = {"messages": deepcopy(messages or []), "updated_at": time.time()}
        stale = sorted(RESPONSES_STATE.items(), key=lambda item: item[1].get("updated_at", 0), reverse=True)[80:]
        for key, _ in stale:
            RESPONSES_STATE.pop(key, None)


def _is_auth_invalid(status_code: int, text: str) -> bool:
    low = (text or "").lower()
    if status_code in (302, 307, 308):
        return True
    if status_code == 401:
        return True
    if status_code == 403 and (
        "login" in low or "session" in low or "auth" in low or "callbackurl" in low
    ):
        return True
    return False


def _attempt_upstream_request(
    payload: dict,
    upstream_model: str,
    *,
    path: str,
    client_model: str,
    request_body=None,
    prompt_text: str = "",
    client_stream: bool = False,
    tool_count: int = 0,
    previous_response_id: str = "",
):
    exclude_ids = set()
    last_error = "no account available"
    last_status = 503
    attempt_limit = 2

    for _ in range(attempt_limit):
        account = ACCOUNT_MANAGER.reserve_next(upstream_model, exclude_ids=exclude_ids)
        if not account:
            break
        exclude_ids.add(account["id"])
        headers = build_upstream_headers(payload["id"])
        started = time.time()
        try:
            upstream = get_http_session().post(
                UPSTREAM_URL,
                headers=headers,
                cookies=account.get("cookies") or {},
                json=payload,
                stream=True,
                allow_redirects=False,
                timeout=300,
            )
        except (requests.exceptions.RequestException, UnicodeEncodeError) as e:
            last_error = str(e)
            last_status = 502
            ACCOUNT_MANAGER.mark_fail(account["id"], last_error)
            ACCOUNT_MANAGER.release_reservation(account["id"])
            insert_request_log(
                path=path,
                api_format=infer_api_format(path),
                stream=client_stream,
                client_model=client_model,
                upstream_model=upstream_model,
                account_id=account["id"],
                account_name=account.get("name") or "",
                ok=False,
                status_code=last_status,
                duration_ms=int((time.time() - started) * 1000),
                error=last_error,
                tool_count=tool_count,
                detail=build_log_detail(
                    request_body=request_body,
                    prompt_text=prompt_text,
                    previous_response_id=previous_response_id,
                    note="上游连接阶段失败",
                    phases=[
                        {"phase": "reserve", "offset_ms": 0, "event_name": "account_reserved", "payload": {"account_id": account["id"]}},
                        {"phase": "dispatch", "offset_ms": int((time.time() - started) * 1000), "event_name": "upstream_connect_error", "payload": {"error": last_error}},
                    ],
                ),
            )
            continue

        ACCOUNT_MANAGER.merge_response_cookies(account["id"], upstream.cookies)
        if upstream.status_code == 200:
            return upstream, account, started

        text = upstream.text[:500]
        last_error = f"upstream {upstream.status_code}: {text}"
        last_status = 502
        ACCOUNT_MANAGER.mark_fail(
            account["id"],
            last_error,
            auth_invalid=_is_auth_invalid(upstream.status_code, text),
        )
        ACCOUNT_MANAGER.release_reservation(account["id"])
        insert_request_log(
            path=path,
            api_format=infer_api_format(path),
            stream=client_stream,
            client_model=client_model,
            upstream_model=upstream_model,
            account_id=account["id"],
            account_name=account.get("name") or "",
            ok=False,
            status_code=upstream.status_code,
            duration_ms=int((time.time() - started) * 1000),
            error=last_error,
            tool_count=tool_count,
            detail=build_log_detail(
                request_body=request_body,
                prompt_text=prompt_text,
                previous_response_id=previous_response_id,
                note="上游返回非 200",
                phases=[
                    {"phase": "reserve", "offset_ms": 0, "event_name": "account_reserved", "payload": {"account_id": account["id"]}},
                    {"phase": "dispatch", "offset_ms": int((time.time() - started) * 1000), "event_name": "upstream_non_200", "payload": {"status_code": upstream.status_code, "body_preview": text}},
                ],
            ),
        )

    return None, None, None, last_status, last_error


def wants_anthropic_thinking(body):
    thinking = body.get("thinking")
    return isinstance(thinking, dict) and thinking.get("type") in {"enabled", "adaptive"}


def wants_openai_reasoning(body):
    if body.get("reasoning_effort"):
        return True
    reasoning = body.get("reasoning")
    return isinstance(reasoning, dict) and bool(reasoning.get("effort"))


def build_openai_tool_calls(full_text, native_tools=None):
    reasoning_text, visible_text = extract_thinking_blocks(full_text)
    tool_defs = []
    if native_tools:
        tool_defs = native_tools
    else:
        parsed_calls, rest = parse_tool_calls(visible_text)
        if parsed_calls:
            visible_text = rest
            tool_defs = [{"id": "call_" + uuid.uuid4().hex[:24], "name": call["name"], "input": call["input"]} for call in parsed_calls]
    tool_calls = []
    for item in tool_defs:
        tool_calls.append(
            {
                "id": item.get("id") or ("call_" + uuid.uuid4().hex[:24]),
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": json.dumps(item.get("input") or {}, ensure_ascii=False),
                },
            }
        )
    return visible_text, reasoning_text, tool_calls


def chunk_string(text: str, size: int = 160):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def write_openai_chunk(chunk: dict):
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def build_responses_usage(input_tokens: int, output_tokens: int):
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def estimate_text_tokens(text: str) -> int:
    return max(1, len(text or "") // 4) if text else 0


def build_usage_metrics(
    *,
    input_tokens: int,
    output_tokens: int,
    reasoning_text: str = "",
    cache_hit: bool = False,
    cache_created: bool = False,
) -> dict:
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    reasoning_tokens = estimate_text_tokens(reasoning_text)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_hit": bool(cache_hit),
        "cache_read_input_tokens": input_tokens if cache_hit else 0,
        "cache_creation_input_tokens": input_tokens if cache_created else 0,
    }


def attach_usage_details(
    api_format: str,
    usage: dict | None,
    *,
    reasoning_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> dict:
    payload = dict(usage or {})
    if api_format == "anthropic":
        if cache_read_input_tokens:
            payload["cache_read_input_tokens"] = int(cache_read_input_tokens)
        if cache_creation_input_tokens:
            payload["cache_creation_input_tokens"] = int(cache_creation_input_tokens)
        return payload

    if api_format == "openai":
        prompt_details = dict(payload.get("prompt_tokens_details") or {})
        completion_details = dict(payload.get("completion_tokens_details") or {})
        if cache_read_input_tokens:
            prompt_details["cached_tokens"] = int(cache_read_input_tokens)
        if reasoning_tokens:
            completion_details["reasoning_tokens"] = int(reasoning_tokens)
        if prompt_details:
            payload["prompt_tokens_details"] = prompt_details
        if completion_details:
            payload["completion_tokens_details"] = completion_details
        return payload

    input_details = dict(payload.get("input_tokens_details") or {})
    output_details = dict(payload.get("output_tokens_details") or {})
    if cache_read_input_tokens:
        input_details["cached_tokens"] = int(cache_read_input_tokens)
    if reasoning_tokens:
        output_details["reasoning_tokens"] = int(reasoning_tokens)
    if input_details:
        payload["input_tokens_details"] = input_details
    if output_details:
        payload["output_tokens_details"] = output_details
    return payload


def get_request_scope_key() -> str:
    auth = request.headers.get("x-api-key") or request.headers.get("X-Api-Key") or ""
    if not auth:
        bearer = request.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            auth = bearer[len("Bearer "):]
    raw = auth or "public"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_response_cache_enabled(path: str, *, tool_count: int = 0) -> bool:
    if not bool(CFG.get("RESPONSE_CACHE_ENABLED", True)):
        return False
    if tool_count:
        return False
    return path in {"/v1/messages", "/v1/chat/completions", "/v1/responses"}


def build_response_cache_identity(path: str, upstream_model: str, prompt_text: str) -> tuple[str, str, str]:
    scope_key = get_request_scope_key()
    prompt_hash = hashlib.sha256((prompt_text or "").encode("utf-8")).hexdigest()
    raw = json.dumps(
        {
            "scope": scope_key,
            "path": path,
            "model": upstream_model,
            "prompt_hash": prompt_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    cache_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return cache_key, scope_key, prompt_hash


def get_cached_response(path: str, upstream_model: str, prompt_text: str, *, tool_count: int = 0) -> dict | None:
    if not is_response_cache_enabled(path, tool_count=tool_count):
        return None
    cache_key, scope_key, prompt_hash = build_response_cache_identity(path, upstream_model, prompt_text)
    item = get_response_cache(cache_key)
    if not item:
        return None
    payload = item.get("payload") or {}
    if payload.get("scope_key") != scope_key or payload.get("prompt_hash") != prompt_hash:
        return None
    return payload


def store_cached_response(
    *,
    path: str,
    api_format: str,
    upstream_model: str,
    prompt_text: str,
    tool_count: int,
    response_body: dict,
):
    if not is_response_cache_enabled(path, tool_count=tool_count):
        return False
    cache_key, scope_key, prompt_hash = build_response_cache_identity(path, upstream_model, prompt_text)
    payload = {
        "scope_key": scope_key,
        "prompt_hash": prompt_hash,
        "api_format": api_format,
        "path": path,
        "response": response_body,
    }
    upsert_response_cache(
        cache_key=cache_key,
        scope_key=scope_key,
        api_format=api_format,
        model=upstream_model,
        prompt_hash=prompt_hash,
        payload=payload,
        ttl_seconds=int(CFG.get("RESPONSE_CACHE_TTL_SECONDS", 300) or 300),
    )
    prune_response_cache(int(CFG.get("RESPONSE_CACHE_MAX_ENTRIES", 1000) or 1000))
    return True


def clone_cached_response(payload: dict) -> dict:
    response_body = deepcopy((payload or {}).get("response") or {})
    api_format = (payload or {}).get("api_format") or ""
    now = int(time.time())
    if api_format == "anthropic":
        response_body["id"] = "msg_" + uuid.uuid4().hex[:24]
    elif api_format == "openai":
        response_body["id"] = "chatcmpl-" + uuid.uuid4().hex[:24]
        response_body["created"] = now
    elif api_format == "responses":
        response_body["id"] = "resp_" + uuid.uuid4().hex[:24]
        response_body["created_at"] = now
    return response_body


def anthropic_stream_from_cached(response_body: dict):
    msg_id = response_body.get("id") or ("msg_" + uuid.uuid4().hex[:24])
    usage = response_body.get("usage") or {}
    content = response_body.get("content") or []
    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": response_body.get("model"),
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": int(usage.get("input_tokens") or 0), "output_tokens": 0},
            },
        },
    )
    for idx, blk in enumerate(content):
        if blk.get("type") == "thinking":
            yield sse("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            if blk.get("thinking"):
                yield sse("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "thinking_delta", "thinking": blk.get("thinking", "")}})
            yield sse("content_block_stop", {"type": "content_block_stop", "index": idx})
            continue
        yield sse("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})
        if blk.get("text"):
            yield sse("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": blk.get("text", "")}})
        yield sse("content_block_stop", {"type": "content_block_stop", "index": idx})
    yield sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": response_body.get("stop_reason") or "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": int(usage.get("output_tokens") or 0)},
        },
    )
    yield sse("message_stop", {"type": "message_stop"})


def openai_stream_from_cached(response_body: dict):
    message = (((response_body or {}).get("choices") or [{}])[0].get("message") or {})
    usage = (response_body or {}).get("usage") or {}
    cmpl_id = response_body.get("id") or ("chatcmpl-" + uuid.uuid4().hex[:24])
    created = int(response_body.get("created") or time.time())
    model = response_body.get("model")
    yield write_openai_chunk(
        {
            "id": cmpl_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        }
    )
    reasoning_text = message.get("reasoning_content") or ""
    for chunk in chunk_string(reasoning_text):
        if reasoning_text:
            yield write_openai_chunk(
                {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"reasoning_content": chunk}, "finish_reason": None}],
                }
            )
    text = message.get("content") or ""
    for chunk in chunk_string(text):
        if text:
            yield write_openai_chunk(
                {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                }
            )
    done = {
        "id": cmpl_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": (((response_body.get("choices") or [{}])[0].get("finish_reason")) or "stop")}],
        "usage": usage,
    }
    yield write_openai_chunk(done)
    yield "data: [DONE]\n\n"


def responses_stream_from_cached(response_body: dict):
    payload = deepcopy(response_body or {})
    response_id = payload.get("id") or ("resp_" + uuid.uuid4().hex[:24])
    model = payload.get("model")
    usage = payload.get("usage") or {}
    output_items = payload.get("output") or []
    yield responses_sse("response.created", {"response": build_response_object(response_id, model, "in_progress", [])})
    yield responses_sse("response.in_progress", {"response": build_response_object(response_id, model, "in_progress", [])})
    for idx, item in enumerate(output_items):
        if item.get("type") == "reasoning":
            in_progress = {"id": item.get("id"), "type": "reasoning", "summary": [], "status": "in_progress"}
            yield responses_sse("response.output_item.added", {"output_index": idx, "item": in_progress})
            text = (((item.get("summary") or [{}])[0]).get("text") or "")
            for chunk in chunk_string(text, 160):
                yield responses_sse("response.reasoning_summary_text.delta", {"output_index": idx, "summary_index": 0, "delta": chunk})
            yield responses_sse("response.reasoning_summary_text.done", {"output_index": idx, "summary_index": 0, "text": text})
            yield responses_sse("response.output_item.done", {"output_index": idx, "item": item})
            continue
        if item.get("type") == "message":
            in_progress = {"id": item.get("id"), "type": "message", "role": "assistant", "status": "in_progress", "content": []}
            yield responses_sse("response.output_item.added", {"output_index": idx, "item": in_progress})
            yield responses_sse("response.content_part.added", {"output_index": idx, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
            text = ((((item.get("content") or [{}])[0]).get("text")) or "")
            for chunk in chunk_string(text, 160):
                yield responses_sse("response.output_text.delta", {"output_index": idx, "content_index": 0, "delta": chunk})
            part = {"type": "output_text", "text": text, "annotations": []}
            yield responses_sse("response.output_text.done", {"output_index": idx, "content_index": 0, "text": text})
            yield responses_sse("response.content_part.done", {"output_index": idx, "content_index": 0, "part": part})
            yield responses_sse("response.output_item.done", {"output_index": idx, "item": item})
    yield responses_sse("response.completed", {"response": build_response_object(response_id, model, "completed", output_items, usage)})


def responses_to_openai_request(body):
    items = body.get("input")
    previous_messages = []
    previous_response_id = body.get("previous_response_id") or ""
    saw_function_call_output = False
    if previous_response_id:
        state = get_response_state(previous_response_id)
        if state:
            previous_messages = state.get("messages") or []

    messages = list(previous_messages)
    instructions = body.get("instructions")
    if isinstance(items, str):
        messages.append({"role": "user", "content": items})
    elif isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call_output":
                output = item.get("output", "")
                if not isinstance(output, (str, list)):
                    output = json.dumps(output, ensure_ascii=False)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.get("call_id", ""),
                        "content": output,
                    }
                )
                saw_function_call_output = True
                continue
            role = item.get("role", "user")
            content = item.get("content", "")
            if role in {"system", "developer"} and instructions:
                instructions += "\n\n" + openai_content_to_text(content)
            elif role in {"system", "developer"}:
                instructions = openai_content_to_text(content)
            else:
                messages.append({"role": role, "content": content})
    if saw_function_call_output:
        instructions = (
            ((instructions + "\n\n") if instructions else "")
            + "You have just received a tool_result for a previously requested tool call. Treat that tool_result as authoritative execution output. Unless another distinct tool call is strictly necessary, do not call the same tool again. Continue naturally and give the user the final answer based on the tool_result."
        )
    return {
        "model": body.get("model") or DEFAULT_MODEL,
        "messages": messages,
        "stream": bool(body.get("stream", False)),
        "tools": body.get("tools") or [],
        "reasoning_effort": (body.get("reasoning") or {}).get("effort") if isinstance(body.get("reasoning"), dict) else body.get("reasoning_effort"),
        "instructions": instructions or "",
    }


def responses_sse(event: str, data: dict):
    return f"event: {event}\ndata: {json.dumps({'type': event, **data}, ensure_ascii=False)}\n\n"


def build_response_object(response_id: str, model: str, status: str, output: list, usage: dict | None = None):
    payload = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": output,
    }
    if usage:
        payload["usage"] = usage
    return payload


def infer_api_format(path: str) -> str:
    if "responses" in (path or ""):
        return "responses"
    if "chat/completions" in (path or ""):
        return "openai"
    return "anthropic"


def short_preview(value, limit: int = 600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …"


def build_log_detail(
    *,
    request_body=None,
    response_body=None,
    tool_calls=None,
    reasoning_text="",
    prompt_text="",
    previous_response_id="",
    note="",
    phases=None,
):
    timeline = [item for item in (phases or []) if isinstance(item, dict)]
    if not any(item.get("phase") == "dispatch" for item in timeline):
        tool_count = len(tool_calls or [])
        if not tool_count and isinstance(request_body, dict) and isinstance(request_body.get("tools"), list):
            tool_count = len(request_body.get("tools") or [])
        dispatch_payload = {"tool_count": tool_count, "has_reasoning": bool(reasoning_text)}
        if previous_response_id:
            dispatch_payload["previous_response_id"] = previous_response_id
        timeline.insert(
            1 if timeline else 0,
            {
                "phase": "dispatch",
                "offset_ms": 0,
                "event_name": "request_dispatched",
                "payload": dispatch_payload,
            },
        )
    return {
        "request": {
            "preview": short_preview(request_body or {}),
            "prompt_preview": short_preview(prompt_text, 900) if prompt_text else "",
            "previous_response_id": previous_response_id or "",
        },
        "response": {
            "preview": short_preview(response_body or {}),
            "reasoning_preview": short_preview(reasoning_text, 900) if reasoning_text else "",
            "tool_calls": tool_calls or [],
        },
        "timeline": timeline,
        "note": note or "",
    }


def extract_reasoning_text(api_format: str, response_body: dict) -> str:
    if api_format == "anthropic":
        return "\n\n".join(
            item.get("thinking", "")
            for item in (response_body.get("content") or [])
            if item.get("type") == "thinking" and item.get("thinking")
        )
    if api_format == "openai":
        return (((response_body.get("choices") or [{}])[0].get("message") or {}).get("reasoning_content")) or ""
    return "\n\n".join(
        ((item.get("summary") or [{}])[0].get("text") or "")
        for item in (response_body.get("output") or [])
        if item.get("type") == "reasoning"
    )


def extract_usage_metrics(api_format: str, response_body: dict) -> dict:
    usage = response_body.get("usage") or {}
    if api_format == "anthropic":
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
    elif api_format == "openai":
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
    else:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
    reasoning_text = extract_reasoning_text(api_format, response_body)
    return build_usage_metrics(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_text=reasoning_text,
    )


# ---------- 路由 ----------
def _normalize_auto_register_count(value) -> int:
    try:
        count = int(value)
    except Exception:
        count = 1
    return max(1, min(count, 20))


@app.route("/", methods=["GET"])
def root():
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify(build_runtime_info())


@app.route("/api/runtime", methods=["GET"])
def api_runtime():
    guard = ensure_admin()
    if guard:
        return guard
    return jsonify(build_runtime_info())


@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(force=True, silent=True) or {}
    token = (body.get("token") or "").strip()
    if token != ADMIN_TOKEN:
        return admin_error("invalid admin token", 401)
    session["admin_authed"] = True
    return jsonify({"ok": True})


@app.route("/api/auth/check", methods=["GET"])
def api_auth_check():
    if not check_admin_auth():
        return admin_error("unauthorized", 401)
    return jsonify({"ok": True})


@app.route("/api/stats", methods=["GET"])
def api_stats():
    guard = ensure_admin()
    if guard:
        return guard
    return jsonify(ACCOUNT_MANAGER.summary())


@app.route("/api/usage", methods=["GET"])
def api_usage():
    guard = ensure_admin()
    if guard:
        return guard
    return jsonify(
        {
            "usage": summarize_request_logs(),
            "cache": response_cache_stats(),
        }
    )


@app.route("/api/logs", methods=["GET"])
def api_logs():
    guard = ensure_admin()
    if guard:
        return guard
    limit = int(request.args.get("limit", 100) or 100)
    return jsonify({"logs": list_request_logs(limit)})


@app.route("/api/logs/<log_id>", methods=["GET"])
def api_log_detail(log_id):
    guard = ensure_admin()
    if guard:
        return guard
    item = get_request_log(log_id)
    if not item:
        return admin_error("log not found", 404)
    return jsonify({"log": item})


@app.route("/api/accounts", methods=["GET"])
def api_accounts():
    guard = ensure_admin()
    if guard:
        return guard
    account_id = (request.args.get("account_id") or "").strip()
    include_cookies = request.args.get("include_cookies") in {"1", "true", "yes"}
    if account_id:
        acc = ACCOUNT_MANAGER.get_account(account_id, include_cookies=include_cookies)
        if not acc:
            return admin_error("account not found", 404)
        return jsonify({"account": acc})
    return jsonify({"accounts": ACCOUNT_MANAGER.list_accounts()})


@app.route("/api/accounts", methods=["POST"])
def api_add_account():
    guard = ensure_admin()
    if guard:
        return guard
    body = request.get_json(force=True, silent=True) or {}
    try:
        acc = ACCOUNT_MANAGER.add_account(
            name=body.get("name", ""),
            email=body.get("email", ""),
            password=body.get("password", ""),
            cookies=body.get("cookies_json") or body.get("cookies") or {},
            allowed_model_prefix=body.get("allowed_model_prefix") or "umans-",
            enabled=bool(body.get("enabled", True)),
            register_source=body.get("register_source") or "",
            auth_mode=body.get("auth_mode") or "",
        )
    except Exception as e:
        return admin_error(str(e), 400)
    KEEPALIVE.check_account(acc["id"])
    return jsonify({"ok": True, "account": ACCOUNT_MANAGER.get_account(acc["id"])})


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def api_delete_account(account_id):
    guard = ensure_admin()
    if guard:
        return guard
    return jsonify({"ok": ACCOUNT_MANAGER.delete_account(account_id)})


@app.route("/api/accounts/<account_id>", methods=["GET"])
def api_get_account(account_id):
    guard = ensure_admin()
    if guard:
        return guard
    include_cookies = request.args.get("include_cookies") in {"1", "true", "yes"}
    acc = ACCOUNT_MANAGER.get_account(account_id, include_cookies=include_cookies)
    if not acc:
        return admin_error("account not found", 404)
    return jsonify({"account": acc})


@app.route("/api/accounts/<account_id>", methods=["PUT"])
def api_update_account(account_id):
    guard = ensure_admin()
    if guard:
        return guard
    body = request.get_json(force=True, silent=True) or {}
    try:
        acc = ACCOUNT_MANAGER.update_account(account_id, **body)
    except Exception as e:
        return admin_error(str(e), 400)
    if not acc:
        return admin_error("account not found", 404)
    return jsonify({"ok": True, "account": acc})


@app.route("/api/accounts/batch-action", methods=["POST"])
def api_batch_action():
    guard = ensure_admin()
    if guard:
        return guard
    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or []
    action = (body.get("action") or "").strip()
    if not isinstance(ids, list) or not ids:
        return admin_error("ids 不能为空", 400)
    if action == "enable":
        items = ACCOUNT_MANAGER.batch_set_enabled(ids, True)
        return jsonify({"ok": True, "updated": len(items)})
    if action == "disable":
        items = ACCOUNT_MANAGER.batch_set_enabled(ids, False)
        return jsonify({"ok": True, "updated": len(items)})
    if action == "delete":
        deleted = ACCOUNT_MANAGER.batch_delete(ids)
        return jsonify({"ok": True, "deleted": deleted})
    if action == "keepalive":
        results = {}
        success = 0
        failed = 0
        for account_id in ids:
            try:
                results[account_id] = {"ok": True, "data": KEEPALIVE.refresh_account(account_id)}
                success += 1
            except Exception as e:
                results[account_id] = {"ok": False, "error": str(e)}
                failed += 1
        return jsonify({"ok": True, "success": success, "failed": failed, "results": results})
    if action == "test-session":
        results = {}
        success = 0
        failed = 0
        for account_id in ids:
            res = KEEPALIVE.check_account(account_id)
            results[account_id] = res
            if res.get("ok"):
                success += 1
            else:
                failed += 1
        return jsonify({"ok": True, "success": success, "failed": failed, "results": results})
    return admin_error("unsupported batch action", 400)


@app.route("/api/accounts/<account_id>/test-session", methods=["POST"])
def api_test_session(account_id):
    guard = ensure_admin()
    if guard:
        return guard
    result = KEEPALIVE.check_account(account_id)
    return jsonify(result)


@app.route("/api/accounts/<account_id>/keepalive", methods=["POST"])
def api_keepalive_account(account_id):
    guard = ensure_admin()
    if guard:
        return guard
    try:
        result = KEEPALIVE.refresh_account(account_id)
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return admin_error(str(e), 400)


@app.route("/api/accounts/<account_id>/relogin", methods=["POST"])
def api_relogin_account(account_id):
    guard = ensure_admin()
    if guard:
        return guard
    body = request.get_json(force=True, silent=True) or {}
    browser_mode = "visible" if str(body.get("browser_mode") or "").strip().lower() == "visible" else "headless"
    try:
        result = KEEPALIVE.relogin_account(account_id, browser_mode=browser_mode)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return admin_error(str(e), 400)


@app.route("/api/accounts/<account_id>/enable", methods=["POST"])
def api_enable_account(account_id):
    guard = ensure_admin()
    if guard:
        return guard
    acc = ACCOUNT_MANAGER.set_enabled(account_id, True)
    if not acc:
        return admin_error("account not found", 404)
    return jsonify({"ok": True, "account": acc})


@app.route("/api/accounts/<account_id>/disable", methods=["POST"])
def api_disable_account(account_id):
    guard = ensure_admin()
    if guard:
        return guard
    acc = ACCOUNT_MANAGER.set_enabled(account_id, False)
    if not acc:
        return admin_error("account not found", 404)
    return jsonify({"ok": True, "account": acc})


@app.route("/api/keepalive/run", methods=["POST"])
def api_keepalive_run():
    guard = ensure_admin()
    if guard:
        return guard
    return jsonify({"ok": True, "result": KEEPALIVE.run_once()})


@app.route("/api/auto-register/config", methods=["GET"])
def api_auto_register_config():
    guard = ensure_admin()
    if guard:
        return guard
    return jsonify(auto_register.check_config())


@app.route("/api/auto-register/start", methods=["POST"])
def api_auto_register_start():
    guard = ensure_admin()
    if guard:
        return guard
    body = request.get_json(force=True, silent=True) or {}
    count = _normalize_auto_register_count(body.get("count", 1))
    browser_mode = "visible" if str(body.get("browser_mode") or "").strip().lower() == "visible" else "headless"
    workers = body.get("workers", 1)
    task, err = auto_register.start(count=count, workers=workers, browser_mode=browser_mode)
    if err == "busy":
        return jsonify({"error": "auto register task already running", "task": task}), 409
    if err:
        return jsonify({"error": err, "config": auto_register.check_config()}), 400
    return jsonify({"ok": True, "task": task, "config": auto_register.check_config()})


@app.route("/api/auto-register/stop", methods=["POST"])
def api_auto_register_stop():
    guard = ensure_admin()
    if guard:
        return guard
    body = request.get_json(force=True, silent=True) or {}
    task_id = str(body.get("task_id") or "").strip() or None
    task = auto_register.stop(task_id)
    if not task:
        return admin_error("no matching auto register task", 404)
    return jsonify({"ok": True, "task": task})


@app.route("/api/auto-register/stream", methods=["GET"])
def api_auto_register_stream():
    guard = ensure_admin()
    if guard:
        return guard
    task_id = str(request.args.get("task_id") or "").strip()
    if not task_id:
        return admin_error("task_id is required", 400)

    def event_stream():
        last_state = None
        last_log_seq = 0
        last_result_seq = 0
        while True:
            task = auto_register.get_current_task(task_id, include_logs=True, include_results=True)
            if not task:
                yield f"event: error\ndata: {json.dumps({'message': 'task not found', 'task_id': task_id}, ensure_ascii=False)}\n\n"
                break
            state_payload = {k: v for k, v in task.items() if k not in {"logs", "results"}}
            state_key = json.dumps(state_payload, ensure_ascii=False, sort_keys=True)
            if state_key != last_state:
                yield f"event: state\ndata: {json.dumps(state_payload, ensure_ascii=False)}\n\n"
                last_state = state_key
            for item in task.get("logs") or []:
                if int(item.get("seq") or 0) <= last_log_seq:
                    continue
                yield f"event: log\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                last_log_seq = int(item.get("seq") or 0)
            for item in task.get("results") or []:
                if int(item.get("seq") or 0) <= last_result_seq:
                    continue
                yield f"event: result\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                last_result_seq = int(item.get("seq") or 0)
            if task.get("status") in {"completed", "failed", "stopped"}:
                yield f"event: done\ndata: {json.dumps(state_payload, ensure_ascii=False)}\n\n"
                break
            yield ": ping\n\n"
            time.sleep(1)

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/config", methods=["GET"])
def api_get_config():
    guard = ensure_admin()
    if guard:
        return guard
    return jsonify(
        {
            "host": HOST,
            "port": PORT,
            "api_key": API_KEY,
            "admin_token": ADMIN_TOKEN,
            "upstream_url": UPSTREAM_URL,
            "default_model": DEFAULT_MODEL,
            "keepalive_interval_seconds": CFG.get("keepalive_interval_seconds", 900),
            "keepalive_expiring_minutes": CFG.get("keepalive_expiring_minutes", 20),
            "keepalive_chat_fallback_enabled": bool(CFG.get("keepalive_chat_fallback_enabled", True)),
            "fail_threshold": CFG.get("fail_threshold", 3),
            "max_inflight": CFG.get("max_inflight", 2),
            "cooldown_seconds": CFG.get("cooldown_seconds", 120),
            "AUTO_REGISTER_ENABLED": bool(CFG.get("AUTO_REGISTER_ENABLED", False)),
            "AUTO_REGISTER_MIN_ACTIVE": int(CFG.get("AUTO_REGISTER_MIN_ACTIVE", 1) or 1),
            "AUTO_REGISTER_BATCH": int(CFG.get("AUTO_REGISTER_BATCH", 1) or 1),
            "AUTO_REGISTER_MAX_WORKERS": int(CFG.get("AUTO_REGISTER_MAX_WORKERS", 2) or 2),
            "AUTO_REGISTER_PASSWORD": CFG.get("AUTO_REGISTER_PASSWORD", ""),
            "AUTO_REGISTER_BROWSER_MODE_MANUAL": CFG.get("AUTO_REGISTER_BROWSER_MODE_MANUAL", "visible"),
            "AUTO_REGISTER_BROWSER_MODE_BACKGROUND": CFG.get("AUTO_REGISTER_BROWSER_MODE_BACKGROUND", "headless"),
            "AUTO_RELOGIN_ENABLED": bool(CFG.get("AUTO_RELOGIN_ENABLED", True)),
            "REGISTER_PROXY": CFG.get("REGISTER_PROXY", ""),
            "MAIL_USE_PROXY": bool(CFG.get("MAIL_USE_PROXY", False)),
            "MAIL_PROVIDER_DEFAULT": CFG.get("MAIL_PROVIDER_DEFAULT", "moemail"),
            "MOEMAIL_API_KEY": CFG.get("MOEMAIL_API_KEY", ""),
            "MOEMAIL_API_BASE": CFG.get("MOEMAIL_API_BASE", ""),
            "MOEMAIL_CHANNELS_JSON": CFG.get("MOEMAIL_CHANNELS_JSON", ""),
        }
    )


@app.route("/api/config", methods=["PUT"])
def api_set_config():
    guard = ensure_admin()
    if guard:
        return guard
    body = request.get_json(force=True, silent=True) or {}
    allowed_keys = {
        "api_key",
        "admin_token",
        "default_model",
        "keepalive_interval_seconds",
        "keepalive_expiring_minutes",
        "keepalive_chat_fallback_enabled",
        "fail_threshold",
        "max_inflight",
        "cooldown_seconds",
        "AUTO_REGISTER_ENABLED",
        "AUTO_REGISTER_MIN_ACTIVE",
        "AUTO_REGISTER_BATCH",
        "AUTO_REGISTER_MAX_WORKERS",
        "AUTO_REGISTER_PASSWORD",
        "AUTO_REGISTER_BROWSER_MODE_MANUAL",
        "AUTO_REGISTER_BROWSER_MODE_BACKGROUND",
        "AUTO_RELOGIN_ENABLED",
        "REGISTER_PROXY",
        "MAIL_USE_PROXY",
        "MAIL_PROVIDER_DEFAULT",
        "MOEMAIL_API_KEY",
        "MOEMAIL_API_BASE",
        "MOEMAIL_CHANNELS_JSON",
    }
    updates = {}
    for key, value in body.items():
        if key in allowed_keys:
            updates[key] = value
    upsert_config_values(updates)
    reload_runtime_config()
    refresh_upstream_model_catalog(force=True)
    app.secret_key = APP_SECRET
    return jsonify({"ok": True})


@app.route("/v1/models", methods=["GET"])
def list_models():
    if not (check_auth() or check_admin_auth()):
        return (
            jsonify(
                {
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "invalid api key"},
                }
            ),
            401,
        )
    refresh_upstream_model_catalog()
    now = int(time.time())
    ids = merge_unique(AVAILABLE_MODELS, CLAUDE_MODEL_MAP.keys())
    return jsonify(
        {
            "data": [
                {"id": m, "object": "model", "created": now, "owned_by": "umans"}
                for m in ids
            ],
            "object": "list",
        }
    )


@app.route("/v1/messages/count_tokens", methods=["POST"])
def count_tokens():
    if not check_auth():
        return (
            jsonify(
                {
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "invalid api key"},
                }
            ),
            401,
        )
    body = request.get_json(force=True, silent=True) or {}
    system = body.get("system")
    msgs = body.get("messages", [])
    tools = body.get("tools") or []
    extra_system = []
    tool_system = build_tools_prompt(tools, body.get("tool_choice"))
    if tool_system:
        extra_system.append(tool_system)
    if wants_anthropic_thinking(body):
        extra_system.append(thinking_prompt(True))
    prompt_text = anthropic_messages_to_text(system, msgs, extra_system="\n\n".join(extra_system) if extra_system else None)
    return jsonify({"input_tokens": max(1, len(prompt_text) // 4)})


@app.route("/v1/messages", methods=["POST"])
def messages():
    if not check_auth():
        return (
            jsonify(
                {
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "invalid api key"},
                }
            ),
            401,
        )

    body = request.get_json(force=True, silent=True) or {}
    req_model = body.get("model", "")
    upstream_model = resolve_model(req_model)
    stream = bool(body.get("stream", False))
    system = body.get("system")
    msgs = body.get("messages", [])
    tools = body.get("tools") or []
    has_tools = bool(tools)
    wants_thinking = wants_anthropic_thinking(body)

    extra_system_parts = []
    tool_system = build_tools_prompt(tools, body.get("tool_choice"))
    if tool_system:
        extra_system_parts.append(tool_system)
    thinking_system = thinking_prompt(wants_thinking)
    if thinking_system:
        extra_system_parts.append(thinking_system)
    prompt_text = anthropic_messages_to_text(system, msgs, extra_system="\n\n".join(extra_system_parts) if extra_system_parts else None)
    cache_started = time.time()
    cached_payload = get_cached_response("/v1/messages", upstream_model, prompt_text, tool_count=len(tools))
    if cached_payload:
        response_body = clone_cached_response(cached_payload)
        cached_metrics = extract_usage_metrics("anthropic", response_body)
        response_body["usage"] = attach_usage_details(
            "anthropic",
            response_body.get("usage"),
            cache_read_input_tokens=cached_metrics["input_tokens"],
        )
        log_metrics = build_usage_metrics(
            input_tokens=cached_metrics["input_tokens"],
            output_tokens=cached_metrics["output_tokens"],
            reasoning_text=extract_reasoning_text("anthropic", response_body),
            cache_hit=True,
        )
        insert_request_log(
            path="/v1/messages",
            api_format="anthropic",
            stream=stream,
            client_model=req_model or "",
            upstream_model=upstream_model,
            account_id="",
            account_name="app-cache",
            ok=True,
            status_code=200,
            duration_ms=int((time.time() - cache_started) * 1000),
            finish_reason=response_body.get("stop_reason") or "end_turn",
            response_id=response_body.get("id") or "",
            reasoning_chars=len(extract_reasoning_text("anthropic", response_body)),
            input_tokens=log_metrics["input_tokens"],
            output_tokens=log_metrics["output_tokens"],
            total_tokens=log_metrics["total_tokens"],
            reasoning_tokens=log_metrics["reasoning_tokens"],
            cache_hit=True,
            cache_read_input_tokens=log_metrics["cache_read_input_tokens"],
            cache_creation_input_tokens=0,
            detail=build_log_detail(
                request_body=body,
                response_body=response_body,
                reasoning_text=extract_reasoning_text("anthropic", response_body),
                prompt_text=prompt_text,
                note="命中本地 exact response cache",
                phases=[
                    {"phase": "cache", "offset_ms": int((time.time() - cache_started) * 1000), "event_name": "cache_hit", "payload": {"path": "/v1/messages"}},
                ],
            ),
        )
        if stream:
            return Response(
                stream_with_context(anthropic_stream_from_cached(response_body)),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
            )
        return jsonify(response_body)
    payload, chat_id = build_upstream_payload(upstream_model, prompt_text)

    log.info(
        "请求: client=%s -> upstream=%s, stream=%s, tools=%d, prompt_len=%d",
        req_model, upstream_model, stream, len(tools), len(prompt_text),
    )

    result = _attempt_upstream_request(
        payload,
        upstream_model,
        path="/v1/messages",
        client_model=req_model or "",
        request_body=body,
        prompt_text=prompt_text,
        client_stream=stream,
        tool_count=len(tools),
    )
    if len(result) == 5:
        _upstream, _account, _started, status, message = result
        log.error("上游连接失败: %s", message)
        return (
            jsonify({"type": "error", "error": {"type": "api_error", "message": message}}),
            status,
        )
    upstream, account, started = result

    if stream:
        def generate():
            ok = True
            capture = {}
            try:
                yield from anthropic_stream(upstream, req_model or upstream_model, has_tools, wants_thinking=wants_thinking, capture=capture)
            except Exception as e:
                ok = False
                ACCOUNT_MANAGER.mark_fail(account["id"], str(e))
                raise
            finally:
                if ok:
                    ACCOUNT_MANAGER.mark_ok(account["id"])
                    visible_usage_in = int(capture.get("usage_in") or max(1, len(prompt_text) // 4))
                    visible_usage_out = int(capture.get("usage_out") or 0)
                    reasoning_text = capture.get("reasoning_text") or ""
                    cache_written = False
                    if not has_tools and (capture.get("content_blocks") or []):
                        cached_body = {
                            "id": "msg_" + uuid.uuid4().hex[:24],
                            "type": "message",
                            "role": "assistant",
                            "model": req_model or upstream_model,
                            "content": capture.get("content_blocks") or [{"type": "text", "text": ""}],
                            "stop_reason": capture.get("stop_reason") or "end_turn",
                            "stop_sequence": None,
                            "usage": {
                                "input_tokens": visible_usage_in,
                                "output_tokens": visible_usage_out,
                            },
                        }
                        cache_written = store_cached_response(
                            path="/v1/messages",
                            api_format="anthropic",
                            upstream_model=upstream_model,
                            prompt_text=prompt_text,
                            tool_count=len(tools),
                            response_body=cached_body,
                        )
                    usage_metrics = build_usage_metrics(
                        input_tokens=visible_usage_in,
                        output_tokens=visible_usage_out,
                        reasoning_text=reasoning_text,
                        cache_created=cache_written,
                    )
                    insert_request_log(
                        path="/v1/messages",
                        api_format="anthropic",
                        stream=True,
                        client_model=req_model or "",
                        upstream_model=upstream_model,
                        account_id=account["id"],
                        account_name=account.get("name") or "",
                        ok=True,
                        status_code=200,
                        duration_ms=int((time.time() - started) * 1000),
                        finish_reason=capture.get("stop_reason") or "stream_completed",
                        tool_count=len(tools),
                        input_tokens=usage_metrics["input_tokens"],
                        output_tokens=usage_metrics["output_tokens"],
                        total_tokens=usage_metrics["total_tokens"],
                        reasoning_tokens=usage_metrics["reasoning_tokens"],
                        cache_hit=False,
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=usage_metrics["cache_creation_input_tokens"],
                        detail=build_log_detail(
                            request_body=body,
                            prompt_text=prompt_text,
                            note="流式完成。详细 chunk 仍在前端调试台可见。",
                            phases=[
                                {"phase": "reserve", "offset_ms": 0, "event_name": "account_reserved", "payload": {"account_id": account["id"]}},
                                {"phase": "dispatch", "offset_ms": int((time.time() - started) * 1000), "event_name": "stream_completed", "payload": {"tool_mode": has_tools, "thinking_enabled": wants_thinking, "cache_written": cache_written}},
                            ],
                        ),
                    )
                ACCOUNT_MANAGER.release_reservation(account["id"])

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # 非流式
    full, usage_in, usage_out, native_tools = collect_full_text(upstream)
    stop_reason, content_blocks, _reasoning = build_tool_use_blocks(full, native_tools, wants_thinking=wants_thinking)
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    public_usage = attach_usage_details("anthropic", {"input_tokens": usage_in, "output_tokens": usage_out or max(1, len(full) // 4)})
    response_body = {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": req_model or upstream_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": public_usage,
    }
    cache_written = False
    if not has_tools:
        cache_written = store_cached_response(
            path="/v1/messages",
            api_format="anthropic",
            upstream_model=upstream_model,
            prompt_text=prompt_text,
            tool_count=len(tools),
            response_body=response_body,
        )
    usage_metrics = build_usage_metrics(
        input_tokens=usage_in,
        output_tokens=usage_out or max(1, len(full) // 4),
        reasoning_text="\n\n".join(item.get("thinking", "") for item in content_blocks if item.get("type") == "thinking"),
        cache_created=cache_written,
    )
    ACCOUNT_MANAGER.mark_ok(account["id"])
    insert_request_log(
        path="/v1/messages",
        api_format="anthropic",
        stream=False,
        client_model=req_model or "",
        upstream_model=upstream_model,
        account_id=account["id"],
        account_name=account.get("name") or "",
        ok=True,
        status_code=200,
        duration_ms=int((time.time() - started) * 1000),
        finish_reason=stop_reason,
        response_id=msg_id,
        tool_count=len([x for x in content_blocks if x.get("type") == "tool_use"]),
        reasoning_chars=len("\n\n".join(item.get("thinking", "") for item in content_blocks if item.get("type") == "thinking")),
        input_tokens=usage_metrics["input_tokens"],
        output_tokens=usage_metrics["output_tokens"],
        total_tokens=usage_metrics["total_tokens"],
        reasoning_tokens=usage_metrics["reasoning_tokens"],
        cache_hit=False,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=usage_metrics["cache_creation_input_tokens"],
        detail=build_log_detail(
            request_body=body,
            response_body={"content": content_blocks},
            tool_calls=[{"name": item.get("name"), "input": item.get("input", {})} for item in content_blocks if item.get("type") == "tool_use"],
            reasoning_text="\n\n".join(item.get("thinking", "") for item in content_blocks if item.get("type") == "thinking"),
            prompt_text=prompt_text,
            phases=[
                {"phase": "reserve", "offset_ms": 0, "event_name": "account_reserved", "payload": {"account_id": account["id"]}},
                {"phase": "collect", "offset_ms": int((time.time() - started) * 1000), "event_name": "full_response_collected", "payload": {"stop_reason": stop_reason, "cache_written": cache_written}},
            ],
        ),
    )
    ACCOUNT_MANAGER.release_reservation(account["id"])
    return jsonify(response_body)


# ---------- OpenAI 兼容 ----------
@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    if not check_auth():
        return (
            jsonify(
                {
                    "error": {
                        "message": "invalid api key",
                        "type": "authentication_error",
                    }
                }
            ),
            401,
        )

    body = request.get_json(force=True, silent=True) or {}
    req_model = body.get("model", "")
    upstream_model = resolve_model(req_model)
    stream = bool(body.get("stream", False))
    msgs = body.get("messages", [])
    tools = normalize_openai_tools(body.get("tools") or [])
    has_tools = bool(tools)
    wants_reasoning = wants_openai_reasoning(body)
    system_text, anth_msgs = openai_messages_to_anthropic([], msgs)
    extra_system_parts = []
    tool_system = build_tools_prompt(tools, normalize_openai_tool_choice(body.get("tool_choice")))
    if tool_system:
        extra_system_parts.append(tool_system)
    if wants_reasoning:
        extra_system_parts.append(thinking_prompt(True))
    prompt_text = anthropic_messages_to_text(system_text, anth_msgs, extra_system="\n\n".join(extra_system_parts) if extra_system_parts else None)
    cache_started = time.time()
    cached_payload = get_cached_response("/v1/chat/completions", upstream_model, prompt_text, tool_count=len(tools))
    if cached_payload:
        response_body = clone_cached_response(cached_payload)
        cached_metrics = extract_usage_metrics("openai", response_body)
        response_body["usage"] = attach_usage_details(
            "openai",
            response_body.get("usage"),
            reasoning_tokens=cached_metrics["reasoning_tokens"],
            cache_read_input_tokens=cached_metrics["input_tokens"],
        )
        log_metrics = build_usage_metrics(
            input_tokens=cached_metrics["input_tokens"],
            output_tokens=cached_metrics["output_tokens"],
            reasoning_text=extract_reasoning_text("openai", response_body),
            cache_hit=True,
        )
        insert_request_log(
            path="/v1/chat/completions",
            api_format="openai",
            stream=stream,
            client_model=req_model or "",
            upstream_model=upstream_model,
            account_id="",
            account_name="app-cache",
            ok=True,
            status_code=200,
            duration_ms=int((time.time() - cache_started) * 1000),
            finish_reason=((response_body.get("choices") or [{}])[0].get("finish_reason")) or "stop",
            response_id=response_body.get("id") or "",
            input_tokens=log_metrics["input_tokens"],
            output_tokens=log_metrics["output_tokens"],
            total_tokens=log_metrics["total_tokens"],
            reasoning_tokens=log_metrics["reasoning_tokens"],
            cache_hit=True,
            cache_read_input_tokens=log_metrics["cache_read_input_tokens"],
            detail=build_log_detail(
                request_body=body,
                response_body=response_body,
                reasoning_text=extract_reasoning_text("openai", response_body),
                prompt_text=prompt_text,
                note="命中本地 exact response cache",
                phases=[{"phase": "cache", "offset_ms": int((time.time() - cache_started) * 1000), "event_name": "cache_hit", "payload": {"path": "/v1/chat/completions"}}],
            ),
        )
        if stream:
            return Response(
                stream_with_context(openai_stream_from_cached(response_body)),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return jsonify(response_body)

    payload, chat_id = build_upstream_payload(upstream_model, prompt_text)
    result = _attempt_upstream_request(
        payload,
        upstream_model,
        path="/v1/chat/completions",
        client_model=req_model or "",
        request_body=body,
        prompt_text=prompt_text,
        client_stream=stream,
        tool_count=len(tools),
    )
    if len(result) == 5:
        _upstream, _account, _started, status, message = result
        return jsonify({"error": {"message": message, "type": "upstream_error"}}), status
    upstream, account, started = result

    cmpl_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    if stream:
        def gen():
            ok = True
            text_parts = []
            reasoning_text = ""
            tool_calls = []
            usage_in = 0
            usage_out = 0
            try:
                if has_tools or wants_reasoning:
                    full, usage_in, usage_out, native_tools = collect_full_text(upstream)
                    visible_text, reasoning_text, tool_calls = build_openai_tool_calls(full, native_tools)
                    yield write_openai_chunk(
                        {
                            "id": cmpl_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": req_model or upstream_model,
                            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                        }
                    )
                    if wants_reasoning and reasoning_text:
                        for chunk in chunk_string(reasoning_text):
                            yield write_openai_chunk(
                                {
                                    "id": cmpl_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": req_model or upstream_model,
                                    "choices": [{"index": 0, "delta": {"reasoning_content": chunk}, "finish_reason": None}],
                                }
                            )
                    if visible_text:
                        first_text = True
                        for chunk in chunk_string(visible_text):
                            yield write_openai_chunk(
                                {
                                    "id": cmpl_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": req_model or upstream_model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": chunk} if not first_text else {"content": chunk},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                            )
                            first_text = False
                    for tool_index, tool_call in enumerate(tool_calls):
                        yield write_openai_chunk(
                            {
                                "id": cmpl_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": req_model or upstream_model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "tool_calls": [
                                                {
                                                    "index": tool_index,
                                                    "id": tool_call["id"],
                                                    "type": "function",
                                                    "function": {"name": tool_call["function"]["name"], "arguments": ""},
                                                }
                                            ]
                                        },
                                        "finish_reason": None,
                                    }
                                ],
                            }
                        )
                        for arg_chunk in chunk_string(tool_call["function"]["arguments"], 120):
                            yield write_openai_chunk(
                                {
                                    "id": cmpl_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": req_model or upstream_model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"tool_calls": [{"index": tool_index, "function": {"arguments": arg_chunk}}]},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                            )
                    finish_reason = "tool_calls" if tool_calls else "stop"
                    done = {
                        "id": cmpl_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": req_model or upstream_model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                        "usage": {
                            "prompt_tokens": usage_in or max(1, len(prompt_text) // 4),
                            "completion_tokens": usage_out or max(1, len(visible_text or full) // 4),
                            "total_tokens": (usage_in or max(1, len(prompt_text) // 4)) + (usage_out or max(1, len(visible_text or full) // 4)),
                        },
                    }
                else:
                    first = True
                    for ev in iter_upstream_events(upstream):
                        if ev.get("__done__"):
                            break
                        if ev.get("type") != "text-delta":
                            continue
                        delta = ev.get("delta", "")
                        if not delta:
                            continue
                        text_parts.append(delta)
                        chunk = {
                            "id": cmpl_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": req_model or upstream_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"role": "assistant", "content": delta} if first else {"content": delta},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        first = False
                        yield write_openai_chunk(chunk)
                    done = {
                        "id": cmpl_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": req_model or upstream_model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    usage_in = max(1, len(prompt_text) // 4)
                    usage_out = max(1, len("".join(text_parts)) // 4)
                yield write_openai_chunk(done)
                yield "data: [DONE]\n\n"
            except Exception as e:
                ok = False
                ACCOUNT_MANAGER.mark_fail(account["id"], str(e))
                raise
            finally:
                if ok:
                    ACCOUNT_MANAGER.mark_ok(account["id"])
                    visible_text = visible_text if 'visible_text' in locals() else "".join(text_parts)
                    cache_written = False
                    if not has_tools and not tool_calls:
                        cache_response_body = {
                            "id": cmpl_id,
                            "object": "chat.completion",
                            "created": created,
                            "model": req_model or upstream_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": visible_text or None,
                                        **({"reasoning_content": reasoning_text} if wants_reasoning and reasoning_text else {}),
                                    },
                                    "finish_reason": done["choices"][0]["finish_reason"] if 'done' in locals() else "stop",
                                }
                            ],
                            "usage": attach_usage_details(
                                "openai",
                                {
                                    "prompt_tokens": usage_in or max(1, len(prompt_text) // 4),
                                    "completion_tokens": usage_out or max(1, len(visible_text or "") // 4),
                                    "total_tokens": (usage_in or max(1, len(prompt_text) // 4)) + (usage_out or max(1, len(visible_text or "") // 4)),
                                },
                                reasoning_tokens=estimate_text_tokens(reasoning_text),
                            ),
                        }
                        cache_written = store_cached_response(
                            path="/v1/chat/completions",
                            api_format="openai",
                            upstream_model=upstream_model,
                            prompt_text=prompt_text,
                            tool_count=len(tools),
                            response_body=cache_response_body,
                        )
                    usage_metrics = build_usage_metrics(
                        input_tokens=usage_in or max(1, len(prompt_text) // 4),
                        output_tokens=usage_out or max(1, len(visible_text or "") // 4),
                        reasoning_text=reasoning_text,
                        cache_created=cache_written,
                    )
                    insert_request_log(
                        path="/v1/chat/completions",
                        api_format="openai",
                        stream=True,
                        client_model=req_model or "",
                        upstream_model=upstream_model,
                        account_id=account["id"],
                        account_name=account.get("name") or "",
                        ok=True,
                        status_code=200,
                        duration_ms=int((time.time() - started) * 1000),
                        finish_reason=done["choices"][0]["finish_reason"] if 'done' in locals() else "stream_completed",
                        response_id=cmpl_id,
                        tool_count=len(tool_calls) if 'tool_calls' in locals() else len(tools),
                        reasoning_chars=len(reasoning_text or "") if 'reasoning_text' in locals() else 0,
                        input_tokens=usage_metrics["input_tokens"],
                        output_tokens=usage_metrics["output_tokens"],
                        total_tokens=usage_metrics["total_tokens"],
                        reasoning_tokens=usage_metrics["reasoning_tokens"],
                        cache_hit=False,
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=usage_metrics["cache_creation_input_tokens"],
                        detail=build_log_detail(
                            request_body=body,
                            response_body={"finish_reason": done["choices"][0]["finish_reason"] if 'done' in locals() else "stream_completed"},
                            tool_calls=tool_calls if 'tool_calls' in locals() else [],
                            reasoning_text=reasoning_text if 'reasoning_text' in locals() else "",
                            prompt_text=prompt_text,
                            phases=[
                                {"phase": "reserve", "offset_ms": 0, "event_name": "account_reserved", "payload": {"account_id": account["id"]}},
                                {"phase": "dispatch", "offset_ms": int((time.time() - started) * 1000), "event_name": "stream_completed", "payload": {"stream": True, "cache_written": cache_written}},
                            ],
                        ),
                    )
                ACCOUNT_MANAGER.release_reservation(account["id"])

        return Response(
            stream_with_context(gen()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    text, usage_in, usage_out, _native = collect_full_text(upstream)
    visible_text, reasoning_text, tool_calls = build_openai_tool_calls(text, _native)
    response_body = {
        "id": cmpl_id,
        "object": "chat.completion",
        "created": created,
        "model": req_model or upstream_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": visible_text or None,
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                    **({"reasoning_content": reasoning_text} if wants_reasoning and reasoning_text else {}),
                },
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": attach_usage_details(
            "openai",
            {
                "prompt_tokens": usage_in or max(1, len(prompt_text) // 4),
                "completion_tokens": usage_out or max(1, len(visible_text or text) // 4),
                "total_tokens": (usage_in or max(1, len(prompt_text) // 4)) + (usage_out or max(1, len(visible_text or text) // 4)),
            },
            reasoning_tokens=estimate_text_tokens(reasoning_text),
        ),
    }
    cache_written = False
    if not tool_calls:
        cache_written = store_cached_response(
            path="/v1/chat/completions",
            api_format="openai",
            upstream_model=upstream_model,
            prompt_text=prompt_text,
            tool_count=len(tools),
            response_body=response_body,
        )
    usage_metrics = build_usage_metrics(
        input_tokens=usage_in or max(1, len(prompt_text) // 4),
        output_tokens=usage_out or max(1, len(visible_text or text) // 4),
        reasoning_text=reasoning_text,
        cache_created=cache_written,
    )
    ACCOUNT_MANAGER.mark_ok(account["id"])
    insert_request_log(
        path="/v1/chat/completions",
        api_format="openai",
        stream=False,
        client_model=req_model or "",
        upstream_model=upstream_model,
        account_id=account["id"],
        account_name=account.get("name") or "",
        ok=True,
        status_code=200,
        duration_ms=int((time.time() - started) * 1000),
        finish_reason="tool_calls" if tool_calls else "stop",
        response_id=cmpl_id,
        tool_count=len(tool_calls),
        reasoning_chars=len(reasoning_text or ""),
        input_tokens=usage_metrics["input_tokens"],
        output_tokens=usage_metrics["output_tokens"],
        total_tokens=usage_metrics["total_tokens"],
        reasoning_tokens=usage_metrics["reasoning_tokens"],
        cache_hit=False,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=usage_metrics["cache_creation_input_tokens"],
        detail=build_log_detail(
            request_body=body,
            response_body={"content": visible_text, "tool_calls": tool_calls},
            tool_calls=tool_calls,
            reasoning_text=reasoning_text,
            prompt_text=prompt_text,
            phases=[
                {"phase": "reserve", "offset_ms": 0, "event_name": "account_reserved", "payload": {"account_id": account["id"]}},
                {"phase": "collect", "offset_ms": int((time.time() - started) * 1000), "event_name": "full_response_collected", "payload": {"finish_reason": "tool_calls" if tool_calls else "stop", "cache_written": cache_written}},
            ],
        ),
    )
    ACCOUNT_MANAGER.release_reservation(account["id"])
    return jsonify(response_body)


@app.route("/v1/responses", methods=["POST"])
def responses_api():
    if not check_auth():
        return jsonify({"error": {"message": "invalid api key", "type": "authentication_error"}}), 401

    body = request.get_json(force=True, silent=True) or {}
    openai_req = responses_to_openai_request(body)
    req_model = openai_req.get("model", "")
    upstream_model = resolve_model(req_model)
    stream = bool(body.get("stream", False))
    wants_reasoning = wants_openai_reasoning(openai_req)
    tools = normalize_openai_tools(openai_req.get("tools") or [])
    system_text, anth_msgs = openai_messages_to_anthropic([openai_req.get("instructions", "")], openai_req.get("messages") or [])
    extra_system_parts = []
    tool_system = build_tools_prompt(tools, normalize_openai_tool_choice(body.get("tool_choice")))
    if tool_system:
        extra_system_parts.append(tool_system)
    if wants_reasoning:
        extra_system_parts.append(thinking_prompt(True))
    prompt_text = anthropic_messages_to_text(system_text, anth_msgs, extra_system="\n\n".join(extra_system_parts) if extra_system_parts else None)
    cache_started = time.time()
    cached_payload = get_cached_response("/v1/responses", upstream_model, prompt_text, tool_count=len(tools))
    if cached_payload:
        response_body = clone_cached_response(cached_payload)
        cached_metrics = extract_usage_metrics("responses", response_body)
        response_body["usage"] = attach_usage_details(
            "responses",
            response_body.get("usage"),
            reasoning_tokens=cached_metrics["reasoning_tokens"],
            cache_read_input_tokens=cached_metrics["input_tokens"],
        )
        log_metrics = build_usage_metrics(
            input_tokens=cached_metrics["input_tokens"],
            output_tokens=cached_metrics["output_tokens"],
            reasoning_text=extract_reasoning_text("responses", response_body),
            cache_hit=True,
        )
        insert_request_log(
            path="/v1/responses",
            api_format="responses",
            stream=stream,
            client_model=req_model or "",
            upstream_model=upstream_model,
            account_id="",
            account_name="app-cache",
            ok=True,
            status_code=200,
            duration_ms=int((time.time() - cache_started) * 1000),
            finish_reason="stop",
            response_id=response_body.get("id") or "",
            input_tokens=log_metrics["input_tokens"],
            output_tokens=log_metrics["output_tokens"],
            total_tokens=log_metrics["total_tokens"],
            reasoning_tokens=log_metrics["reasoning_tokens"],
            cache_hit=True,
            cache_read_input_tokens=log_metrics["cache_read_input_tokens"],
            detail=build_log_detail(
                request_body=body,
                response_body=response_body,
                reasoning_text=extract_reasoning_text("responses", response_body),
                prompt_text=prompt_text,
                previous_response_id=body.get("previous_response_id") or "",
                note="命中本地 exact response cache",
                phases=[{"phase": "cache", "offset_ms": int((time.time() - cache_started) * 1000), "event_name": "cache_hit", "payload": {"path": "/v1/responses"}}],
            ),
        )
        if stream:
            return Response(
                stream_with_context(responses_stream_from_cached(response_body)),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
            )
        return jsonify(response_body)

    payload, chat_id = build_upstream_payload(upstream_model, prompt_text)
    result = _attempt_upstream_request(
        payload,
        upstream_model,
        path="/v1/responses",
        client_model=req_model or "",
        request_body=body,
        prompt_text=prompt_text,
        client_stream=stream,
        tool_count=len(tools),
        previous_response_id=body.get("previous_response_id") or "",
    )
    if len(result) == 5:
        _upstream, _account, _started, status, message = result
        return jsonify({"error": {"message": message, "type": "upstream_error"}}), status
    upstream, account, started = result

    response_id = "resp_" + uuid.uuid4().hex[:24]
    output_index = 0

    if stream:
        def gen():
            ok = True
            try:
                full, usage_in, usage_out, native_tools = collect_full_text(upstream)
                visible_text, reasoning_text, tool_calls = build_openai_tool_calls(full, native_tools)
                usage = build_responses_usage(
                    usage_in or max(1, len(prompt_text) // 4),
                    usage_out or max(1, len(visible_text or full) // 4),
                )
                output_items = []
                yield responses_sse("response.created", {"response": build_response_object(response_id, req_model or upstream_model, "in_progress", [])})
                yield responses_sse("response.in_progress", {"response": build_response_object(response_id, req_model or upstream_model, "in_progress", [])})

                current_index = 0
                if wants_reasoning and reasoning_text:
                    reasoning_item = {
                        "id": "rs_" + uuid.uuid4().hex[:24],
                        "type": "reasoning",
                        "summary": [],
                        "status": "in_progress",
                    }
                    yield responses_sse("response.output_item.added", {"output_index": current_index, "item": reasoning_item})
                    summary_text = ""
                    for chunk in chunk_string(reasoning_text, 160):
                        summary_text += chunk
                        yield responses_sse("response.reasoning_summary_text.delta", {"output_index": current_index, "summary_index": 0, "delta": chunk})
                    reasoning_item = {
                        "id": reasoning_item["id"],
                        "type": "reasoning",
                        "status": "completed",
                        "summary": [{"type": "summary_text", "text": summary_text}],
                    }
                    yield responses_sse("response.reasoning_summary_text.done", {"output_index": current_index, "summary_index": 0, "text": summary_text})
                    yield responses_sse("response.output_item.done", {"output_index": current_index, "item": reasoning_item})
                    output_items.append(reasoning_item)
                    current_index += 1

                for tool_call in tool_calls:
                    fc_item = {
                        "id": "fc_" + uuid.uuid4().hex[:24],
                        "type": "function_call",
                        "name": tool_call["function"]["name"],
                        "call_id": tool_call["id"],
                        "arguments": "",
                        "status": "in_progress",
                    }
                    yield responses_sse("response.output_item.added", {"output_index": current_index, "item": fc_item})
                    arg_text = tool_call["function"]["arguments"]
                    for chunk in chunk_string(arg_text, 120):
                        fc_item["arguments"] += chunk
                        yield responses_sse("response.function_call_arguments.delta", {"output_index": current_index, "delta": chunk})
                    yield responses_sse("response.function_call_arguments.done", {"output_index": current_index, "arguments": fc_item["arguments"]})
                    done_item = {**fc_item, "status": "completed"}
                    yield responses_sse("response.output_item.done", {"output_index": current_index, "item": done_item})
                    output_items.append(done_item)
                    current_index += 1

                if visible_text:
                    msg_item = {
                        "id": "msg_" + uuid.uuid4().hex[:24],
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    }
                    yield responses_sse("response.output_item.added", {"output_index": current_index, "item": msg_item})
                    yield responses_sse("response.content_part.added", {"output_index": current_index, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
                    text_acc = ""
                    for chunk in chunk_string(visible_text, 160):
                        text_acc += chunk
                        yield responses_sse("response.output_text.delta", {"output_index": current_index, "content_index": 0, "delta": chunk})
                    yield responses_sse("response.output_text.done", {"output_index": current_index, "content_index": 0, "text": text_acc})
                    part = {"type": "output_text", "text": text_acc, "annotations": []}
                    yield responses_sse("response.content_part.done", {"output_index": current_index, "content_index": 0, "part": part})
                    done_item = {"id": msg_item["id"], "type": "message", "role": "assistant", "status": "completed", "content": [part]}
                    yield responses_sse("response.output_item.done", {"output_index": current_index, "item": done_item})
                    output_items.append(done_item)

                yield responses_sse("response.completed", {"response": build_response_object(response_id, req_model or upstream_model, "completed", output_items, usage)})
                cache_written = False
                if not tool_calls:
                    cache_response_body = build_response_object(response_id, req_model or upstream_model, "completed", output_items, attach_usage_details("responses", usage, reasoning_tokens=estimate_text_tokens(reasoning_text)))
                    cache_written = store_cached_response(
                        path="/v1/responses",
                        api_format="responses",
                        upstream_model=upstream_model,
                        prompt_text=prompt_text,
                        tool_count=len(tools),
                        response_body=cache_response_body,
                    )
                update_response_state(
                    response_id,
                    (openai_req.get("messages") or [])
                    + [{"role": "assistant", "content": visible_text or None, **({"tool_calls": tool_calls} if tool_calls else {})}],
                )
            except Exception as e:
                ok = False
                ACCOUNT_MANAGER.mark_fail(account["id"], str(e))
                error_text = f"[Error: {e}]"
                error_item = {
                    "id": "msg_" + uuid.uuid4().hex[:24],
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": error_text, "annotations": []}],
                }
                yield responses_sse("response.created", {"response": build_response_object(response_id, req_model or upstream_model, "in_progress", [])})
                yield responses_sse("response.output_item.added", {"output_index": 0, "item": {"id": error_item["id"], "type": "message", "role": "assistant", "status": "in_progress", "content": []}})
                yield responses_sse("response.content_part.added", {"output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
                yield responses_sse("response.output_text.delta", {"output_index": 0, "content_index": 0, "delta": error_text})
                yield responses_sse("response.output_text.done", {"output_index": 0, "content_index": 0, "text": error_text})
                yield responses_sse("response.content_part.done", {"output_index": 0, "content_index": 0, "part": error_item["content"][0]})
                yield responses_sse("response.output_item.done", {"output_index": 0, "item": error_item})
                yield responses_sse("response.completed", {"response": build_response_object(response_id, req_model or upstream_model, "completed", [error_item], build_responses_usage(0, max(1, len(error_text) // 4)))})
            finally:
                if ok:
                    ACCOUNT_MANAGER.mark_ok(account["id"])
                    usage_metrics = build_usage_metrics(
                        input_tokens=usage.get("input_tokens") or max(1, len(prompt_text) // 4),
                        output_tokens=usage.get("output_tokens") or max(1, len(visible_text or full) // 4),
                        reasoning_text=reasoning_text,
                        cache_created=cache_written if 'cache_written' in locals() else False,
                    )
                    insert_request_log(
                        path="/v1/responses",
                        api_format="responses",
                        stream=True,
                        client_model=req_model or "",
                        upstream_model=upstream_model,
                        account_id=account["id"],
                        account_name=account.get("name") or "",
                        ok=True,
                        status_code=200,
                        duration_ms=int((time.time() - started) * 1000),
                        finish_reason="tool_calls" if tool_calls else "stop",
                        response_id=response_id,
                        tool_count=len(tool_calls),
                        reasoning_chars=len(reasoning_text or ""),
                        input_tokens=usage_metrics["input_tokens"],
                        output_tokens=usage_metrics["output_tokens"],
                        total_tokens=usage_metrics["total_tokens"],
                        reasoning_tokens=usage_metrics["reasoning_tokens"],
                        cache_hit=False,
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=usage_metrics["cache_creation_input_tokens"],
                        detail=build_log_detail(
                            request_body=body,
                            response_body={"output": output_items},
                            tool_calls=tool_calls,
                            reasoning_text=reasoning_text,
                            prompt_text=prompt_text,
                            previous_response_id=body.get("previous_response_id") or "",
                            phases=[
                                {"phase": "reserve", "offset_ms": 0, "event_name": "account_reserved", "payload": {"account_id": account["id"]}},
                                {"phase": "response.created", "offset_ms": 0, "event_name": "response_created", "payload": {"response_id": response_id}},
                                {"phase": "response.completed", "offset_ms": int((time.time() - started) * 1000), "event_name": "response_completed", "payload": {"output_count": len(output_items), "cache_written": cache_written if 'cache_written' in locals() else False}},
                            ],
                        ),
                    )
                ACCOUNT_MANAGER.release_reservation(account["id"])

        return Response(
            stream_with_context(gen()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    full, usage_in, usage_out, native_tools = collect_full_text(upstream)
    visible_text, reasoning_text, tool_calls = build_openai_tool_calls(full, native_tools)
    usage = build_responses_usage(
        usage_in or max(1, len(prompt_text) // 4),
        usage_out or max(1, len(visible_text or full) // 4),
    )
    output_items = []
    if wants_reasoning and reasoning_text:
        output_items.append(
            {
                "id": "rs_" + uuid.uuid4().hex[:24],
                "type": "reasoning",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": reasoning_text}],
            }
        )
    for tool_call in tool_calls:
        output_items.append(
            {
                "id": "fc_" + uuid.uuid4().hex[:24],
                "type": "function_call",
                "name": tool_call["function"]["name"],
                "call_id": tool_call["id"],
                "arguments": tool_call["function"]["arguments"],
                "status": "completed",
            }
        )
    if visible_text:
        output_items.append(
            {
                "id": "msg_" + uuid.uuid4().hex[:24],
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": visible_text, "annotations": []}],
            }
        )

    response_body = build_response_object(
        response_id,
        req_model or upstream_model,
        "completed",
        output_items,
        attach_usage_details("responses", usage, reasoning_tokens=estimate_text_tokens(reasoning_text)),
    )
    cache_written = False
    if not tool_calls:
        cache_written = store_cached_response(
            path="/v1/responses",
            api_format="responses",
            upstream_model=upstream_model,
            prompt_text=prompt_text,
            tool_count=len(tools),
            response_body=response_body,
        )
    usage_metrics = build_usage_metrics(
        input_tokens=usage.get("input_tokens") or max(1, len(prompt_text) // 4),
        output_tokens=usage.get("output_tokens") or max(1, len(visible_text or full) // 4),
        reasoning_text=reasoning_text,
        cache_created=cache_written,
    )
    ACCOUNT_MANAGER.mark_ok(account["id"])
    insert_request_log(
        path="/v1/responses",
        api_format="responses",
        stream=False,
        client_model=req_model or "",
        upstream_model=upstream_model,
        account_id=account["id"],
        account_name=account.get("name") or "",
        ok=True,
        status_code=200,
        duration_ms=int((time.time() - started) * 1000),
        finish_reason="tool_calls" if tool_calls else "stop",
        response_id=response_id,
        tool_count=len(tool_calls),
        reasoning_chars=len(reasoning_text or ""),
        input_tokens=usage_metrics["input_tokens"],
        output_tokens=usage_metrics["output_tokens"],
        total_tokens=usage_metrics["total_tokens"],
        reasoning_tokens=usage_metrics["reasoning_tokens"],
        cache_hit=False,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=usage_metrics["cache_creation_input_tokens"],
        detail=build_log_detail(
            request_body=body,
            response_body={"output": output_items},
            tool_calls=tool_calls,
            reasoning_text=reasoning_text,
            prompt_text=prompt_text,
            previous_response_id=body.get("previous_response_id") or "",
            phases=[
                {"phase": "reserve", "offset_ms": 0, "event_name": "account_reserved", "payload": {"account_id": account["id"]}},
                {"phase": "collect", "offset_ms": int((time.time() - started) * 1000), "event_name": "response_completed", "payload": {"output_count": len(output_items), "cache_written": cache_written}},
            ],
        ),
    )
    ACCOUNT_MANAGER.release_reservation(account["id"])
    update_response_state(
        response_id,
        (openai_req.get("messages") or [])
        + [{"role": "assistant", "content": visible_text or None, **({"tool_calls": tool_calls} if tool_calls else {})}],
    )
    return jsonify(response_body)


if acquire_background_owner():
    threading.Thread(
        target=lambda: refresh_upstream_model_catalog(force=True),
        daemon=True,
        name="umans-model-catalog-refresh",
    ).start()


if __name__ == "__main__":
    log.info("启动 umans2api：http://%s:%d", HOST, PORT)
    log.info("默认模型: %s", DEFAULT_MODEL)
    log.info("可用模型: %s", ", ".join(AVAILABLE_MODELS))
    log.info("Claude 映射: %s", CLAUDE_MODEL_MAP)
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
