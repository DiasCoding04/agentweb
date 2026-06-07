from __future__ import annotations

# server.py — FastAPI backend (optimised)
import asyncio
import json
import logging
import os
import time
import uuid
import urllib.request
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import (
    get_gemini_api_key,
    verify_gemini_api_key,
    use_vertex,
    get_vertex_project,
    get_vertex_location,
    get_vertex_credentials
)

load_dotenv()
logging.getLogger("browser_use").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Lazy imports ─────────────────────────────────────────────────────────────
try:
    from browser_use import Agent, Browser, BrowserConfig
    from browser_use.controller.service import Controller
    from browser_use.agent.views import ActionResult
    from browser_use.browser.context import BrowserContext
    BROWSER_USE_AVAILABLE = True
except ImportError:
    Agent = Any  # type: ignore[misc, assignment]
    Browser = Any  # type: ignore[misc, assignment]
    BrowserConfig = Any  # type: ignore[misc, assignment]
    Controller = Any
    ActionResult = Any
    BrowserContext = Any
    BROWSER_USE_AVAILABLE = False

try:
    from langchain_core.callbacks import UsageMetadataCallbackHandler
    from langchain_google_genai import ChatGoogleGenerativeAI
    GOOGLE_LLM_AVAILABLE = True
except ImportError:
    UsageMetadataCallbackHandler = Any  # type: ignore[misc, assignment]
    ChatGoogleGenerativeAI = Any  # type: ignore[misc, assignment]
    GOOGLE_LLM_AVAILABLE = False

# ── Custom Controller ────────────────────────────────────────────────────────
controller = Controller() if BROWSER_USE_AVAILABLE else None

if BROWSER_USE_AVAILABLE:
    class ScrollElementAction(BaseModel):
        index: int = Field(..., description="Chỉ số (index) của phần tử cần cuộn từ danh sách DOM")
        amount: int = Field(150, description="Số pixel cần cuộn (số dương cho cuộn xuống/phải, số âm cho cuộn lên/trái)")
        direction: str = Field("down", description="Hướng cuộn: 'down', 'up', 'left', hoặc 'right'")

    @controller.registry.action(
        'Scroll a specific scrollable element (e.g. dropdown menu, inner div, scrollable panel) by its index',
        param_model=ScrollElementAction
    )
    async def scroll_element(index, amount, direction, browser) -> ActionResult:
        try:
            selector_map = await browser.get_selector_map()
            if index not in selector_map:
                raise Exception(f"Không tìm thấy phần tử có index {index} trên trang")

            element_node = await browser.get_dom_element_by_index(index)
            element_handle = await browser.get_locate_element(element_node)

            if not element_handle:
                raise Exception(f"Không tìm thấy handle của phần tử index {index}")

            scroll_amount = amount
            if direction in ("up", "left"):
                scroll_amount = -abs(scroll_amount)
            else:
                scroll_amount = abs(scroll_amount)

            if direction in ("left", "right"):
                await element_handle.evaluate(f'''(el) => {{
                    function findScrollableParent(e) {{
                        let current = e;
                        while (current && current !== document.body && current !== document.documentElement) {{
                            const style = window.getComputedStyle(current);
                            const overflowX = style.getPropertyValue('overflow-x') || style.getPropertyValue('overflow');
                            if ((overflowX === 'scroll' || overflowX === 'auto') && current.scrollWidth > current.clientWidth) {{
                                return current;
                            }}
                            current = current.parentElement;
                        }}
                        return e;
                    }}
                    const scrollTarget = findScrollableParent(el);
                    scrollTarget.scrollBy({scroll_amount}, 0);
                }}''')
                msg = f"📜 Đã cuộn phần tử (hoặc phần tử cha) index {index} theo chiều ngang {scroll_amount} pixel"
            else:
                await element_handle.evaluate(f'''(el) => {{
                    function findScrollableParent(e) {{
                        let current = e;
                        while (current && current !== document.body && current !== document.documentElement) {{
                            const style = window.getComputedStyle(current);
                            const overflowY = style.getPropertyValue('overflow-y') || style.getPropertyValue('overflow');
                            if ((overflowY === 'scroll' || overflowY === 'auto') && current.scrollHeight > current.clientHeight) {{
                                return current;
                            }}
                            current = current.parentElement;
                        }}
                        return e;
                    }}
                    const scrollTarget = findScrollableParent(el);
                    scrollTarget.scrollBy(0, {scroll_amount});
                }}''')
                msg = f"📜 Đã cuộn phần tử (hoặc phần tử cha) index {index} theo chiều dọc {scroll_amount} pixel"

            return ActionResult(extracted_content=msg, include_in_memory=True)
        except Exception as e:
            return ActionResult(error=str(e))

    class ClickElementByTextAction(BaseModel):
        text: str = Field(..., description="Văn bản của phần tử cần click (ví dụ: tên chi nhánh, nhãn nút)")

    @controller.registry.action(
        'Click an element by its text content directly',
        param_model=ClickElementByTextAction
    )
    async def click_element_by_text(text, browser) -> ActionResult:
        try:
            page = await browser.get_current_page()

            # 1. Search clickable elements first
            locator = page.locator('button, a, [role="button"], input[type="submit"], input[type="button"]').filter(has_text=text)
            try:
                # Wait up to 5s for an interactive element matching this text to become visible
                await locator.first.wait_for(state="visible", timeout=5000)
            except Exception:
                pass

            count = 0
            try:
                count = await locator.count()
            except Exception:
                pass

            # 2. Fallback to generic text selector
            if count == 0:
                locator = page.get_by_text(text)
                try:
                    await locator.first.wait_for(state="visible", timeout=3000)
                except Exception:
                    pass
                try:
                    count = await locator.count()
                except Exception:
                    pass

            # 3. Last resort fallback
            if count == 0:
                locator = page.locator(f"text={text}")
                try:
                    await locator.first.wait_for(state="visible", timeout=2000)
                except Exception:
                    pass
                try:
                    count = await locator.count()
                except Exception:
                    pass

            if count == 0:
                raise Exception(f"Không tìm thấy phần tử nào chứa text: '{text}'")

            clicked = False
            for i in range(count):
                el = locator.nth(i)
                if await el.is_visible():
                    await el.click()
                    clicked = True
                    break

            if not clicked:
                await locator.first.click()

            msg = f"🖱️ Clicked element by text: {text}"
            return ActionResult(extracted_content=msg, include_in_memory=True)
        except Exception as e:
            return ActionResult(error=str(e))

# ── Constants ─────────────────────────────────────────────────────────────────
if BROWSER_USE_AVAILABLE:
    class FillLoginFormAction(BaseModel):
        username: str = Field(..., description="Username, phone number, email, or account id")
        password: str = Field(..., description="Password for the login form")

    @controller.registry.action(
        'Fill a login form using semantic username and password fields, not DOM indexes',
        param_model=FillLoginFormAction
    )
    async def fill_login_form(username, password, browser) -> ActionResult:
        try:
            page = await browser.get_current_page()

            async def first_visible_locator(selectors: list[str]):
                for selector in selectors:
                    locator = page.locator(selector)
                    try:
                        count = await locator.count()
                    except Exception:
                        continue
                    for i in range(min(count, 12)):
                        target = locator.nth(i)
                        try:
                            if await target.is_visible():
                                return target, selector
                        except Exception:
                            continue
                return None, ""

            password_target, password_selector = await first_visible_locator([
                'input[type="password"]',
                'input[autocomplete="current-password"]',
                'input[name*="pass" i]',
                'input[placeholder*="pass" i]',
                'input[placeholder*="mat khau" i]',
            ])
            if password_target is None:
                raise Exception("Could not find a visible password field")

            username_target, username_selector = await first_visible_locator([
                'input[autocomplete="username"]',
                'input[type="tel"]',
                'input[type="email"]',
                'input[name*="phone" i]',
                'input[name*="user" i]',
                'input[name*="login" i]',
                'input[name*="account" i]',
                'input[placeholder*="dien thoai" i]',
                'input[placeholder*="ten dang nhap" i]',
                'input[placeholder*="username" i]',
                'input:not([type="hidden"]):not([type="password"])',
            ])
            if username_target is None:
                raise Exception("Could not find a visible username field")

            await username_target.fill(str(username))
            await password_target.fill(str(password))
            msg = f"Filled login form semantically using {username_selector} and {password_selector}"
            return ActionResult(extracted_content=msg, include_in_memory=True)
        except Exception as e:
            return ActionResult(error=str(e))

HISTORY_FILE             = Path("chat_history.json")
USER_PROFILES_FILE       = Path("local/user_profiles.json")
SITE_PROFILES_FILE       = Path("local/site_profiles.json")
GEMINI_API_KEY           = get_gemini_api_key()
MAX_CONTEXT_CHARS        = 900
MAX_CONTEXT_MESSAGES     = 6
BROWSER_START_TIMEOUT    = 15
BROWSER_CONTEXT_TIMEOUT  = 10
MAX_AGENT_STEPS          = 1200
MAX_AGENT_SECONDS        = 4 * 60 * 60
MAX_INPUT_TOKENS         = 180_000
MAX_ACTIONS_PER_STEP     = 10
PLANNER_INTERVAL         = 4
MAX_REPEAT_ACTIONS       = 4
MAX_FAILS_BEFORE_ASK     = 2
DEFAULT_USD_TO_VND       = 26_000.0
EXCHANGE_RATE_CACHE_SECONDS = 900
EXCHANGE_RATE_URL        = "https://open.er-api.com/v6/latest/USD"
GEMINI_PRICING_SOURCE    = "https://ai.google.dev/gemini-api/docs/pricing"
GEMINI_PRICING_UPDATED_AT = "2026-05-27"
WORKFLOW_SCHEMA_VERSION  = 3
WORKFLOW_EXACT_SCORE     = 1.0
WORKFLOW_FUZZY_THRESHOLD = 0.78
WORKFLOW_REPLAY_MIN_CONFIDENCE = 0.42
WORKFLOW_RUNTIME_VERSION = 4
WORKFLOW_LOCAL_REPAIR_LIMIT = 4
WORKFLOW_AI_RECOVERY_MAX_STEPS = 6
WORKFLOW_AI_RECOVERY_MAX_INTERVENTIONS = 2
WORKFLOW_TEXT_SNAPSHOT_CHARS = 8000


@dataclass
class ReplayStepResult:
    ok: bool
    action_name: str
    resolver: str
    message: str = ""
    error: str = ""
    done: bool = False
    final_text: str = ""
    repaired: bool = False
    skipped: bool = False
    verification: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowRecoveryResult:
    ok: bool
    message: str = ""
    error: str = ""
    verification: dict[str, Any] = field(default_factory=dict)
    final_text: str = ""

MODEL_OPTIONS = [
    {"id": "gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash-Lite",
     "description": "Mặc định: siêu nhanh, siêu rẻ, tối ưu cho agent."},
    {"id": "gemini-3.1-pro",        "label": "Gemini 3.1 Pro",
     "description": "Planner mới: suy luận mạnh mẽ, thiết lập kế hoạch phức tạp."},
    {"id": "gemini-3.5-flash",      "label": "Gemini 3.5 Flash",
     "description": "Cân bằng tốc độ/độ chính xác."},
    {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash-Lite",
     "description": "Nhanh, rẻ cho đa số tác vụ."},
    {"id": "gemini-2.5-flash",      "label": "Gemini 2.5 Flash",
     "description": "Cân bằng tốc độ/độ chính xác."},
    {"id": "gemini-2.5-pro",        "label": "Gemini 2.5 Pro",
     "description": "Mạnh nhất thế hệ 2.5, tác vụ khó/dài."},
    {"id": "gemini-2.0-flash",      "label": "Gemini 2.0 Flash",
     "description": "Nhanh, ổn định cho tác vụ hàng loạt."},
]
MODEL_IDS      = {item["id"] for item in MODEL_OPTIONS}
MODEL_DEFAULTS = {
    "executor_model": "gemini-3.1-flash-lite",
    "planner_model": "gemini-3.5-flash",
    "vision_mode": "auto",
}
MODEL_CONFIG = dict(MODEL_DEFAULTS)

VISION_MODES = frozenset({"auto", "on", "off"})
VISION_MODE_DEFAULT = "auto"
MAX_VISION_STEPS_PER_TASK = 12

MODEL_PRICING_USD_PER_1M = {
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50,  "cached": 0.025},
    "gemini-3.1-pro":        {"input": 2.00, "output": 12.00, "cached": 0.20},
    "gemini-3.5-flash":      {"input": 1.50, "output": 9.00,  "cached": 0.15},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40,  "cached": 0.01},
    "gemini-2.5-flash":      {"input": 0.30, "output": 2.50,  "cached": 0.03},
    "gemini-2.5-pro":        {"input": 1.25, "output": 10.00, "cached": 0.125},
    "gemini-2.0-flash":      {"input": 0.075, "output": 0.30, "cached": 0.0075},
}

STUCK_ACTIONS = {
    "scroll_down", "scroll_up", "click_element",
    "extract_content", "search_google", "wait",
}

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ── Global state ──────────────────────────────────────────────────────────────
sessions: dict[str, dict[str, Any]] = {}
stop_requests: set[str] = set()
_stop_lock = asyncio.Lock()
browser_instance = None

# FIX 1: In-memory history cache — eliminates repeated disk reads/writes per request
_history_cache: dict | None = None
_history_lock = asyncio.Lock()

exchange_rate_cache = {
    "usd_to_vnd": DEFAULT_USD_TO_VND,
    "updated_at": None,
    "source": "fallback",
    "stale": True,
    "fetched_at": None,
}

# FIX 2: API-key verified once at startup, not on every chat request
# verify_gemini_api_key() calls llm.invoke("OK") which wastes ~1-2s + real tokens
# each time a user sends a message. We verify once here and cache the result.
_key_ok: bool = False
_key_err: str = ""

def _init_key_check() -> None:
    global _key_ok, _key_err
    ok, msg = verify_gemini_api_key(GEMINI_API_KEY)
    _key_ok = ok
    _key_err = msg

# Run synchronously at import time (server start). After this, _key_ok is reliable
# for the lifetime of the process. If the key changes, restart the server.
if not use_vertex() and GEMINI_API_KEY:
    try:
        _init_key_check()
    except Exception as exc:
        _key_err = str(exc)


# ── Utility ───────────────────────────────────────────────────────────────────
async def with_timeout(awaitable, timeout_seconds: int, operation_name: str):
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"{operation_name} quá thời gian sau {timeout_seconds} giây") from exc
    except Exception as exc:
        raise RuntimeError(f"{operation_name} thất bại: {exc}") from exc


# ── Chat history helpers (in-memory cache) ────────────────────────────────────
# FIX 1 (continued): Every load_history() previously read the JSON file from disk.
# In a long task, add_message() is called after each turn which does:
#   load_history() → mutate → save_history()
# and then at stream end load_history() is called two more times to refresh context
# and update the sidebar title — so 4–5 disk reads/writes per user message.
# With _history_cache, all reads are from memory; writes still persist to disk.

def load_history() -> dict:
    global _history_cache
    if _history_cache is None:
        if HISTORY_FILE.exists():
            try:
                _history_cache = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            except Exception:
                _history_cache = {}
        else:
            _history_cache = {}
    return _history_cache


def save_history(history: dict) -> None:
    global _history_cache
    _history_cache = history  # keep cache in sync
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add_message(chat_id: str, role: str, content: str, metadata: dict | None = None) -> None:
    history = load_history()
    if chat_id not in history:
        history[chat_id] = {
            "id": chat_id,
            "title": content[:40] + ("..." if len(content) > 40 else ""),
            "created_at": datetime.now().isoformat(),
            "messages": [],
        }
    message: dict[str, Any] = {
        "role": role,
        "content": content,
        "time": datetime.now().strftime("%H:%M"),
    }
    if metadata is not None:
        message["metadata"] = metadata
    history[chat_id]["messages"].append(message)
    save_history(history)


async def load_history_async() -> dict:
    async with _history_lock:
        return load_history()


async def add_message_async(
    chat_id: str, role: str, content: str, metadata: dict | None = None
) -> None:
    async with _history_lock:
        add_message(chat_id, role, content, metadata)


async def save_history_async(history: dict) -> None:
    async with _history_lock:
        save_history(history)


# ── User and Site Profile helpers (Behavioral Training Loop) ─────────────────
def load_user_profiles() -> list[str]:
    if not USER_PROFILES_FILE.exists():
        USER_PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_PROFILES_FILE.write_text(json.dumps(["Default"], ensure_ascii=False), encoding="utf-8")
        return ["Default"]
    try:
        return json.loads(USER_PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ["Default"]


def save_user_profiles(profiles: list[str]) -> None:
    USER_PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_PROFILES_FILE.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")


def load_site_profiles() -> dict:
    if not SITE_PROFILES_FILE.exists():
        return {}
    try:
        profiles = json.loads(SITE_PROFILES_FILE.read_text(encoding="utf-8"))
        normalized, changed = normalize_site_profiles(profiles)
        if changed:
            save_site_profiles(normalized)
        return normalized
    except Exception:
        return {}


def save_site_profiles(profiles: dict) -> None:
    SITE_PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SITE_PROFILES_FILE.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def normalize_for_match(text: str) -> str:
    text = normalize_text(text).lower()
    text = re.sub(r"[^\w\s{}:/.-]+", " ", text, flags=re.UNICODE)
    return normalize_text(text)


def token_set(text: str) -> set[str]:
    return {t for t in re.split(r"\s+", normalize_for_match(text)) if len(t) >= 2}


def jaccard_score(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a | b), 1)


def workflow_action_names(steps: list) -> list[str]:
    names: list[str] = []
    for step in steps or []:
        name, _ = action_name_and_params(step)
        if name:
            names.append(name)
    return names


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def semantic_prompt(text: str) -> str:
    return normalize_text(strip_accents(text).lower())


def extract_semantic_slots(task: str) -> dict[str, str]:
    """Extract named user parameters from natural language task text.

    This is intentionally rule-first and deterministic. It keeps replay cheap and
    turns opaque var_1/var_2 placeholders into user-facing names.
    """
    slots: dict[str, str] = {}
    patterns = [
        ("username", r"(?:ten\s*dang\s*nhap|username|user|tai\s*khoan|so\s*dien\s*thoai|sdt)\s*[:=\-]?\s*([^\s,;]+)"),
        ("password", r"(?:pass|password|mat\s*khau)\s*[:=\-]?\s*([^\s,;]+)"),
        ("branch_name", r"(?:chon|chọn|tim|tìm|vao|vào)\s+(?:chi\s*nhanh|chi\s*nhánh)\s+([^\s,.;]+)"),
        ("branch_name", r"(?:chi\s*nhanh|chi\s*nhánh)\s+([^\s,.;]+)\s*$"),
        ("search_query", r"(?:tim|tìm|search|google)\s+(.+)$"),
    ]
    folded = semantic_prompt(task)
    for name, pattern in patterns:
        if name in slots:
            continue
        match = re.search(pattern, folded, flags=re.IGNORECASE)
        if match:
            value = normalize_text(match.group(1))
            if value:
                slots[name] = value
    url_match = re.search(r"((?:https?://)?[a-z0-9.-]+\.[a-z]{2,})(?:\s|$)", folded)
    if url_match:
        slots.setdefault("target_url", url_match.group(1))
    return slots


def unique_variable_name(base: str, used: set[str]) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_]+", "_", base).strip("_").lower() or "param"
    if clean not in used:
        used.add(clean)
        return clean
    idx = 2
    while f"{clean}_{idx}" in used:
        idx += 1
    name = f"{clean}_{idx}"
    used.add(name)
    return name


def variable_schema_for(name: str) -> dict:
    labels = {
        "username": "Tên đăng nhập",
        "password": "Mật khẩu",
        "branch_name": "Tên chi nhánh",
        "search_query": "Từ khóa tìm kiếm",
        "target_url": "Website/URL",
    }
    descriptions = {
        "username": "Tài khoản, số điện thoại hoặc email dùng để đăng nhập.",
        "password": "Mật khẩu đăng nhập. Giá trị này nhạy cảm và không nên hiển thị lại.",
        "branch_name": "Tên chi nhánh/cửa hàng cần chọn trong bộ lọc hoặc dropdown.",
        "search_query": "Nội dung cần tìm kiếm.",
        "target_url": "Website hoặc URL đích của workflow.",
    }
    return {
        "name": name,
        "label": labels.get(name, name.replace("_", " ").title()),
        "description": descriptions.get(name, f"Giá trị cho tham số {name}."),
        "type": "password" if name == "password" else "text",
        "required": True,
        "sensitive": name == "password",
        "placeholder": labels.get(name, name),
    }


def order_variables(variables: list[str]) -> list[str]:
    priority = {
        "target_url": 5,
        "username": 10,
        "password": 20,
        "branch_name": 30,
        "search_query": 40,
    }
    return sorted(list(dict.fromkeys(variables)), key=lambda name: (priority.get(str(name), 100), str(name)))


def replace_value_case_insensitive(text: str, raw: str, placeholder: str) -> str:
    return re.compile(re.escape(raw), re.IGNORECASE).sub(placeholder, text)


def build_variable_mapping(original_task: str, candidate_values: list[str]) -> tuple[str, dict[str, str], list[str], list[dict]]:
    slots = extract_semantic_slots(original_task)
    used: set[str] = set()
    value_to_name: dict[str, str] = {}

    for slot_name, slot_value in slots.items():
        for value in candidate_values + [slot_value]:
            if semantic_prompt(value) == semantic_prompt(slot_value):
                value_to_name[value] = unique_variable_name(slot_name, used)
                break

    for value in candidate_values:
        if value in value_to_name:
            continue
        if len(value) < 2 or semantic_prompt(value) not in semantic_prompt(original_task):
            continue
        inferred = "param"
        if re.fullmatch(r"[0-9+().-]{7,}", value):
            inferred = "username"
        elif len(value) >= 6 and re.search(r"[A-Za-z]", value) and re.search(r"\d", value):
            inferred = "password"
        value_to_name[value] = unique_variable_name(inferred, used)

    prompt_template = original_task
    for raw, name in sorted(value_to_name.items(), key=lambda item: len(item[0]), reverse=True):
        prompt_template = replace_value_case_insensitive(prompt_template, raw, f"{{{name}}}")

    variables = order_variables(list(value_to_name.values()))
    input_schema = [variable_schema_for(name) for name in variables]
    return prompt_template, value_to_name, variables, input_schema


def replace_params_with_variables(params: dict, value_to_name: dict[str, str]) -> dict:
    new_params = dict(params)
    for key, value in list(new_params.items()):
        if not isinstance(value, str):
            continue
        for raw, name in value_to_name.items():
            if semantic_prompt(value) == semantic_prompt(raw):
                new_params[key] = f"{{{name}}}"
                break
    return new_params


def semanticize_workflow_steps(steps: list[dict], variables: list[str]) -> list[dict]:
    if not steps:
        return []
    semantic_steps: list[dict] = []
    login_buffer: dict[str, str] = {}
    login_inserted = False

    def flush_login() -> None:
        nonlocal login_inserted, login_buffer
        if not login_inserted and {"username", "password"}.issubset(login_buffer):
            semantic_steps.append({
                "fill_login_form": {
                    "username": login_buffer["username"],
                    "password": login_buffer["password"],
                }
            })
            login_inserted = True
        login_buffer = {}

    for step in steps:
        action_name, params = action_name_and_params(step)
        if action_name == "input_text" and params.get("text") in {"{username}", "{password}"}:
            key = str(params.get("text")).strip("{}")
            login_buffer[key] = params["text"]
            if {"username", "password"}.issubset(login_buffer):
                flush_login()
            continue
        flush_login()
        semantic_steps.append(step)
    flush_login()
    return semantic_steps


def workflow_validation_issues(workflow_like: dict) -> list[str]:
    variables = set(workflow_like.get("variables") or [])
    steps = workflow_like.get("steps") or []
    actions = workflow_action_names(steps)
    issues: list[str] = []

    if any(re.fullmatch(r"var_\d+", str(v)) for v in variables):
        issues.append("workflow_has_opaque_var_names")

    if "password" in variables and "username" in variables and "fill_login_form" not in actions:
        issues.append("credential_workflow_must_use_fill_login_form")

    for step in steps:
        action_name, params = action_name_and_params(step)
        if action_name == "input_text" and params.get("text") == "{password}":
            issues.append("password_must_not_use_raw_input_text_index")
        if action_name == "fill_login_form" and params.get("username") == params.get("password"):
            issues.append("username_password_values_must_differ")
    return list(dict.fromkeys(issues))


def workflow_brittle_actions(steps: list[dict]) -> list[str]:
    brittle: list[str] = []
    for step in steps or []:
        action_name, params = action_name_and_params(step)
        if action_name in {"click_element", "click_element_by_index"}:
            brittle.append(action_name)
        elif action_name == "input_text" and "index" in params:
            brittle.append(action_name)
    return list(dict.fromkeys(brittle))


def workflow_replay_quality_issues(wf: dict) -> list[str]:
    """Detect workflows that are too opaque to replay safely.

    A long workflow without slots/checkpoints and many index actions cannot be
    handed back safely after recovery because the runtime cannot prove where it
    is in the business process.
    """
    steps = wf.get("steps") or []
    variables = wf.get("variables") or []
    actions = workflow_action_names(steps)
    brittle_count = sum(1 for action in actions if action in {"click_element", "click_element_by_index", "input_text"})
    issues: list[str] = []
    if not variables and len(steps) >= 10 and brittle_count >= 4:
        issues.append("workflow_lacks_slots_for_long_brittle_replay")
    if len(steps) >= 20 and actions.count("click_element_by_index") >= 4:
        issues.append("workflow_too_index_heavy_for_replay")
    return issues


def runtime_metadata_defaults(metadata: dict, steps: list[dict]) -> tuple[dict, bool]:
    runtime = dict(metadata.get("runtime") or {})
    changed = False
    defaults = {
        "runtime_version": WORKFLOW_RUNTIME_VERSION,
        "replay_strategy": "semantic_capability",
        "resolver_hints": runtime.get("resolver_hints") or {},
        "resolver_stats": runtime.get("resolver_stats") or {},
        "last_replay_trace": runtime.get("last_replay_trace") or [],
        "last_replay_success": runtime.get("last_replay_success"),
        "last_failure_reason": runtime.get("last_failure_reason") or "",
    }
    for key, value in defaults.items():
        if key not in runtime or runtime.get(key) in (None, ""):
            runtime[key] = value
            changed = True
    brittle = workflow_brittle_actions(steps)
    if runtime.get("brittle_actions") != brittle:
        runtime["brittle_actions"] = brittle
        changed = True
    if metadata.get("runtime") != runtime:
        metadata["runtime"] = runtime
        changed = True
    return metadata, changed


def infer_legacy_variable_renames(prompt_template: str, variables: list[str]) -> dict[str, str]:
    folded = semantic_prompt(prompt_template)
    renames: dict[str, str] = {}
    used: set[str] = set(v for v in variables if not re.fullmatch(r"var_\d+", str(v)))
    rules = [
        ("username", r"(?:ten\s*dang\s*nhap|username|user|tai\s*khoan|sdt|so\s*dien\s*thoai)\s*[:=\-]?\s*"),
        ("password", r"(?:pass|password|mat\s*khau)\s*[:=\-]?\s*"),
        ("branch_name", r"(?:chi\s*nhanh|chi\s*nhánh)\s+"),
        ("search_query", r"(?:tim|tìm|search|google)\s+"),
    ]
    for var in variables:
        if not re.fullmatch(r"var_\d+", str(var)):
            continue
        placeholder = re.escape("{" + str(var).lower() + "}")
        for target, prefix in rules:
            if target in used:
                continue
            if re.search(prefix + placeholder, folded):
                renames[str(var)] = unique_variable_name(target, used)
                break
    return renames


def replace_placeholders_deep(value: Any, renames: dict[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in renames.items():
            value = value.replace(f"{{{old}}}", f"{{{new}}}")
        return value
    if isinstance(value, list):
        return [replace_placeholders_deep(item, renames) for item in value]
    if isinstance(value, dict):
        return {key: replace_placeholders_deep(item, renames) for key, item in value.items()}
    return value


def migrate_workflow_semantics(wf: dict) -> tuple[dict, bool]:
    changed = False
    wf = dict(wf)
    variables = list(wf.get("variables") or [])
    renames = infer_legacy_variable_renames(wf.get("prompt_template") or "", variables)
    if renames:
        wf = replace_placeholders_deep(wf, renames)
        wf["variables"] = order_variables([renames.get(str(v), v) for v in variables])
        changed = True
    else:
        ordered_variables = order_variables(variables)
        if ordered_variables != variables:
            wf["variables"] = ordered_variables
            changed = True

    steps = wf.get("steps") or []
    semantic_steps = semanticize_workflow_steps(steps, wf.get("variables") or [])
    if semantic_steps != steps:
        wf["steps"] = semantic_steps
        changed = True

    metadata = dict(wf.get("metadata") or {})
    input_schema = metadata.get("input_schema")
    schema_names = [item.get("name") for item in input_schema or [] if isinstance(item, dict)]
    if not input_schema or input_schema == metadata.get("inputs") or schema_names != (wf.get("variables") or []):
        metadata["input_schema"] = [variable_schema_for(v) for v in wf.get("variables") or []]
        changed = True
    validation = workflow_validation_issues(wf)
    quality_issues = workflow_replay_quality_issues(wf)
    if metadata.get("validation_issues") != validation:
        metadata["validation_issues"] = validation
        changed = True
    if metadata.get("replay_quality_issues") != quality_issues:
        metadata["replay_quality_issues"] = quality_issues
        changed = True
    if metadata.get("action_names") != workflow_action_names(wf.get("steps") or []):
        metadata["action_names"] = workflow_action_names(wf.get("steps") or [])
        changed = True
    wf["metadata"] = metadata
    return wf, changed


def workflow_metadata_defaults(wf: dict, domain: str) -> tuple[dict, bool]:
    changed = False
    wf, semantic_changed = migrate_workflow_semantics(dict(wf))
    changed = changed or semantic_changed
    prompt = wf.get("prompt_template") or ""
    name = wf.get("workflow_name") or "Workflow"
    steps = wf.get("steps") or []
    variables = wf.get("variables") or []

    metadata = dict(wf.get("metadata") or {})
    if metadata.get("schema_version") != WORKFLOW_SCHEMA_VERSION:
        metadata["schema_version"] = WORKFLOW_SCHEMA_VERSION
        changed = True
    defaults = {
        "intent": metadata.get("intent") or prompt or name,
        "description": metadata.get("description") or f"{name}: {prompt}",
        "domain": metadata.get("domain") or domain,
        "inputs": metadata.get("inputs") or variables,
        "input_schema": metadata.get("input_schema") or [variable_schema_for(v) for v in variables],
        "success_criteria": metadata.get("success_criteria") or "Workflow replay finishes without action errors.",
        "avoid_when": metadata.get("avoid_when") or [],
        "failure_patterns": metadata.get("failure_patterns") or [],
        "examples": metadata.get("examples") or ([prompt] if prompt else []),
        "action_names": metadata.get("action_names") or workflow_action_names(steps),
        "validation_issues": workflow_validation_issues(wf),
        "replay_quality_issues": workflow_replay_quality_issues(wf),
    }
    for key, value in defaults.items():
        if key not in metadata or metadata.get(key) in (None, "", []):
            metadata[key] = value
            changed = True
    metadata, runtime_changed = runtime_metadata_defaults(metadata, steps)
    changed = changed or runtime_changed

    stats = dict(wf.get("stats") or {})
    stat_defaults = {
        "success_count": 0,
        "fail_count": 0,
        "replay_count": 0,
        "fallback_count": 0,
        "repair_count": 0,
        "avg_replay_tokens": 0,
        "last_success_at": None,
        "last_failure_at": None,
        "last_repaired_at": None,
        "last_repair_tokens": 0,
        "last_error": "",
    }
    for key, value in stat_defaults.items():
        if key not in stats:
            stats[key] = value
            changed = True

    if "confidence" not in wf:
        wf["confidence"] = 0.62
        changed = True
    if wf.get("metadata") != metadata:
        wf["metadata"] = metadata
        changed = True
    if wf.get("stats") != stats:
        wf["stats"] = stats
        changed = True
    return wf, changed


def normalize_site_profiles(profiles: dict) -> tuple[dict, bool]:
    if not isinstance(profiles, dict):
        return {}, True
    changed = False
    normalized: dict[str, list[dict]] = {}
    for domain, workflows in profiles.items():
        if not isinstance(workflows, list):
            changed = True
            continue
        normalized[domain] = []
        for wf in workflows:
            if not isinstance(wf, dict):
                changed = True
                continue
            new_wf, wf_changed = workflow_metadata_defaults(wf, domain)
            normalized[domain].append(new_wf)
            changed = changed or wf_changed
    return normalized, changed


def template_to_regex(template: str) -> tuple[re.Pattern, list[str]]:
    vars_found = re.findall(r"\{([a-zA-Z0-9_]+)\}", template)
    tokens = re.split(r"\{[a-zA-Z0-9_]+\}", template)
    escaped_tokens = [re.escape(t) for t in tokens]
    regex_str = "^" + "(.+?)".join(escaped_tokens) + "$"
    pattern = re.compile(regex_str, re.IGNORECASE | re.DOTALL)
    return pattern, vars_found


def workflow_health(wf: dict) -> float:
    stats = wf.get("stats") or {}
    success = int(stats.get("success_count") or 0)
    fail = int(stats.get("fail_count") or 0)
    total = success + fail
    confidence = float(wf.get("confidence", 0.62) or 0.62)
    if total:
        observed = success / total
        confidence = (confidence * 0.55) + (observed * 0.45)
    if fail >= 3 and success == 0:
        confidence *= 0.45
    return max(0.0, min(confidence, 1.0))


def score_workflow_candidate(wf: dict, domain: str, user_prompt: str) -> tuple[float, str]:
    prompt = normalize_for_match(user_prompt)
    template = normalize_for_match(wf.get("prompt_template") or "")
    metadata = wf.get("metadata") or {}
    health = workflow_health(wf)

    if not template:
        return 0.0, "missing_template"

    if (metadata.get("workflow_scope") or "").strip().lower() == "specific":
        if prompt == template:
            return WORKFLOW_EXACT_SCORE, "specific_exact"
        return 0.0, "specific_requires_exact_match"

    _, vars_found = template_to_regex(normalize_text(wf.get("prompt_template") or ""))
    if vars_found:
        return 0.0, "variables_require_exact_match"

    texts = [
        template,
        normalize_for_match(wf.get("workflow_name") or ""),
        normalize_for_match(metadata.get("intent") or ""),
        normalize_for_match(metadata.get("description") or ""),
        " ".join(normalize_for_match(x) for x in metadata.get("examples") or []),
    ]
    haystack = normalize_text(" ".join(t for t in texts if t))
    seq = max(SequenceMatcher(None, prompt, t).ratio() for t in texts if t)
    overlap = jaccard_score(token_set(prompt), token_set(haystack))
    score = (seq * 0.58) + (overlap * 0.28) + (health * 0.14)
    if domain != "unknown_domain" and domain in prompt:
        score += 0.08
    return max(0.0, min(score, 1.0)), "fuzzy"


def build_workflow_match(
    *,
    domain: str,
    wf: dict,
    var_values: dict[str, str],
    score: float,
    match_type: str,
) -> dict:
    steps = wf.get("steps", [])
    return {
        "domain": domain,
        "workflow": wf,
        "actions": instantiate_workflow(steps, var_values),
        "variables": var_values,
        "score": round(score, 4),
        "match_type": match_type,
        "confidence": workflow_health(wf),
    }


def instantiate_workflow(steps: list, var_values: dict[str, str]) -> list[dict]:
    instantiated = []
    for step in steps:
        new_step = {}
        for action_name, params in step.items():
            new_params = {}
            for k, v in params.items():
                if isinstance(v, str):
                    val_str = v
                    for var_name, var_val in var_values.items():
                        val_str = val_str.replace(f"{{{var_name}}}", var_val)
                    new_params[k] = val_str
                else:
                    new_params[k] = v
            new_step[action_name] = new_params
        instantiated.append(new_step)
    return instantiated


def find_matching_workflow(user_profile: str, user_prompt: str) -> dict | None:
    site_profiles = load_site_profiles()
    normalized_prompt = normalize_text(user_prompt)
    candidates: list[dict] = []

    for domain, workflows in site_profiles.items():
        for wf in workflows:
            if wf.get("user_profile") != user_profile and wf.get("user_profile") != "Default":
                continue

            template = wf.get("prompt_template", "")
            if not template:
                continue

            normalized_template = normalize_text(template)
            pattern, vars_found = template_to_regex(normalized_template)

            match = pattern.match(normalized_prompt)
            if match:
                groups = match.groups()
                var_values = {}
                for idx, var_name in enumerate(vars_found):
                    if idx < len(groups):
                        var_values[var_name] = groups[idx]

                score = 10.0 + workflow_health(wf)
                candidates.append(build_workflow_match(
                    domain=domain,
                    wf=wf,
                    var_values=var_values,
                    score=score,
                    match_type="template_exact",
                ))
                continue

            score, match_type = score_workflow_candidate(wf, domain, user_prompt)
            if score >= WORKFLOW_FUZZY_THRESHOLD:
                candidates.append(build_workflow_match(
                    domain=domain,
                    wf=wf,
                    var_values={},
                    score=score,
                    match_type=match_type,
                ))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
    best = candidates[0]
    try:
        print(
            f"[Match Found] workflow='{best.get('workflow', {}).get('workflow_name')}' "
            f"domain='{best.get('domain')}' type={best.get('match_type')} score={best.get('score')}"
        )
    except Exception:
        pass
    return best


def first_action_name(actions: list[dict] | None) -> str:
    if not actions:
        return ""
    first = actions[0]
    if not isinstance(first, dict) or not first:
        return ""
    return next(iter(first.keys()), "")


def workflow_has_navigation(actions: list[dict]) -> bool:
    return first_action_name(actions) in {"go_to_url", "search_google", "open_tab"}


def action_name_and_params(action: dict) -> tuple[str, dict]:
    if not isinstance(action, dict) or not action:
        return "", {}
    name = next(iter(action.keys()), "")
    params = action.get(name) or {}
    return name, params if isinstance(params, dict) else {}


def normalize_workflow_actions(match: dict, task: str, current_url: str = "") -> list[dict]:
    """Make saved workflows less tied to the browser's current page.

    Saved histories can miss the original navigation step (for example when the
    agent started from a page that was already open). Replay should be explicit:
    if the workflow is tied to a known domain and the current page is elsewhere,
    navigate there first. For unknown domains, use only high-confidence task
    inference such as "youtube" or an explicit URL.
    """
    actions = list(match.get("actions") or [])
    domain = (match.get("domain") or "").strip()
    current = (current_url or "").lower()

    if workflow_has_navigation(actions):
        return actions

    if domain and domain != "unknown_domain" and domain not in current:
        return [{"go_to_url": {"url": f"https://{domain}"}}] + actions

    inferred = infer_initial_actions(task) or []
    if inferred:
        return inferred + actions

    return actions


def replay_done_text(actions: list[dict]) -> str:
    for action in reversed(actions):
        name, params = action_name_and_params(action)
        if name == "done":
            return str(params.get("text") or "Hoan thanh workflow da hoc.")
    return "Hoan thanh workflow da hoc."


def replay_final_text(match: dict, actions: list[dict], task: str) -> str:
    text = replay_done_text(actions)
    if match.get("variables") and "{" not in text:
        wf = match.get("workflow") or {}
        name = wf.get("workflow_name") or "workflow da hoc"
        return f"Da chay xong workflow '{name}' cho lenh: {task}"
    return text


def should_replay_workflow(match: dict, explicit: bool = False) -> tuple[bool, str]:
    wf = match.get("workflow") or {}
    confidence = float(match.get("confidence") or workflow_health(wf))
    score = float(match.get("score") or 0)
    stats = wf.get("stats") or {}
    fail_count = int(stats.get("fail_count") or 0)
    success_count = int(stats.get("success_count") or 0)
    if not explicit:
        if confidence < WORKFLOW_REPLAY_MIN_CONFIDENCE:
            return False, f"confidence thấp ({confidence:.2f})"
        if fail_count >= 3 and success_count == 0:
            return False, "workflow lỗi nhiều lần liên tiếp"
        if score < WORKFLOW_FUZZY_THRESHOLD and match.get("match_type") != "template_exact":
            return False, f"độ khớp thấp ({score:.2f})"
    blocking_issues = {
        "workflow_has_opaque_var_names",
        "credential_workflow_must_use_fill_login_form",
        "password_must_not_use_raw_input_text_index",
        "username_password_values_must_differ",
    }
    issues = set(((wf.get("metadata") or {}).get("validation_issues") or []) + workflow_validation_issues(wf))
    if issues & blocking_issues:
        return False, "workflow needs structural repair before replay"
    quality_issues = set(((wf.get("metadata") or {}).get("replay_quality_issues") or []) + workflow_replay_quality_issues(wf))
    blocking_quality = {
        "workflow_lacks_slots_for_long_brittle_replay",
        "workflow_too_index_heavy_for_replay",
    }
    if quality_issues & blocking_quality:
        return False, "workflow lacks reliable semantic checkpoints for safe replay"
    return True, "ok"


def replay_capability_for(action_name: str) -> str:
    if action_name in {"go_to_url", "open_tab", "search_google"}:
        return "navigate"
    if action_name in {"input_text", "fill_login_form"}:
        return "fill"
    if action_name in {"click_element", "click_element_by_index", "click_element_by_text"}:
        return "activate"
    if action_name in {"send_keys"}:
        return "keyboard"
    if action_name in {"scroll_element", "scroll_down", "scroll_up", "scroll_to_text"}:
        return "scroll"
    if action_name == "extract_content":
        return "extract"
    if action_name == "wait":
        return "wait"
    if action_name == "done":
        return "done"
    return "native"


def is_brittle_replay_action(action_name: str, params: dict) -> bool:
    if action_name in {"click_element", "click_element_by_index"}:
        return True
    if action_name == "input_text" and "index" in params:
        return True
    return False


def should_count_local_repair(result: ReplayStepResult) -> bool:
    """Count only risky recoveries toward the repair budget.

    Semantic resolvers are the normal workflow runtime path. Counting them as
    repairs punishes long forms that legitimately have many fill steps.
    """
    if not result.repaired:
        return False
    if result.resolver in {
        "semantic_fill",
        "semantic_login_form",
        "semantic_text",
        "local_page_snapshot",
        "terminal_verifier",
    }:
        return False
    if str(result.resolver or "").startswith("semantic_") and (result.verification or {}).get("ok"):
        return False
    return True


def redact_replay_params(params: dict) -> dict:
    redacted: dict[str, Any] = {}
    for key, value in (params or {}).items():
        key_text = str(key).lower()
        if any(secret in key_text for secret in ("password", "pass", "secret", "token", "key")):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def build_workflow_replay_plan(match: dict, task: str, current_url: str = "") -> list[dict]:
    actions = list(match.get("actions") or [])
    domain = (match.get("domain") or "").strip()
    current = (current_url or "").lower()
    lookup_like = workflow_is_lookup_like(match, actions)
    plan: list[dict] = []

    if not workflow_has_navigation(actions):
        prefix: list[dict] = []
        if domain and domain != "unknown_domain" and domain not in current:
            prefix = [{"go_to_url": {"url": f"https://{domain}"}}]
        else:
            prefix = infer_initial_actions(task) or []
        for action in prefix:
            action_name, _ = action_name_and_params(action)
            plan.append({
                "action": action,
                "source_index": None,
                "capability": replay_capability_for(action_name),
                "inferred": True,
                "lookup_like": lookup_like,
            })

    for index, action in enumerate(actions, start=1):
        action_name, _ = action_name_and_params(action)
        plan.append({
            "action": action,
            "source_index": index,
            "capability": replay_capability_for(action_name),
            "inferred": False,
            "lookup_like": lookup_like,
        })
    return plan


def runtime_hint_for_step(match: dict, plan_step: dict) -> dict:
    source_index = plan_step.get("source_index")
    if source_index is None:
        return {}
    wf = match.get("workflow") or {}
    metadata = wf.get("metadata") or {}
    runtime = metadata.get("runtime") or {}
    hints = runtime.get("resolver_hints") or {}
    return dict(hints.get(str(source_index)) or {})


def replay_trace_entry(step_index: int, plan_step: dict, result: ReplayStepResult) -> dict:
    action_name, params = action_name_and_params(plan_step.get("action") or {})
    verification = result.verification or {}
    return {
        "step": step_index,
        "source_index": plan_step.get("source_index"),
        "action_name": action_name,
        "capability": plan_step.get("capability") or replay_capability_for(action_name),
        "params": redact_replay_params(params),
        "resolver": result.resolver,
        "ok": result.ok,
        "repaired": result.repaired,
        "skipped": result.skipped,
        "message": shorten_text(result.message or "", 220),
        "error": shorten_text(result.error or "", 300),
        "verification": verification,
    }


async def page_state_snapshot(browser_context: Any, max_chars: int = WORKFLOW_TEXT_SNAPSHOT_CHARS) -> dict:
    page = await browser_context.get_current_page()
    url = ""
    title = ""
    text = ""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        body = page.locator("body")
        text = await body.inner_text(timeout=2500)
    except Exception:
        text = ""
    return {
        "url": url,
        "title": title,
        "text": text[:max_chars],
    }


def workflow_is_lookup_like(match: dict, actions: list[dict]) -> bool:
    wf = match.get("workflow") or {}
    metadata = wf.get("metadata") or {}
    haystack = semantic_prompt(" ".join([
        wf.get("prompt_template") or "",
        wf.get("workflow_name") or "",
        metadata.get("intent") or "",
        metadata.get("description") or "",
        metadata.get("success_criteria") or "",
        replay_done_text(actions),
    ]))
    markers = (
        "tim", "search", "ket qua", "result", "lookup", "tra cuu",
        "find", "filter", "loc",
    )
    return any(marker in haystack for marker in markers)


def lookup_result_context_score(url_text: str, page_text: str) -> float:
    """Estimate whether the page is showing submitted lookup results, not just typed input."""
    score = 0.0
    result_url_markers = (
        "search", "results", "result", "query=", "q=", "search_query=",
        "tim-kiem", "timkiem", "ket-qua",
    )
    result_text_markers = (
        "results", "result", "search results", "ket qua", "ket qua tim kiem",
        "kết quả", "kết quả tìm kiếm", "bai viet", "sản phẩm", "san pham",
    )
    if any(marker in url_text for marker in result_url_markers):
        score += 0.35
    if any(marker in page_text for marker in result_text_markers):
        score += 0.25
    return min(score, 0.6)


async def verify_workflow_terminal_state(match: dict, actions: list[dict], browser_context: Any) -> dict:
    variables = match.get("variables") or {}
    if not variables:
        return {"ok": False, "confidence": 0.0, "reason": "no_variables"}

    snapshot = await page_state_snapshot(browser_context, max_chars=5000)
    url_text = semantic_prompt(snapshot.get("url") or "")
    page_text = semantic_prompt(" ".join([
        snapshot.get("title") or "",
        snapshot.get("text") or "",
    ]))

    signals: list[str] = []
    score = 0.0
    lookup_like = workflow_is_lookup_like(match, actions)

    search_query = str(variables.get("search_query") or "").strip()
    if search_query and lookup_like:
        needle = semantic_prompt(search_query)
        if needle and needle in url_text:
            score = max(score, 0.9)
            signals.append("search_query_in_url")
        if needle and needle in page_text:
            context_score = lookup_result_context_score(url_text, page_text)
            page_score = 0.55 + context_score
            score = max(score, min(page_score, 0.82))
            signals.append("search_query_on_page")
            if context_score:
                signals.append("lookup_result_context")
        if score >= 0.72:
            return {
                "ok": True,
                "confidence": score,
                "reason": ",".join(signals),
                "url": snapshot.get("url") or "",
            }

    target_url = str(variables.get("target_url") or "").strip()
    if target_url:
        target_host = (urlparse(target_url if "://" in target_url else f"https://{target_url}").netloc or target_url).lower()
        target_host = target_host.removeprefix("www.")
        current_host = (urlparse(snapshot.get("url") or "").netloc or "").lower().removeprefix("www.")
        if target_host and current_host and target_host in current_host:
            return {
                "ok": True,
                "confidence": 0.82,
                "reason": "target_url_domain_matches",
                "url": snapshot.get("url") or "",
            }

    return {
        "ok": False,
        "confidence": score,
        "reason": ",".join(signals) or "terminal_state_not_verified",
        "url": snapshot.get("url") or "",
    }


async def first_visible_locator_from_selectors(page: Any, selectors: list[str]) -> tuple[Any | None, str]:
    for selector in selectors:
        if not selector:
            continue
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for i in range(min(count, 16)):
            target = locator.nth(i)
            try:
                if await target.is_visible():
                    return target, selector
            except Exception:
                continue
    return None, ""


async def first_visible_empty_locator_from_selectors(page: Any, selectors: list[str]) -> tuple[Any | None, str]:
    fallback = None
    fallback_selector = ""
    for selector in selectors:
        if not selector:
            continue
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for i in range(min(count, 18)):
            target = locator.nth(i)
            try:
                if not await target.is_visible():
                    continue
                if fallback is None:
                    fallback = target
                    fallback_selector = selector
                value = await target.evaluate(
                    """el => String(el.value ?? el.textContent ?? '').trim()"""
                )
                if not value:
                    return target, selector
            except Exception:
                continue
    return fallback, fallback_selector


async def fill_locator_text(page: Any, locator: Any, text: str) -> None:
    try:
        await locator.fill(text, timeout=3000)
        return
    except Exception:
        pass
    await locator.click(timeout=2500)
    await page.keyboard.press("Control+A")
    await page.keyboard.type(text)


async def verify_text_in_editable(page: Any, text: str) -> bool:
    if not text:
        return False
    try:
        return bool(await page.evaluate(
            """value => {
                const wanted = String(value || '').toLowerCase();
                const nodes = Array.from(document.querySelectorAll(
                    'input, textarea, [contenteditable="true"], [role="textbox"], [role="searchbox"]'
                ));
                return nodes.some((el) => {
                    const raw = el.value ?? el.textContent ?? '';
                    return String(raw).toLowerCase().includes(wanted);
                });
            }""",
            text,
        ))
    except Exception:
        return False


async def verify_fill_target_state(page: Any, text: str, *, lookup_like: bool) -> dict:
    kind = classify_fill_value(text)
    try:
        current_url = (page.url or "").lower()
    except Exception:
        current_url = ""
    auth_context = any(marker in current_url for marker in (
        "signup", "sign-up", "register", "login", "signin", "sign-in", "account",
    ))
    if auth_context or kind in {"username", "password", "email"}:
        lookup_like = False
    selectors = selectors_for_fill_kind(kind, lookup_like=lookup_like)
    for selector in selectors:
        if not selector:
            continue
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for i in range(min(count, 18)):
            target = locator.nth(i)
            try:
                if not await target.is_visible():
                    continue
                value = await target.evaluate(
                    """el => String(el.value ?? el.textContent ?? '').trim()"""
                )
                if str(text).lower() in str(value).lower():
                    return {
                        "ok": True,
                        "confidence": 0.86,
                        "kind": kind,
                        "selector": selector,
                        "reason": "value_in_expected_fill_target",
                    }
            except Exception:
                continue
    return {
        "ok": False,
        "confidence": 0.0,
        "kind": kind,
        "reason": "value_not_found_in_expected_fill_target",
    }


def classify_fill_value(text: str) -> str:
    value = str(text or "").strip()
    lower = value.lower()
    if "@" in value and re.search(r"@[a-z0-9.-]+\.[a-z]{2,}", lower):
        return "email"
    if re.fullmatch(r"[A-Za-z0-9_.-]{4,}", value) and any(
        marker in lower for marker in ("user", "account", "admin", "test")
    ):
        return "username"
    if "_" in value and re.fullmatch(r"[A-Za-z0-9_.-]{4,}", value):
        return "username"
    if (
        len(value) >= 6
        and re.search(r"[A-Za-z]", value)
        and re.search(r"\d", value)
        and re.search(r"[!@#$%^&*()+={}[\]:;\"'<>?,/\\|`~]", value)
    ):
        return "password"
    if re.fullmatch(r"[0-9+().-]{7,}", value):
        return "username"
    return "text"


def selectors_for_fill_kind(kind: str, *, lookup_like: bool) -> list[str]:
    common_text = [
        'input[autocomplete="username"]',
        'input[name*="user" i]',
        'input[name*="login" i]',
        'input[name*="account" i]',
        'input[name*="name" i]',
        'input[placeholder*="username" i]',
        'input[placeholder*="user" i]',
        'input[placeholder*="tài khoản" i]',
        'input[placeholder*="tai khoan" i]',
        'input[placeholder*="tên đăng nhập" i]',
        'input[placeholder*="ten dang nhap" i]',
        'input[type="text"]:not([role="searchbox"]):not([type="search"])',
        'textarea',
        '[role="textbox"]:not([role="searchbox"])',
        '[contenteditable="true"]',
        'input:not([type="hidden"]):not([type="password"]):not([type="search"])',
    ]
    if kind == "password":
        return [
            'input[type="password"]',
            'input[autocomplete="current-password"]',
            'input[autocomplete="new-password"]',
            'input[name*="pass" i]',
            'input[placeholder*="password" i]',
            'input[placeholder*="mật khẩu" i]',
            'input[placeholder*="mat khau" i]',
        ]
    if kind == "email":
        return [
            'input[type="email"]',
            'input[autocomplete="email"]',
            'input[name*="email" i]',
            'input[placeholder*="email" i]',
            'input[aria-label*="email" i]',
        ] + common_text
    if lookup_like:
        return [
            'input[type="search"]',
            '[role="searchbox"]',
            'input[aria-label*="search" i]',
            'input[placeholder*="search" i]',
            'input[placeholder*="tim" i]',
            'input[placeholder*="tìm" i]',
        ] + common_text
    return common_text


async def execute_native_replay_action(
    action_name: str,
    params: dict,
    browser_context: Any,
    *,
    page_llm: Any = None,
    resolver: str = "native",
) -> ReplayStepResult:
    try:
        result = await controller.registry.execute_action(
            action_name,
            params,
            browser=browser_context,
            page_extraction_llm=page_llm,
        )
    except Exception as exc:
        return ReplayStepResult(
            ok=False,
            action_name=action_name,
            resolver=resolver,
            error=str(exc),
        )

    if getattr(result, "error", None):
        return ReplayStepResult(
            ok=False,
            action_name=action_name,
            resolver=resolver,
            error=str(result.error),
        )

    message = str(getattr(result, "extracted_content", "") or f"Executed {action_name}")
    return ReplayStepResult(
        ok=True,
        action_name=action_name,
        resolver=resolver,
        message=message,
        done=bool(getattr(result, "is_done", False)),
        final_text=message if getattr(result, "is_done", False) else "",
    )


async def replay_fill_capability(
    action_name: str,
    params: dict,
    browser_context: Any,
    *,
    hint: dict | None = None,
    lookup_like: bool = False,
) -> ReplayStepResult:
    if action_name == "fill_login_form":
        return await execute_native_replay_action(
            action_name,
            params,
            browser_context,
            resolver="semantic_login_form",
        )

    text = str(params.get("text") or "")
    if not text:
        return ReplayStepResult(False, action_name, "semantic_fill", error="missing_text")

    page = await browser_context.get_current_page()
    hint = hint or {}
    preferred_selector = str(hint.get("preferred_selector") or "")
    kind = classify_fill_value(text)
    try:
        current_url = (page.url or "").lower()
    except Exception:
        current_url = ""
    auth_context = any(marker in current_url for marker in (
        "signup", "sign-up", "register", "login", "signin", "sign-in", "account",
    ))
    if auth_context or kind in {"username", "password", "email"}:
        lookup_like = False
    selectors = [preferred_selector] + selectors_for_fill_kind(kind, lookup_like=lookup_like)
    target, selector = await first_visible_empty_locator_from_selectors(page, selectors)
    if target is not None:
        try:
            await fill_locator_text(page, target, text)
            verified = await verify_text_in_editable(page, text)
            return ReplayStepResult(
                ok=True,
                action_name=action_name,
                resolver="semantic_fill",
                message=f"Filled visible editable via {selector}",
                repaired="index" in params,
                verification={"ok": verified, "kind": "editable_contains_text"},
            )
        except Exception:
            pass

    native = await execute_native_replay_action(
        action_name,
        params,
        browser_context,
        resolver="legacy_index_fill",
    )
    if native.ok:
        try:
            page = await browser_context.get_current_page()
            native.verification = {
                "ok": await verify_text_in_editable(page, text),
                "kind": "editable_contains_text",
            }
        except Exception:
            native.verification = {"ok": True, "kind": "native_result"}
    return native


async def click_locator(locator: Any) -> bool:
    try:
        count = await locator.count()
    except Exception:
        count = 0
    for i in range(min(count, 12)):
        target = locator.nth(i)
        try:
            if await target.is_visible():
                await target.click(timeout=3500)
                return True
        except Exception:
            continue
    try:
        await locator.first.click(timeout=2500)
        return True
    except Exception:
        return False


async def replay_click_by_text(page: Any, text: str) -> ReplayStepResult:
    if not text:
        return ReplayStepResult(False, "click_element_by_text", "semantic_text", error="missing_text")
    locators = []
    for role in ("button", "link", "menuitem", "tab", "option"):
        try:
            locators.append(page.get_by_role(role, name=text, exact=True))
            locators.append(page.get_by_role(role, name=text))
        except Exception:
            pass
    try:
        locators.append(page.get_by_text(text, exact=True))
        locators.append(page.get_by_text(text))
    except Exception:
        pass
    for locator in locators:
        if await click_locator(locator):
            return ReplayStepResult(
                ok=True,
                action_name="click_element_by_text",
                resolver="semantic_text",
                message=f"Clicked visible element by text: {text}",
            )
    return ReplayStepResult(False, "click_element_by_text", "semantic_text", error=f"text_not_found: {text}")


async def replay_activate_capability(
    action_name: str,
    params: dict,
    browser_context: Any,
    *,
    after_text_input: bool = False,
    hint: dict | None = None,
) -> ReplayStepResult:
    page = await browser_context.get_current_page()
    hint = hint or {}
    preferred = str(hint.get("preferred_resolver") or "")

    if preferred == "keyboard_submit" and after_text_input:
        try:
            await page.keyboard.press("Enter")
            return ReplayStepResult(
                ok=True,
                action_name=action_name,
                resolver="keyboard_submit",
                message="Submitted current editable with Enter",
                repaired=True,
            )
        except Exception as exc:
            return ReplayStepResult(False, action_name, "keyboard_submit", error=str(exc))

    text = normalize_text(str(params.get("text") or ""))
    if text:
        clicked = await replay_click_by_text(page, text)
        clicked.action_name = action_name
        if clicked.ok:
            return clicked

    native = await execute_native_replay_action(
        action_name,
        params,
        browser_context,
        resolver="legacy_index_activate" if "index" in params else "native",
    )
    if native.ok:
        return native

    if after_text_input:
        try:
            await page.keyboard.press("Enter")
            return ReplayStepResult(
                ok=True,
                action_name=action_name,
                resolver="keyboard_submit",
                message="Submitted current editable with Enter",
                repaired=True,
            )
        except Exception:
            pass

        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button[aria-label*="search" i]',
            'button[aria-label*="submit" i]',
            '[role="button"][aria-label*="search" i]',
            '[role="button"][aria-label*="submit" i]',
        ]
        target, selector = await first_visible_locator_from_selectors(page, submit_selectors)
        if target is not None:
            try:
                await target.click(timeout=3500)
                return ReplayStepResult(
                    ok=True,
                    action_name=action_name,
                    resolver="semantic_submit_control",
                    message=f"Clicked semantic submit control via {selector}",
                    repaired=True,
                )
            except Exception:
                pass

    return native


async def replay_extract_capability(action_name: str, params: dict, browser_context: Any) -> ReplayStepResult:
    try:
        snapshot = await page_state_snapshot(browser_context, max_chars=WORKFLOW_TEXT_SNAPSHOT_CHARS)
        text = normalize_text(snapshot.get("text") or "")
        if text:
            return ReplayStepResult(
                ok=True,
                action_name=action_name,
                resolver="local_page_snapshot",
                message=shorten_text(text, 600),
                verification={"ok": True, "kind": "body_text"},
            )
    except Exception as exc:
        return ReplayStepResult(False, action_name, "local_page_snapshot", error=str(exc))
    return ReplayStepResult(False, action_name, "local_page_snapshot", error="empty_page_text")


async def replay_scroll_fallback(action_name: str, params: dict, browser_context: Any) -> ReplayStepResult:
    page = await browser_context.get_current_page()
    amount = int(params.get("amount") or 500)
    try:
        if action_name == "scroll_up":
            await page.mouse.wheel(0, -abs(amount))
        else:
            await page.mouse.wheel(0, abs(amount))
        return ReplayStepResult(
            ok=True,
            action_name=action_name,
            resolver="page_scroll_fallback",
            message="Scrolled page with mouse wheel",
            repaired=True,
        )
    except Exception as exc:
        return ReplayStepResult(False, action_name, "page_scroll_fallback", error=str(exc))


async def execute_semantic_replay_step(
    plan_step: dict,
    browser_context: Any,
    *,
    after_text_input: bool = False,
    hint: dict | None = None,
) -> ReplayStepResult:
    action = plan_step.get("action") or {}
    action_name, params = action_name_and_params(action)
    capability = plan_step.get("capability") or replay_capability_for(action_name)

    if capability == "done":
        return ReplayStepResult(
            ok=True,
            action_name=action_name,
            resolver="done",
            done=True,
            final_text=str(params.get("text") or "Hoan thanh workflow da hoc."),
            message="Workflow done action reached",
        )
    if capability == "fill":
        return await replay_fill_capability(
            action_name,
            params,
            browser_context,
            hint=hint,
            lookup_like=bool(plan_step.get("lookup_like")),
        )
    if capability == "activate":
        return await replay_activate_capability(
            action_name,
            params,
            browser_context,
            after_text_input=after_text_input,
            hint=hint,
        )
    if capability == "extract":
        return await replay_extract_capability(action_name, params, browser_context)

    native = await execute_native_replay_action(action_name, params, browser_context)
    if native.ok:
        return native
    if capability == "scroll":
        return await replay_scroll_fallback(action_name, params, browser_context)
    return native


async def verify_recovery_checkpoint(
    match: dict,
    replay_actions: list[dict],
    plan_step: dict,
    browser_context: Any,
) -> dict:
    action = plan_step.get("action") or {}
    action_name, params = action_name_and_params(action)
    capability = plan_step.get("capability") or replay_capability_for(action_name)

    if capability == "fill":
        page = await browser_context.get_current_page()
        return await verify_fill_target_state(
            page,
            str(params.get("text") or ""),
            lookup_like=bool(plan_step.get("lookup_like")),
        )

    if capability == "activate" and bool(plan_step.get("lookup_like")):
        terminal = await verify_workflow_terminal_state(match, replay_actions, browser_context)
        if terminal.get("ok"):
            return terminal
        return {
            **terminal,
            "ok": False,
            "reason": terminal.get("reason") or "lookup_activation_not_verified",
        }

    if capability == "navigate":
        action_url = str(params.get("url") or "")
        try:
            page = await browser_context.get_current_page()
            current_url = page.url or ""
        except Exception:
            current_url = ""
        if action_url and action_url.rstrip("/") in current_url:
            return {
                "ok": True,
                "confidence": 0.82,
                "reason": "navigation_url_matches",
                "url": current_url,
            }

    return {
        "ok": True,
        "confidence": 0.55,
        "reason": "ai_recovery_completed_resume_with_next_step",
    }


def can_attempt_ai_recovery(match: dict, plan_step: dict) -> tuple[bool, str]:
    action = plan_step.get("action") or {}
    action_name, params = action_name_and_params(action)
    capability = plan_step.get("capability") or replay_capability_for(action_name)
    if capability in {"fill", "navigate"}:
        return True, "checkpoint_verifiable"
    if capability == "activate":
        if str(params.get("text") or "").strip():
            return True, "text_activation_can_be_rechecked_by_next_step"
        if bool(plan_step.get("lookup_like")) and (match.get("variables") or {}).get("search_query"):
            return True, "lookup_terminal_checkpoint"
        return False, "activation_step_has_no_verifiable_checkpoint"
    if capability in {"scroll", "wait", "extract", "keyboard"}:
        return True, "soft_checkpoint"
    return False, "no_verifiable_recovery_checkpoint"


def describe_plan_action(plan_step: dict) -> str:
    action_name, params = action_name_and_params(plan_step.get("action") or {})
    return json.dumps(
        {
            "action_name": action_name,
            "capability": plan_step.get("capability") or replay_capability_for(action_name),
            "params": redact_replay_params(params),
            "source_index": plan_step.get("source_index"),
        },
        ensure_ascii=False,
    )


async def attempt_ai_workflow_recovery(
    *,
    match: dict,
    replay_actions: list[dict],
    plan_step: dict,
    browser_context: Any,
    user_task: str,
    failure_reason: str,
    llm: Any,
    max_steps: int = WORKFLOW_AI_RECOVERY_MAX_STEPS,
) -> WorkflowRecoveryResult:
    if llm is None:
        return WorkflowRecoveryResult(ok=False, error="missing_recovery_llm")

    workflow = match.get("workflow") or {}
    wf_name = workflow.get("workflow_name") or "workflow"
    action_name, _ = action_name_and_params(plan_step.get("action") or {})
    next_index = plan_step.get("source_index")
    recovery_task = (
        "You are a workflow exception handler, not the main task agent.\n"
        "Recover ONLY the failed workflow step below, then stop immediately.\n"
        "Do not continue the full user task. Do not solve later workflow steps.\n"
        "Prefer the smallest stable browser action: fill the correct field, click the correct control, press Enter, wait, or navigate back to the expected page.\n"
        "When the failed step is restored, call done with a short RECOVERED message.\n\n"
        f"User task: {user_task}\n"
        f"Workflow: {wf_name}\n"
        f"Failed step: {describe_plan_action(plan_step)}\n"
        f"Failure reason: {failure_reason[:600]}\n"
        f"Resume target: restore the browser state so workflow can continue after this failed step"
        + (f" (source step {next_index})." if next_index else ".")
    )

    try:
        recovery_agent = Agent(
            task=recovery_task,
            llm=llm,
            planner_llm=None,
            max_actions_per_step=min(MAX_ACTIONS_PER_STEP, 4),
            max_input_tokens=min(MAX_INPUT_TOKENS, 60_000),
            max_failures=2,
            use_vision=False,
            enable_memory=False,
            browser_context=browser_context,
            controller=controller,
        )
        result = await recovery_agent.run(max_steps=max_steps)
        final_text = ""
        try:
            final_text = result.final_result() or ""
        except Exception:
            final_text = ""
    except Exception as exc:
        return WorkflowRecoveryResult(ok=False, error=str(exc))

    verification = await verify_recovery_checkpoint(
        match,
        replay_actions,
        plan_step,
        browser_context,
    )
    if verification.get("ok"):
        return WorkflowRecoveryResult(
            ok=True,
            message=f"AI recovered failed step {action_name}",
            verification=verification,
            final_text=final_text,
        )
    return WorkflowRecoveryResult(
        ok=False,
        error=f"AI recovery did not restore checkpoint: {verification.get('reason')}",
        verification=verification,
        final_text=final_text,
    )


def persist_workflow_runtime_learning(
    domain: str,
    workflow_id: str | None,
    *,
    trace: list[dict],
    success: bool,
    failure_reason: str = "",
) -> None:
    if not workflow_id:
        return
    profiles = load_site_profiles()
    workflows = profiles.get(domain) or []
    now = datetime.now().isoformat()
    for wf in workflows:
        if wf.get("workflow_id") != workflow_id:
            continue
        metadata = dict(wf.get("metadata") or {})
        metadata, _ = runtime_metadata_defaults(metadata, wf.get("steps") or [])
        runtime = dict(metadata.get("runtime") or {})
        runtime["runtime_version"] = WORKFLOW_RUNTIME_VERSION
        runtime["last_replay_trace"] = trace[-40:]
        runtime["last_replay_success"] = bool(success)
        runtime["last_failure_reason"] = (failure_reason or "")[:500]
        runtime["last_replay_at"] = now

        resolver_stats = dict(runtime.get("resolver_stats") or {})
        resolver_hints = dict(runtime.get("resolver_hints") or {})
        if success:
            for entry in trace:
                if not entry.get("ok"):
                    continue
                action_name = entry.get("action_name") or ""
                resolver = entry.get("resolver") or ""
                if action_name and resolver:
                    key = f"{action_name}:{resolver}"
                    resolver_stats[key] = int(resolver_stats.get(key) or 0) + 1
                source_index = entry.get("source_index")
                should_hint = (
                    source_index is not None
                    and resolver
                    and resolver != "ai_recover_only"
                    and (
                        entry.get("repaired")
                        or entry.get("skipped")
                        or resolver.startswith("semantic")
                        or resolver in {"keyboard_submit", "local_page_snapshot"}
                    )
                )
                if should_hint:
                    hint = {
                        "preferred_resolver": resolver,
                        "updated_at": now,
                        "success_count": int((resolver_hints.get(str(source_index)) or {}).get("success_count") or 0) + 1,
                    }
                    resolver_hints[str(source_index)] = hint

        runtime["resolver_stats"] = resolver_stats
        runtime["resolver_hints"] = resolver_hints
        runtime["brittle_actions"] = workflow_brittle_actions(wf.get("steps") or [])
        metadata["runtime"] = runtime
        wf["metadata"] = metadata
        save_site_profiles(profiles)
        return


def update_workflow_stats(
    domain: str,
    workflow_id: str | None,
    *,
    success: bool,
    tokens: int = 0,
    error: str = "",
    example: str = "",
) -> None:
    if not workflow_id:
        return
    profiles = load_site_profiles()
    workflows = profiles.get(domain) or []
    now = datetime.now().isoformat()
    for wf in workflows:
        if wf.get("workflow_id") != workflow_id:
            continue
        stats = dict(wf.get("stats") or {})
        stats["replay_count"] = int(stats.get("replay_count") or 0) + 1
        if success:
            stats["success_count"] = int(stats.get("success_count") or 0) + 1
            stats["last_success_at"] = now
            old_avg = float(stats.get("avg_replay_tokens") or 0)
            count = int(stats.get("success_count") or 1)
            stats["avg_replay_tokens"] = round(((old_avg * max(count - 1, 0)) + tokens) / max(count, 1), 2)
            wf["confidence"] = min(0.98, float(wf.get("confidence", 0.62) or 0.62) + 0.04)
            if example:
                metadata = dict(wf.get("metadata") or {})
                examples = list(metadata.get("examples") or [])
                if example not in examples:
                    examples.append(example)
                    metadata["examples"] = examples[-20:]
                    wf["metadata"] = metadata
        else:
            stats["fail_count"] = int(stats.get("fail_count") or 0) + 1
            stats["fallback_count"] = int(stats.get("fallback_count") or 0) + 1
            stats["last_failure_at"] = now
            stats["last_error"] = error[:500]
            wf["confidence"] = max(0.08, float(wf.get("confidence", 0.62) or 0.62) - 0.12)
            if error:
                metadata = dict(wf.get("metadata") or {})
                failures = list(metadata.get("failure_patterns") or [])
                short_error = error[:220]
                if short_error not in failures:
                    failures.append(short_error)
                    metadata["failure_patterns"] = failures[-12:]
                    wf["metadata"] = metadata
        wf["stats"] = stats
        save_site_profiles(profiles)
        return


def auto_repair_workflow_from_history(
    failed_match: dict | None,
    *,
    original_task: str,
    history_list: list,
    final_text: str,
    tokens: int = 0,
) -> dict | None:
    if not failed_match or not history_list:
        return None
    domain = failed_match.get("domain") or ""
    old_wf = failed_match.get("workflow") or {}
    workflow_id = old_wf.get("workflow_id")
    if not domain or not workflow_id:
        return None

    generalized = generalize_history(original_task, history_list)
    new_steps = generalized.get("steps") or []
    if not new_steps:
        return None
    validation_issues = workflow_validation_issues(generalized)
    blocking_issues = {
        "workflow_has_opaque_var_names",
        "credential_workflow_must_use_fill_login_form",
        "password_must_not_use_raw_input_text_index",
        "username_password_values_must_differ",
    }
    if set(validation_issues) & blocking_issues:
        update_workflow_stats(
            domain,
            workflow_id,
            success=False,
            error=f"Auto-repair rejected invalid workflow: {', '.join(validation_issues)}",
            example=original_task,
        )
        return {
            "workflow_id": workflow_id,
            "workflow_name": old_wf.get("workflow_name"),
            "changed": False,
            "rejected": True,
            "validation_issues": validation_issues,
        }

    profiles = load_site_profiles()
    workflows = profiles.get(domain) or []
    now = datetime.now().isoformat()
    for wf in workflows:
        if wf.get("workflow_id") != workflow_id:
            continue

        previous_steps = wf.get("steps") or []
        if previous_steps == new_steps:
            stats = dict(wf.get("stats") or {})
            stats["success_count"] = int(stats.get("success_count") or 0) + 1
            stats["repair_count"] = int(stats.get("repair_count") or 0) + 1
            stats["last_success_at"] = now
            stats["last_repaired_at"] = now
            stats["last_repair_tokens"] = tokens
            wf["stats"] = stats
            wf["confidence"] = min(0.9, float(wf.get("confidence", 0.62) or 0.62) + 0.08)
            save_site_profiles(profiles)
            return {
                "workflow_id": workflow_id,
                "workflow_name": wf.get("workflow_name"),
                "changed": False,
                "reason": "fallback history matched existing workflow",
            }

        metadata = dict(wf.get("metadata") or {})
        repair_log = list(metadata.get("repair_log") or [])
        backups = list(metadata.get("previous_versions") or [])
        backups.append({
            "version": int(wf.get("version") or 1),
            "replaced_at": now,
            "failure": failed_match.get("last_error") or (wf.get("stats") or {}).get("last_error", ""),
            "steps": previous_steps,
        })
        metadata["previous_versions"] = backups[-5:]
        repair_log.append({
            "repaired_at": now,
            "source": "agent_fallback_success",
            "task": original_task,
            "final_text": final_text[:500],
            "old_action_names": workflow_action_names(previous_steps),
            "new_action_names": workflow_action_names(new_steps),
        })
        metadata["repair_log"] = repair_log[-12:]
        metadata["examples"] = list(dict.fromkeys((metadata.get("examples") or []) + [original_task]))[-20:]
        metadata["action_names"] = workflow_action_names(new_steps)
        metadata["input_schema"] = generalized.get("metadata", {}).get("input_schema") or [variable_schema_for(v) for v in generalized.get("variables") or []]
        metadata["validation_issues"] = validation_issues
        if generalized.get("metadata"):
            metadata["intent"] = metadata.get("intent") or generalized["metadata"].get("intent")
            metadata["description"] = generalized["metadata"].get("description") or metadata.get("description")

        stats = dict(wf.get("stats") or {})
        stats["success_count"] = int(stats.get("success_count") or 0) + 1
        stats["last_success_at"] = now
        stats["last_repaired_at"] = now
        stats["repair_count"] = int(stats.get("repair_count") or 0) + 1
        stats["last_repair_tokens"] = tokens

        wf["steps"] = new_steps
        wf["variables"] = generalized.get("variables") or wf.get("variables") or []
        wf["prompt_template"] = generalized.get("prompt_template") or wf.get("prompt_template")
        wf["metadata"] = metadata
        wf["stats"] = stats
        wf["version"] = int(wf.get("version") or 1) + 1
        wf["confidence"] = min(0.9, max(float(wf.get("confidence", 0.62) or 0.62), 0.62) + 0.12)
        save_site_profiles(profiles)
        return {
            "workflow_id": workflow_id,
            "workflow_name": wf.get("workflow_name"),
            "changed": True,
            "version": wf["version"],
            "old_steps": len(previous_steps),
            "new_steps": len(new_steps),
        }
    return None


async def repair_replay_action(action_name: str, params: dict, browser_context: Any, *, after_text_input: bool = False) -> str | None:
    """Best-effort generic repairs for brittle learned index actions."""
    page = await browser_context.get_current_page()

    if action_name == "input_text":
        text = str(params.get("text") or "")
        if not text:
            return None
        selectors = [
            'input[type="search"]',
            'textarea',
            'input:not([type="hidden"])',
            '[contenteditable="true"]',
            '[role="textbox"]',
        ]
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            for i in range(min(count, 12)):
                target = locator.nth(i)
                try:
                    if await target.is_visible():
                        await target.fill(text)
                        return f"Repaired input_text by filling visible selector {selector}"
                except Exception:
                    continue

    if action_name == "click_element_by_index" and after_text_input:
        try:
            await page.keyboard.press("Enter")
            return "Repaired click after text input by pressing Enter"
        except Exception:
            return None

    if action_name == "click_element_by_text":
        text = normalize_text(str(params.get("text") or ""))
        if not text:
            return None
        locators = [
            page.get_by_text(text, exact=True),
            page.get_by_text(text),
            page.locator(f"text={text}"),
        ]
        for locator in locators:
            try:
                count = await locator.count()
                for i in range(min(count, 10)):
                    target = locator.nth(i)
                    if await target.is_visible():
                        await target.click()
                        return f"Repaired click_element_by_text by visible text: {text}"
            except Exception:
                continue

    if action_name in {"scroll_element", "scroll_down"}:
        amount = int(params.get("amount") or 500)
        await page.mouse.wheel(0, abs(amount))
        return "Repaired scroll by scrolling the page"

    if action_name == "scroll_up":
        amount = int(params.get("amount") or 500)
        await page.mouse.wheel(0, -abs(amount))
        return "Repaired scroll_up by scrolling the page"

    return None


async def wait_for_dom_stability(page: Any, timeout_ms: int = 8000, stability_ms: int = 350) -> None:
    """Wait until there are no mutations in the DOM for stability_ms, up to timeout_ms."""
    js_code = f"""
    new Promise((resolve) => {{
        let timeoutId;
        const observer = new MutationObserver(() => {{
            clearTimeout(timeoutId);
            timeoutId = setTimeout(() => {{
                observer.disconnect();
                resolve(true);
            }}, {stability_ms});
        }});
        observer.observe(document.body, {{
            childList: true,
            subtree: true,
            attributes: true
        }});
        timeoutId = setTimeout(() => {{
            observer.disconnect();
            resolve(true);
        }}, {stability_ms});

        // Safety timeout
        setTimeout(() => {{
            observer.disconnect();
            resolve(false);
        }}, {timeout_ms});
    }})
    """
    try:
        await page.evaluate(js_code)
    except Exception:
        pass


async def wait_for_replay_settle(action_name: str, browser_context: Any) -> None:
    """Let dynamic/SPAs settle between deterministic replay actions."""
    if action_name not in {
        "go_to_url",
        "search_google",
        "open_tab",
        "click_element_by_index",
        "click_element",
        "click_element_by_text",
        "input_text",
        "fill_login_form",
        "scroll_element",
        "scroll_down",
        "scroll_up",
        "scroll_to_text",
    }:
        return
    try:
        page = await browser_context.get_current_page()
        if action_name in {"go_to_url", "search_google", "open_tab", "click_element_by_index", "click_element", "click_element_by_text"}:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=2500)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass

        # Wait for DOM structure to settle (up to 6s, 350ms quiet window)
        if action_name in {
            "go_to_url", "search_google", "open_tab",
            "click_element_by_index", "click_element", "click_element_by_text",
            "input_text", "fill_login_form"
        }:
            await wait_for_dom_stability(page, timeout_ms=6000, stability_ms=350)

        await page.wait_for_timeout(450 if action_name.startswith("scroll") else 700)
    except Exception:
        await asyncio.sleep(0.45)


def extract_clicked_label(extracted_content: str) -> str | None:
    if not extracted_content:
        return None
    match = re.search(r"Clicked .* with index \d+:\s*(.+)$", extracted_content)
    if match:
        label = match.group(1).strip()
        label = re.sub(r"\s*[▾▴\s\-\,\_\|]+$", "", label).strip()
        return label
    return None


def get_domain_from_history(history_list) -> str:
    for h in history_list:
        model_output = getattr(h, "model_output", None)
        if not model_output:
            continue
        for action in getattr(model_output, "action", []) or []:
            action_dict = action.model_dump(exclude_none=True)
            if "go_to_url" in action_dict and "url" in action_dict["go_to_url"]:
                url = action_dict["go_to_url"]["url"]
                parsed = urlparse(url)
                domain = parsed.netloc or parsed.path
                if ":" in domain:
                    domain = domain.split(":")[0]
                return domain
    return "unknown_domain"


def normalize_url_for_cycle_detection(url: str) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return f"{netloc}{path}".lower()
    except Exception:
        return url.lower()


def prune_history(history_list: list) -> list:
    if not history_list:
        return []

    # 1. Filter out steps where any ActionResult has an error
    clean_history = []
    for h in history_list:
        results = getattr(h, "result", []) or []
        has_error = any(getattr(r, "error", None) is not None for r in results)
        if not has_error:
            clean_history.append(h)

    # 2. Eliminate cycles based on normalized URL state (backtracking)
    steps = clean_history
    i = 0
    while i < len(steps):
        state = getattr(steps[i], "state", None)
        url = getattr(state, "url", "") if state else ""
        if not url:
            i += 1
            continue

        norm_url = normalize_url_for_cycle_detection(url)

        # Find the last step that returned to the same normalized URL
        last_idx = i
        for j in range(len(steps) - 1, i, -1):
            s_state = getattr(steps[j], "state", None)
            s_url = getattr(s_state, "url", "") if s_state else ""
            if s_url and normalize_url_for_cycle_detection(s_url) == norm_url:
                # Ensure we actually transitioned to a different URL in between
                has_different_url = False
                for k in range(i + 1, j):
                    k_state = getattr(steps[k], "state", None)
                    k_url = getattr(k_state, "url", "") if k_state else ""
                    if k_url and normalize_url_for_cycle_detection(k_url) != norm_url:
                        has_different_url = True
                        break
                if has_different_url:
                    last_idx = j
                    break

        if last_idx > i:
            steps = steps[:i + 1] + steps[last_idx + 1:]
        else:
            i += 1

    return steps


def generalize_history(original_task: str, history_list: list, *, specific: bool = False) -> dict:
    history_list = prune_history(history_list)
    candidate_values = []

    for h in history_list:
        model_output = getattr(h, "model_output", None)
        if not model_output:
            continue
        for action in getattr(model_output, "action", []) or []:
            action_dict = action.model_dump(exclude_none=True)
            for act_name, params in action_dict.items():
                if act_name == "input_text" and "text" in params:
                    val = str(params["text"]).strip()
                    if val and val not in candidate_values:
                        candidate_values.append(val)
                elif act_name == "click_element_by_text" and "text" in params:
                    val = str(params["text"]).strip()
                    if val and val not in candidate_values:
                        candidate_values.append(val)
                elif act_name in {"click_element", "click_element_by_index"} and "index" in params:
                    results = getattr(h, "result", []) or []
                    for r in results:
                        extracted = getattr(r, "extracted_content", "") or ""
                        lbl = extract_clicked_label(extracted)
                        if lbl and lbl not in candidate_values:
                            candidate_values.append(lbl)

    if specific:
        prompt_template = original_task
        var_mapping = {}
        variables_list = []
        input_schema = []
    else:
        prompt_template, var_mapping, variables_list, input_schema = build_variable_mapping(
            original_task,
            candidate_values,
        )

    generalized_steps = []
    for h in history_list:
        model_output = getattr(h, "model_output", None)
        if not model_output:
            continue
        for action in getattr(model_output, "action", []) or []:
            action_dict = action.model_dump(exclude_none=True)
            gen_action = {}
            for act_name, params in action_dict.items():
                new_params = dict(params)
                if act_name == "input_text" and "text" in new_params:
                    new_params = replace_params_with_variables(new_params, var_mapping)
                    gen_action["input_text"] = new_params
                elif act_name in {"click_element", "click_element_by_index"} and "index" in new_params:
                    label = None
                    results = getattr(h, "result", []) or []
                    for r in results:
                        extracted = getattr(r, "extracted_content", "") or ""
                        lbl = extract_clicked_label(extracted)
                        if lbl:
                            label = lbl
                            break

                    converted = False
                    if label:
                        for raw_val, var_name in var_mapping.items():
                            if semantic_prompt(label) == semantic_prompt(raw_val):
                                gen_action["click_element_by_text"] = {"text": f"{{{var_name}}}"}
                                converted = True
                                break
                    if not converted:
                        if label and len(label) <= 120:
                            gen_action["click_element_by_text"] = {"text": label}
                        else:
                            gen_action[act_name] = new_params
                elif act_name == "click_element_by_text" and "text" in new_params:
                    new_params = replace_params_with_variables(new_params, var_mapping)
                    gen_action["click_element_by_text"] = new_params
                else:
                    gen_action[act_name] = replace_params_with_variables(new_params, var_mapping)
            generalized_steps.append(gen_action)

    if not specific:
        generalized_steps = semanticize_workflow_steps(generalized_steps, variables_list)
    validation_issues = workflow_validation_issues({
        "variables": variables_list,
        "steps": generalized_steps,
    })
    workflow_scope = "specific" if specific else "general"

    return {
        "prompt_template": prompt_template,
        "variables": variables_list,
        "steps": generalized_steps,
        "metadata": {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "workflow_scope": workflow_scope,
            "intent": prompt_template,
            "description": f"{workflow_scope.title()} workflow learned from task: {original_task}",
            "inputs": variables_list,
            "input_schema": input_schema,
            "success_criteria": "Complete the learned browser task without action errors.",
            "avoid_when": [],
            "examples": [original_task, prompt_template],
            "action_names": workflow_action_names(generalized_steps),
            "validation_issues": validation_issues,
        },
        "stats": {
            "success_count": 1,
            "fail_count": 0,
            "replay_count": 0,
            "fallback_count": 0,
            "repair_count": 0,
            "avg_replay_tokens": 0,
            "last_success_at": datetime.now().isoformat(),
            "last_failure_at": None,
            "last_repaired_at": None,
            "last_repair_tokens": 0,
            "last_error": "",
        },
        "confidence": 0.48 if validation_issues else 0.72,
    }


# ── Config / pricing helpers ──────────────────────────────────────────────────
def resolve_model(model_id: str) -> str:
    if model_id not in MODEL_IDS:
        raise ValueError(f"Model không hợp lệ: {model_id}")
    return model_id


def build_config_payload() -> dict:
    return {
        "executor_model": MODEL_CONFIG["executor_model"],
        "planner_model":  MODEL_CONFIG["planner_model"],
        "vision_mode":    MODEL_CONFIG.get("vision_mode", VISION_MODE_DEFAULT),
        "models":         MODEL_OPTIONS,
        "max_steps":      MAX_AGENT_STEPS,
        "vision_modes": [
            {
                "id": "auto",
                "label": "Vision tự động",
                "description": "Text/DOM mặc định; bật ảnh khi agent kẹt (tối đa 12 bước).",
            },
            {
                "id": "on",
                "label": "Luôn vision",
                "description": "Mỗi bước gửi ảnh màn hình — tốn token hơn, chính xác hơn trên UI khó.",
            },
            {
                "id": "off",
                "label": "Chỉ text/DOM",
                "description": "Không gửi ảnh; rẻ nhất, phù hợp trang form/link chuẩn.",
            },
        ],
        "pricing_reference": {
            "source":     GEMINI_PRICING_SOURCE,
            "updated_at": GEMINI_PRICING_UPDATED_AT,
        },
    }


def resolve_vision_mode(mode: str | None) -> str:
    clean = (mode or MODEL_CONFIG.get("vision_mode") or VISION_MODE_DEFAULT).strip().lower()
    if clean in VISION_MODES:
        return clean
    return VISION_MODE_DEFAULT


def initial_use_vision(vision_mode: str) -> bool:
    return vision_mode == "on"


def should_enable_vision_auto(
    consecutive_failures: int,
    last_errors: list[str],
    primary_action: str,
    recent_actions: list[str],
) -> bool:
    err_text = " ".join(last_errors).lower()
    if consecutive_failures >= 2:
        return True
    if consecutive_failures >= 1 and any(
        needle in err_text
        for needle in (
            "element not found",
            "not found",
            "not visible",
            "not interactable",
            "timeout",
            "khong tim",
            "không tìm",
        )
    ):
        return True
    if (
        len(recent_actions) >= MAX_REPEAT_ACTIONS
        and len(set(recent_actions[-MAX_REPEAT_ACTIONS:])) == 1
        and primary_action in STUCK_ACTIONS
    ):
        return True
    return False


def adjust_vision_for_step_end(
    agent_ref: Any,
    session: dict[str, Any],
    task_state: dict[str, Any],
) -> str | None:
    """Chế độ vision: auto bật ảnh khi kẹt; trả thông báo ngắn nếu đổi mode."""
    mode = session.get("vision_mode", VISION_MODE_DEFAULT)
    settings = getattr(agent_ref, "settings", None)
    if settings is None:
        return None

    if mode == "off":
        settings.use_vision = False
        return None
    if mode == "on":
        settings.use_vision = True
        return None

    cap = int(session.get("max_vision_steps", MAX_VISION_STEPS_PER_TASK))
    if getattr(settings, "use_vision", False):
        session["vision_steps_used"] = int(session.get("vision_steps_used", 0)) + 1
        if session["vision_steps_used"] >= cap:
            settings.use_vision = False
            return "Đã tắt vision (đủ 12 bước) — tiếp tục đọc DOM/text."
        return None

    primary_action = (
        task_state["recent_actions"][-1] if task_state.get("recent_actions") else ""
    )
    failures = get_agent_consecutive_failures(agent_ref)
    errors = extract_agent_errors(agent_ref)
    if int(session.get("vision_steps_used", 0)) < cap and should_enable_vision_auto(
        failures,
        errors,
        primary_action,
        task_state.get("recent_actions", []),
    ):
        settings.use_vision = True
        return "Bật vision tạm thời — trang khó nhận diện qua DOM."
    return None


def get_model_option(model_id: str) -> dict:
    for item in MODEL_OPTIONS:
        if item["id"] == model_id:
            return item
    return {"id": model_id, "label": model_id, "description": ""}


def make_llm(model: str, callback_handler: "UsageMetadataCallbackHandler | None" = None):
    # FIX 3: Pass temperature=0 explicitly for deterministic, faster responses.
    # Gemini 2.5 models default to a sampling temperature that adds latency from
    # exploring multiple token candidates. temperature=0 = greedy decode = faster
    # first-token latency and fully reproducible actions (critical for long tasks).
    callbacks = [callback_handler] if callback_handler else None

    if use_vertex():
        try:
            from langchain_google_vertexai import ChatVertexAI
            from google.oauth2 import service_account
        except ImportError as exc:
            raise RuntimeError(
                "Dang dinh dung Vertex AI nhung chua cai dat langchain-google-vertexai. "
                "Chay: pip install langchain-google-vertexai google-cloud-aiplatform"
            ) from exc

        creds_path = get_vertex_credentials()
        if creds_path:
            credentials = service_account.Credentials.from_service_account_file(creds_path)
        else:
            credentials = None

        location = get_vertex_location()
        kwargs = {}
        if location == "global":
            kwargs["api_endpoint"] = "aiplatform.googleapis.com"

        return ChatVertexAI(
            model=model,
            project=get_vertex_project(),
            location=location,
            credentials=credentials,
            temperature=0,
            callbacks=callbacks,
            **kwargs
        )

    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=GEMINI_API_KEY,
        callbacks=callbacks,
        temperature=0,
    )


def extract_usage_totals(callback_handler: "UsageMetadataCallbackHandler | None") -> dict:
    if not callback_handler:
        return {"resolved_models": [], "input_tokens": 0, "output_tokens": 0,
                "cached_tokens": 0, "total_tokens": 0}

    totals: dict[str, Any] = {
        "resolved_models": [], "input_tokens": 0, "output_tokens": 0,
        "cached_tokens": 0, "total_tokens": 0,
    }
    for model_name, usage in callback_handler.usage_metadata.items():
        details = usage.get("input_token_details") or {}
        cached_tokens = int(details.get("cache_read", 0) or 0)
        totals["resolved_models"].append(model_name)
        totals["input_tokens"]  += int(usage.get("input_tokens", 0)  or 0)
        totals["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        totals["total_tokens"]  += int(usage.get("total_tokens", 0)  or 0)
        totals["cached_tokens"] += cached_tokens
    return totals


def calculate_cost_usd(model_id: str, input_tokens: int, output_tokens: int, cached_tokens: int) -> float:
    pricing = MODEL_PRICING_USD_PER_1M[model_id]
    paid_input = max(input_tokens - cached_tokens, 0)
    cost = (
        paid_input     * pricing["input"]
        + output_tokens * pricing["output"]
        + cached_tokens * pricing["cached"]
    ) / 1_000_000
    return round(cost, 6)


def build_role_usage(
    requested_model: str,
    callback_handler: "UsageMetadataCallbackHandler | None",
    exchange_rate: float,
) -> dict:
    totals = extract_usage_totals(callback_handler)
    cost_usd = calculate_cost_usd(
        requested_model,
        totals["input_tokens"], totals["output_tokens"], totals["cached_tokens"],
    )
    return {
        "requested_model":  requested_model,
        "resolved_models":  totals["resolved_models"],
        "input_tokens":     totals["input_tokens"],
        "output_tokens":    totals["output_tokens"],
        "cached_tokens":    totals["cached_tokens"],
        "total_tokens":     totals["total_tokens"],
        "cost_usd":         cost_usd,
        "cost_vnd":         round(cost_usd * exchange_rate, 2),
    }


def build_usage_summary(
    executor_model: str,
    planner_model: str | None,
    executor_callback: "UsageMetadataCallbackHandler",
    planner_callback: "UsageMetadataCallbackHandler | None",
    exchange_rate_info: dict,
    elapsed_seconds: int,
    step: int,
    waiting_for_user: bool,
) -> dict:
    rate     = float(exchange_rate_info["usd_to_vnd"])
    executor = build_role_usage(executor_model, executor_callback, rate)
    planner  = build_role_usage(planner_model, planner_callback, rate) if planner_model else None

    total_input   = executor["input_tokens"]  + (planner["input_tokens"]  if planner else 0)
    total_output  = executor["output_tokens"] + (planner["output_tokens"] if planner else 0)
    total_cached  = executor["cached_tokens"] + (planner["cached_tokens"] if planner else 0)
    total_tokens  = executor["total_tokens"]  + (planner["total_tokens"]  if planner else 0)
    total_cost_usd = executor["cost_usd"]    + (planner["cost_usd"]  if planner else 0.0)
    total_cost_vnd = executor["cost_vnd"]    + (planner["cost_vnd"]  if planner else 0.0)

    return {
        "executor": executor,
        "planner":  planner,
        "totals": {
            "input_tokens":  total_input,
            "output_tokens": total_output,
            "cached_tokens": total_cached,
            "total_tokens":  total_tokens,
            "cost_usd":      round(total_cost_usd, 6),
            "cost_vnd":      round(total_cost_vnd, 2),
        },
        "exchange_rate": exchange_rate_info,
        "pricing_reference": {
            "source":     GEMINI_PRICING_SOURCE,
            "updated_at": GEMINI_PRICING_UPDATED_AT,
        },
        "elapsed_seconds":  elapsed_seconds,
        "step":             step,
        "waiting_for_user": waiting_for_user,
    }


def fetch_exchange_rate_sync() -> dict:
    with urllib.request.urlopen(EXCHANGE_RATE_URL, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rate    = float(payload["rates"]["VND"])
    now_iso = datetime.now().isoformat()
    return {
        "usd_to_vnd": rate,
        "updated_at": payload.get("time_last_update_utc") or now_iso,
        "source":     EXCHANGE_RATE_URL,
        "stale":      False,
        "fetched_at": now_iso,
    }


async def get_exchange_rate_info() -> dict:
    now        = time.time()
    fetched_at = exchange_rate_cache.get("fetched_at")
    if fetched_at:
        last_fetch_ts = datetime.fromisoformat(fetched_at).timestamp()
        if now - last_fetch_ts <= EXCHANGE_RATE_CACHE_SECONDS:
            return dict(exchange_rate_cache)
    try:
        fresh = await asyncio.to_thread(fetch_exchange_rate_sync)
        exchange_rate_cache.update(fresh)
        return dict(exchange_rate_cache)
    except Exception:
        if exchange_rate_cache.get("updated_at"):
            stale = dict(exchange_rate_cache)
            stale["stale"] = True
            return stale
        return {"usd_to_vnd": DEFAULT_USD_TO_VND, "updated_at": None,
                "source": "fallback", "stale": True, "fetched_at": None}


# ── LLM / context helpers ─────────────────────────────────────────────────────
def should_use_planner(task: str) -> bool:
    # FIX 4: Always use planner for tasks intended to run many hours.
    # The original logic only turned on planner for tasks ≥160 chars or with
    # ≥2 chaining markers. This means a short but complex task like
    # "Monitor the inbox and reply to every customer email for 4 hours"
    # runs WITHOUT a planner and the executor has no strategic overview —
    # it makes greedy step-by-step decisions, gets stuck, and wastes tokens
    # on recovery instead of following a plan.
    #
    # New logic: planner is ON by default. It is only turned OFF for
    # clearly trivial single-action tasks (open one URL, answer one question).
    # The planner pays for itself: it reduces total steps and wasted LLM calls
    # by keeping the executor on track across dozens or hundreds of steps.
    normalized = task.lower()

    # Explicitly skip planner only for trivial one-shot navigation
    trivial_markers = ["mở ", "vào ", "open ", "go to ", "navigate to "]
    long_task_markers = [
        "rồi", "sau đó", "tiếp tục", "xong thì", "và sau", "đồng thời",
        "liên tục", "theo dõi", "tự động", "lặp lại", "mỗi", "cho đến khi",
        "trong vòng", "suốt", "hàng giờ", "nhiều giờ", "cả ngày",
        "monitor", "watch", "loop", "repeat", "every", "until", "keep",
        "automatically", "hourly", "for hours", "all day",
    ]

    has_long_task_marker = any(m in normalized for m in long_task_markers)
    if has_long_task_marker:
        return True

    # Use keyword count to catch multi-step phrasing
    multi_step_count = sum(1 for m in ["rồi", "sau đó", "tiếp tục", "xong thì", "và ", " rồi "]
                           if m in normalized)
    if multi_step_count >= 2:
        return True

    if len(task) >= 120:
        return True

    return False


def shorten_text(text: str, max_len: int) -> str:
    clean = " ".join(text.split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3].rstrip() + "..."


def build_message_context(messages: list[dict], current_task: str) -> str:
    if not messages:
        return ""

    prior_messages = list(messages)
    if prior_messages and prior_messages[-1].get("role") == "user":
        if prior_messages[-1].get("content", "") == current_task:
            prior_messages = prior_messages[:-1]

    if not prior_messages:
        return ""

    trimmed  = prior_messages[-MAX_CONTEXT_MESSAGES:]
    recent   = trimmed[-2:]
    older    = trimmed[:-2]
    lines: list[str] = []

    if older:
        lines.append("Tom tat phien truoc:")
        for msg in older:
            role = "Nguoi dung" if msg.get("role") == "user" else "AI"
            lines.append(f"- {role}: {shorten_text(msg.get('content', ''), 90)}")

    if recent:
        lines.append("Gan day:")
        for msg in recent:
            role = "Nguoi dung" if msg.get("role") == "user" else "AI"
            lines.append(f"- {role}: {shorten_text(msg.get('content', ''), 220)}")

    context = "\n".join(lines).strip()
    return shorten_text(context, MAX_CONTEXT_CHARS) if context else ""


def translate_action_name(action_name: str) -> str:
    translations = {
        "go_to_url":      "mở trang web",
        "open_tab":       "mở tab mới",
        "switch_tab":     "chuyển tab",
        "close_tab":      "đóng tab",
        "go_back":        "quay lại",
        "refresh_page":   "tải lại trang",
        "scroll_down":    "cuộn xuống",
        "scroll_up":      "cuộn lên",
        "click_element":  "bấm vào phần tử",
        "input_text":     "nhập nội dung",
        "send_keys":      "gửi phím",
        "search_google":  "tìm kiếm trên Google",
        "extract_content":"đọc nội dung trang",
        "wait":           "chờ trang phản hồi",
        "done":           "hoàn tất",
    }
    return translations.get(action_name, action_name.replace("_", " "))


def build_step_goal(action_name: str, fallback_goal: str) -> str:
    goal_map = {
        "go_to_url":      "Đang mở trang theo yêu cầu.",
        "open_tab":       "Đang tạo tab mới để tiếp tục tác vụ.",
        "switch_tab":     "Đang chuyển sang tab phù hợp.",
        "close_tab":      "Đang dọn bớt tab không cần thiết.",
        "go_back":        "Đang quay lại bước trước đó.",
        "refresh_page":   "Đang tải lại trang để cập nhật nội dung.",
        "scroll_down":    "Đang cuộn xuống để tìm thêm thông tin hoặc nút cần thao tác.",
        "scroll_up":      "Đang cuộn lên để kiểm tra phần trước đó.",
        "click_element":  "Đang bấm vào mục cần thao tác.",
        "input_text":     "Đang nhập nội dung theo yêu cầu.",
        "send_keys":      "Đang gửi phím tắt hoặc xác nhận thao tác.",
        "search_google":  "Đang tìm kiếm thông tin trên Google.",
        "extract_content":"Đang đọc và lấy nội dung cần thiết từ trang.",
        "wait":           "Đang chờ trang phản hồi.",
        "done":           "Tác vụ ở bước này đã hoàn tất.",
    }
    if action_name in goal_map:
        return goal_map[action_name]
    if fallback_goal:
        return "Đang xử lý bước tiếp theo."
    return "Đang phân tích bước tiếp theo."


def extract_primary_action_name(model_output) -> str:
    for action in getattr(model_output, "action", []) or []:
        data = action.model_dump(exclude_none=True)
        if data:
            return next(iter(data.keys()))
    return "dang_phan_tich"


def extract_step_event(step_num: int, model_output) -> dict:
    action_keys: list[str] = []
    for action in getattr(model_output, "action", []) or []:
        data = action.model_dump(exclude_none=True)
        if not data:
            continue
        action_keys.append(next(iter(data.keys())))

    if not action_keys:
        action_keys = ["dang_phan_tich"]

    fallback_goal = ""
    evaluation = ""
    memory = ""
    current_state = getattr(model_output, "current_state", None)
    if current_state is not None:
        fallback_goal = shorten_text(getattr(current_state, "next_goal", ""), 160)
        evaluation = getattr(current_state, "evaluation_previous_goal", "")
        memory = getattr(current_state, "memory", "")

    translated_actions = [translate_action_name(a) for a in action_keys[:2]]
    primary_action     = action_keys[0]
    goal               = build_step_goal(primary_action, fallback_goal)

    return {
        "type":   "step",
        "step":   step_num,
        "action": ", ".join(translated_actions),
        "goal":   goal,
        "evaluation": evaluation,
        "memory": memory,
        "text":   f"Bước {step_num}: {translated_actions[0]}",
    }


def format_duration_vi(total_seconds: int) -> str:
    hours, remainder = divmod(max(total_seconds, 0), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def build_needs_input_message(
    primary_action: str,
    run_state: dict,
    consecutive_failures: int,
    *,
    page_url: str = "",
    last_errors: list[str] | None = None,
    user_task: str = "",
) -> str | None:
    elapsed_seconds = int(time.monotonic() - run_state["started_at"])
    recent_actions  = run_state["recent_actions"]
    errors   = [e for e in (last_errors or []) if e]
    url      = (page_url or "").strip() or "không xác định"
    err_hint = f" Lỗi gần nhất: {errors[0][:180]}." if errors else ""

    if url in {"about:blank", "chrome://newtab/", "edge://newtab/"} or url.startswith("about:"):
        return (
            f"Trình duyệt vẫn ở trang trống ({url}) — agent chưa mở được trang đích.{err_hint} "
            "Hãy trả lời một trong các cách:\n"
            "• Gõ lại lệnh kèm URL đầy đủ, ví dụ: «Mở https://www.youtube.com rồi tìm nhạc trẻ»\n"
            "• Hoặc: «Vào Google, tìm …»\n"
            "• Hoặc bấm Reset trình duyệt (nếu có) rồi thử lại."
        )

    if consecutive_failures >= MAX_FAILS_BEFORE_ASK:
        task_hint = f" Mục tiêu: «{shorten_text(user_task, 120)}»." if user_task else ""
        return (
            f"Agent gặp {consecutive_failures} lỗi liên tiếp trên {url}.{err_hint}{task_hint} "
            "Hãy chỉ rõ:\n"
            "• Nút/link cần bấm (tên hiển thị trên trang)\n"
            "• Từ khóa cần gõ vào ô tìm kiếm\n"
            "• Hoặc URL trang cần mở trước khi thao tiếp"
        )

    if elapsed_seconds >= MAX_AGENT_SECONDS:
        return (
            f"Tác vụ đã chạy khoảng {format_duration_vi(elapsed_seconds)} và cần bạn xác nhận hướng đi tiếp. "
            "Hãy trả lời rõ bước ưu tiên hoặc điều kiện dừng để agent tiếp tục đúng ý bạn."
        )

    if (
        len(recent_actions) >= MAX_REPEAT_ACTIONS
        and len(set(recent_actions[-MAX_REPEAT_ACTIONS:])) == 1
        and primary_action in STUCK_ACTIONS
    ):
        if primary_action in {"scroll_down", "scroll_up"}:
            return (
                "Agent đang cuộn trang lặp lại mà chưa tìm thấy mục tiêu. "
                "Hãy mô tả rõ hơn nút hoặc khu vực cần tìm để tiếp tục chính xác hơn."
            )
        if primary_action == "click_element":
            return (
                "Agent đang thử bấm lặp lại mà chưa tiến triển. "
                "Hãy nói rõ phần tử đúng cần bấm hoặc tiêu chí nhận biết của nó."
            )
        return (
            "Agent đang lặp lại cùng một kiểu hành động mà chưa có tiến triển rõ ràng. "
            "Hãy cho thêm chỉ dẫn để tiếp tục thay vì thử đi thử lại."
        )

    return None


def get_agent_consecutive_failures(agent: Any) -> int:
    """browser-use internal API — dùng getattr để tránh AttributeError khi đổi version."""
    state = getattr(agent, "state", None)
    failures = getattr(state, "consecutive_failures", 0) if state else 0
    try:
        return int(failures or 0)
    except (TypeError, ValueError):
        return 0


def get_agent_browser_context(agent: Any) -> Any:
    return getattr(agent, "browser_context", None)


def safe_agent_stop(agent: Any) -> None:
    stop_fn = getattr(agent, "stop", None)
    if callable(stop_fn):
        stop_fn()


async def get_agent_page_url(agent: "Agent | None") -> str:
    if agent is None:
        return ""
    try:
        ctx = get_agent_browser_context(agent)
        if ctx is None:
            return ""
        page = await ctx.get_current_page()
        return page.url or ""
    except Exception:
        return ""


def extract_agent_errors(agent: "Agent | None") -> list[str]:
    if agent is None:
        return []
    state = getattr(agent, "state", None)
    last_result = getattr(state, "last_result", None) if state else None
    if not last_result:
        return []
    errors: list[str] = []
    for result in last_result:
        err = getattr(result, "error", None)
        if err:
            errors.append(str(err))
    return errors


def infer_initial_actions(task: str) -> list[dict] | None:
    # FIX 5: Remove the unconditional google.com fallback.
    # Previously any task that didn't match youtube/facebook/http would trigger
    # a go_to_url("https://www.google.com") before the agent even started thinking.
    # The agent then spent step 1 just seeing the Google homepage it already
    # could have navigated to on its own — wasting a full LLM call + screenshot.
    # Now we only inject an action when we have high-confidence knowledge of the
    # destination URL. For everything else, returning None lets the agent decide.
    import re
    lower = task.lower()
    if "youtube" in lower:
        return [{"go_to_url": {"url": "https://www.youtube.com"}}]
    if any(k in lower for k in ("facebook", "fb.com", "facebook.com")):
        return [{"go_to_url": {"url": "https://www.facebook.com"}}]
    match = re.search(r"https?://[^\s\"'<>]+", task)
    if match:
        return [{"go_to_url": {"url": match.group(0).rstrip(".,)")}}]
    return None  # let the agent navigate on its own


async def request_stop_for_chat(chat_id: str) -> dict[str, Any]:
    """Dừng hẳn agent đang chạy cho chat_id (nếu có)."""
    async with _stop_lock:
        stop_requests.add(chat_id)
        session = sessions.get(chat_id)
        if not session:
            return {"ok": True, "stopping": False, "reason": "no_session"}

        session["stop_requested"] = True
        run_state = session.get("current_run")
        if isinstance(run_state, dict):
            run_state["stop_requested"] = True

        agent = session.get("current_agent")
        if agent is not None:
            safe_agent_stop(agent)
            return {"ok": True, "stopping": True}

        return {"ok": True, "stopping": False}


async def close_session(chat_id: str) -> None:
    stop_requests.discard(chat_id)
    session = sessions.pop(chat_id, None)
    if not session:
        return
    current_agent = session.get("current_agent")
    if current_agent:
        safe_agent_stop(current_agent)
    browser_context = session.get("browser_context")
    if browser_context:
        try:
            await browser_context.close()
        except Exception:
            pass


# ── API models ────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    chat_id: str
    task: str
    executor_model: str | None = None
    planner_model:  str | None = None
    vision_mode: str | None = None
    user_profile: str | None = None
    workflow_id: str | None = None


class ConfigRequest(BaseModel):
    executor_model: str
    planner_model:  str
    vision_mode: str | None = None


class StopRequest(BaseModel):
    chat_id: str


class CreateProfileRequest(BaseModel):
    name: str


class FeedbackRequest(BaseModel):
    satisfied: bool
    workflow_name: str | None = None
    user_profile: str | None = None
    workflow_mode: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse("index.html")

@app.get("/api/models")
async def get_models():
    return {"models": MODEL_OPTIONS}

@app.get("/api/config")
async def get_config():
    return build_config_payload()

@app.post("/api/config")
async def set_config(req: ConfigRequest):
    MODEL_CONFIG["executor_model"] = resolve_model(req.executor_model)
    MODEL_CONFIG["planner_model"]  = resolve_model(req.planner_model)
    if req.vision_mode is not None:
        MODEL_CONFIG["vision_mode"] = resolve_vision_mode(req.vision_mode)
    return build_config_payload()

@app.get("/api/history")
async def get_history():
    history = await load_history_async()
    return list(reversed(list(history.values())))

@app.post("/api/new_chat")
async def new_chat():
    chat_id = str(uuid.uuid4())[:8]
    return {"chat_id": chat_id}


@app.post("/api/stop")
async def stop_task(req: StopRequest):
    """Dừng hẳn task agent đang chạy cho phiên chat."""
    return await request_stop_for_chat(req.chat_id)


@app.delete("/api/history/{chat_id}")
async def delete_chat(chat_id: str):
    async with _history_lock:
        history = load_history()
        history.pop(chat_id, None)
        save_history(history)
    await close_session(chat_id)
    return {"ok": True}


@app.get("/api/profiles")
async def get_profiles():
    return {"profiles": load_user_profiles()}


@app.post("/api/profiles")
async def create_profile(req: CreateProfileRequest):
    name = req.name.strip()
    if not name:
        return {"ok": False, "error": "Tên hồ sơ không được để trống"}
    profiles = load_user_profiles()
    if name in profiles:
        return {"ok": False, "error": "Hồ sơ đã tồn tại"}
    profiles.append(name)
    save_user_profiles(profiles)
    return {"ok": True, "profiles": profiles}


@app.get("/api/site_workflows")
async def get_site_workflows():
    return {"site_profiles": load_site_profiles()}


@app.delete("/api/site_workflows/{domain}/{workflow_id}")
async def delete_site_workflow(domain: str, workflow_id: str):
    site_profiles = load_site_profiles()
    if domain in site_profiles:
        original_len = len(site_profiles[domain])
        site_profiles[domain] = [wf for wf in site_profiles[domain] if wf.get("workflow_id") != workflow_id]
        if len(site_profiles[domain]) < original_len:
            if not site_profiles[domain]:
                site_profiles.pop(domain)
            save_site_profiles(site_profiles)
            return {"ok": True, "message": "Đã xóa kịch bản thành công"}
    return {"ok": False, "error": "Không tìm thấy kịch bản hoặc tên miền"}


@app.post("/api/chat/{chat_id}/feedback")
async def chat_feedback(chat_id: str, req: FeedbackRequest):
    session = sessions.get(chat_id)
    if not session:
        return {"ok": False, "error": f"Không tìm thấy phiên làm việc {chat_id}"}

    if not req.satisfied:
        session.pop("last_history", None)
        return {"ok": True, "message": "Đã ghi nhận phản hồi không hài lòng"}

    last_history = session.get("last_history")
    original_task = session.get("task")

    if not last_history or not original_task:
        if session.get("last_replay_workflow"):
            return {
                "ok": True,
                "message": "Workflow đã học đã được replay thành công, không cần đóng gói lại.",
            }
        return {"ok": False, "error": "Không tìm thấy kịch bản chạy gần nhất để đóng gói. Vui lòng đảm bảo kịch bản đã chạy thành công trước khi lưu."}

    wf_name = (req.workflow_name or "").strip() or f"Kịch bản chạy lúc {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    user_prof = req.user_profile or "Default"
    workflow_mode = (req.workflow_mode or "general").strip().lower()
    if workflow_mode not in {"general", "specific"}:
        workflow_mode = "general"

    try:
        domain = get_domain_from_history(last_history)
        generalized = generalize_history(
            original_task,
            last_history,
            specific=workflow_mode == "specific",
        )
        validation_issues = workflow_validation_issues(generalized)
        blocking_issues = {
            "workflow_has_opaque_var_names",
            "credential_workflow_must_use_fill_login_form",
            "password_must_not_use_raw_input_text_index",
            "username_password_values_must_differ",
        }
        if set(validation_issues) & blocking_issues:
            return {
                "ok": False,
                "error": "Workflow chưa đủ an toàn để lưu. Hệ thống cần chạy/repair lại bằng action semantic ổn định hơn.",
                "validation_issues": validation_issues,
            }

        site_profiles = load_site_profiles()
        if domain not in site_profiles:
            site_profiles[domain] = []

        workflow = {
            "workflow_id": str(uuid.uuid4())[:8],
            "workflow_name": wf_name,
            "user_profile": user_prof,
            "prompt_template": generalized["prompt_template"],
            "variables": generalized["variables"],
            "steps": generalized["steps"],
            "metadata": generalized.get("metadata", {}),
            "stats": generalized.get("stats", {}),
            "confidence": generalized.get("confidence", 0.68),
        }
        workflow["metadata"]["domain"] = domain
        workflow["metadata"]["workflow_scope"] = workflow_mode
        site_profiles[domain].append(workflow)
        save_site_profiles(site_profiles)

        session.pop("last_history", None)

        return {
            "ok": True,
            "message": "Đã đóng gói và lưu kịch bản thành công!",
            "domain": domain,
            "workflow": workflow
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": f"Lỗi đóng gói kịch bản: {str(e)}"}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Stream phản hồi của AI qua SSE."""

    async def generate() -> AsyncGenerator[str, None]:
        global browser_instance

        def sse(data: dict) -> str:
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        def status(text: str, phase: str) -> str:
            return sse({"type": "status", "text": text, "phase": phase})

        await add_message_async(req.chat_id, "user", req.task)
        stop_requests.discard(req.chat_id)
        yield status("Đã nhận lệnh, đang chuẩn bị xử lý...", "received")

        def is_stop_requested() -> bool:
            return req.chat_id in stop_requests

        async def emit_stopped_early(text: str) -> AsyncGenerator[str, None]:
            stop_requests.discard(req.chat_id)
            await add_message_async(req.chat_id, "assistant", text, {"state": "stopped"})
            yield sse({"type": "stopped", "text": text})

        if not BROWSER_USE_AVAILABLE or not GOOGLE_LLM_AVAILABLE:
            msg = "Thieu thu vien: pip install browser-use langchain-google-genai"
            await add_message_async(req.chat_id, "assistant", msg, {"state": "error"})
            yield sse({"type": "error", "text": msg})
            return

        # FIX 2 (continued): use cached key check instead of calling LLM every time
        if not use_vertex() and not _key_ok:
            msg = _key_err or "API key Gemini không hợp lệ."
            await add_message_async(req.chat_id, "assistant", msg, {"state": "error"})
            yield sse({"type": "error", "text": msg})
            return

        try:
            executor_model = resolve_model(req.executor_model or MODEL_CONFIG["executor_model"])
            planner_model  = resolve_model(req.planner_model  or MODEL_CONFIG["planner_model"])
        except ValueError as exc:
            msg = str(exc)
            await add_message_async(req.chat_id, "assistant", msg, {"state": "error"})
            yield sse({"type": "error", "text": msg})
            return

        created_browser      = False
        fresh_browser_context = False

        if browser_instance is None:
            yield status("Đang khởi động trình duyệt...", "browser")
            try:
                browser_instance = await with_timeout(
                    asyncio.to_thread(
                        Browser,
                        config=BrowserConfig(headless=False, keep_alive=True),
                    ),
                    BROWSER_START_TIMEOUT,
                    "Khởi động trình duyệt",
                )
                created_browser = True
            except RuntimeError as exc:
                msg = str(exc)
                await add_message_async(req.chat_id, "assistant", msg, {"state": "error"})
                yield sse({"type": "error", "text": msg})
                return
        else:
            yield status("Đang dùng lại trình duyệt hiện có...", "browser")

        if is_stop_requested():
            async for chunk in emit_stopped_early("Đã dừng task trước khi agent khởi chạy."):
                yield chunk
            return

        session = sessions.get(req.chat_id)
        if session is None:
            yield status("Đang tạo phiên làm việc mới...", "session")
            try:
                browser_context = await with_timeout(
                    browser_instance.new_context(),
                    BROWSER_CONTEXT_TIMEOUT,
                    "Tạo phiên trình duyệt",
                )
            except RuntimeError as exc:
                if created_browser:
                    try:
                        await browser_instance.close()
                    except Exception:
                        pass
                    browser_instance = None
                msg = str(exc)
                await add_message_async(req.chat_id, "assistant", msg, {"state": "error"})
                yield sse({"type": "error", "text": msg})
                return
            session = {
                "context":        "",
                "browser_context": browser_context,
                "awaiting_user":   False,
                "awaiting_reason": None,
                "last_progress":   None,
                "vision_mode":     resolve_vision_mode(req.vision_mode),
                "vision_steps_used": 0,
                "max_vision_steps": MAX_VISION_STEPS_PER_TASK,
                "stop_requested":  False,
                "current_agent":   None,
                "current_run":     None,
                "last_usage":      None,
            }
            sessions[req.chat_id]  = session
            fresh_browser_context = True
        elif session.get("browser_context") is None:
            yield status("Đang khôi phục ngữ cảnh trình duyệt...", "session")
            try:
                session["browser_context"] = await with_timeout(
                    browser_instance.new_context(),
                    BROWSER_CONTEXT_TIMEOUT,
                    "Khôi phục phiên trình duyệt",
                )
                fresh_browser_context = True
            except RuntimeError as exc:
                msg = str(exc)
                await add_message_async(req.chat_id, "assistant", msg, {"state": "error"})
                yield sse({"type": "error", "text": msg})
                return
        else:
            yield status("Đang dùng lại phiên trước đó...", "session")

        if is_stop_requested():
            async for chunk in emit_stopped_early("Đã dừng task trước khi agent khởi chạy."):
                yield chunk
            return

        session["vision_mode"] = resolve_vision_mode(req.vision_mode)
        session["user_profile"] = req.user_profile or "Default"
        session["stop_requested"] = False
        session.pop("last_replay_workflow", None)
        session["vision_steps_used"] = 0
        session["max_vision_steps"] = MAX_VISION_STEPS_PER_TASK

        resume_progress = session.get("last_progress") if session.get("awaiting_user") else None
        if session.get("awaiting_user"):
            yield status("Đang tiếp tục từ phiên đã chờ chỉ dẫn của bạn...", "resume")
        session["awaiting_user"]   = False
        session["awaiting_reason"] = None

        # FIX 1 (context build): load_history() now reads from _history_cache — no disk I/O
        history          = await load_history_async()
        messages         = history.get(req.chat_id, {}).get("messages", [])
        message_context  = build_message_context(messages, req.task)
        if resume_progress:
            progress_note = f"[Tiến độ trước: {resume_progress}]\n"
            message_context = progress_note + message_context if message_context else progress_note.rstrip()
        if message_context:
            session["context"] = message_context
            yield status("Đang nạp ngữ cảnh hội thoại gọn...", "context")
        else:
            session["context"] = ""
            yield status("Không có ngữ cảnh cũ, chạy với lệnh hiện tại...", "context")

        use_planner     = should_use_planner(req.task)
        vision_mode     = session["vision_mode"]
        mode_text       = "planner" if use_planner else "nhanh"
        executor_option = get_model_option(executor_model)
        planner_option  = get_model_option(planner_model)
        vision_labels   = {"auto": "vision tự động", "on": "luôn vision", "off": "chỉ text/DOM"}
        yield status(
            f"Chế độ {mode_text}, {vision_labels.get(vision_mode, vision_mode)}, "
            f"{executor_option['label']} / planner {planner_option['label']}...",
            "mode",
        )

        # FIX 6: Fetch exchange rate concurrently with the status yield above,
        # not sequentially. For most requests the cache will be hot and this
        # returns instantly; on a cache miss it saves ~0.5s by not blocking.
        exchange_rate_info = await get_exchange_rate_info()

        event_queue: asyncio.Queue = asyncio.Queue(maxsize=300)
        executor_callback = UsageMetadataCallbackHandler()
        planner_callback  = UsageMetadataCallbackHandler() if use_planner else None
        task_state = {
            "started_at":    time.monotonic(),
            "recent_actions": [],
            "awaiting_user": False,
            "stop_requested": False,
            "question":      None,
            "current_step":  0,
        }

        def current_usage_summary() -> dict:
            return build_usage_summary(
                executor_model=executor_model,
                planner_model=planner_model if use_planner else None,
                executor_callback=executor_callback,
                planner_callback=planner_callback,
                exchange_rate_info=exchange_rate_info,
                elapsed_seconds=int(time.monotonic() - task_state["started_at"]),
                step=task_state["current_step"],
                waiting_for_user=task_state["awaiting_user"],
            )

        agent: "Agent | None" = None

        async def on_new_step(_state, model_output, step_num: int):
            task_state["current_step"] = step_num
            primary_action = extract_primary_action_name(model_output)
            task_state["recent_actions"].append(primary_action)
            task_state["recent_actions"] = task_state["recent_actions"][-6:]
            event = extract_step_event(step_num, model_output)
            try:
                event_queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        async def on_step_end(agent_ref: "Agent"):
            usage_event = {"type": "usage", "usage": current_usage_summary()}
            try:
                event_queue.put_nowait(usage_event)
            except asyncio.QueueFull:
                pass

            if task_state["awaiting_user"]:
                return

            if is_stop_requested() or session.get("stop_requested"):
                safe_agent_stop(agent_ref)
                return

            vision_note = adjust_vision_for_step_end(agent_ref, session, task_state)
            if vision_note:
                try:
                    event_queue.put_nowait({
                        "type": "status",
                        "text": vision_note,
                        "phase": "vision",
                    })
                except asyncio.QueueFull:
                    pass

            primary_action = (task_state["recent_actions"][-1]
                              if task_state["recent_actions"] else "dang_phan_tich")

            # FIX 7: Skip the async page-URL lookup when the agent is healthy.
            # get_agent_page_url() awaits a Playwright call on every single step.
            # On a 100-step task that's 100 extra round-trips to the browser process
            # (~50–200ms each). We only need the URL when diagnosing a stuck/failing
            # agent, so gate it on consecutive_failures > 0.
            consecutive_failures = get_agent_consecutive_failures(agent_ref)
            if consecutive_failures > 0:
                page_url   = await get_agent_page_url(agent_ref)
                last_errors = extract_agent_errors(agent_ref)
            else:
                page_url    = ""
                last_errors = []

            question = build_needs_input_message(
                primary_action,
                task_state,
                consecutive_failures,
                page_url=page_url,
                last_errors=last_errors,
                user_task=req.task,
            )
            if question:
                task_state["awaiting_user"]   = True
                task_state["question"]        = question
                session["awaiting_user"]      = True
                session["awaiting_reason"]    = question
                session["last_usage"]         = current_usage_summary()
                progress_url = page_url
                if not progress_url:
                    progress_url = await get_agent_page_url(agent_ref)
                last_action = (
                    task_state["recent_actions"][-1]
                    if task_state["recent_actions"]
                    else "không rõ"
                )
                session["last_progress"] = (
                    f"Đã thực hiện {task_state['current_step']} bước. "
                    f"Hành động cuối: {last_action}. "
                    f"Đang ở trang: {progress_url or 'không xác định'}."
                )
                # needs_input: await put — không drop khi queue đầy (put_nowait + pass gây im lặng)
                await event_queue.put({
                    "type":  "needs_input",
                    "text":  question,
                    "usage": current_usage_summary(),
                })
                safe_agent_stop(agent_ref)

        workflow_replay_hint = ""
        workflow_runtime_metadata = None
        workflow_match = None
        user_profile = session.get("user_profile", "Default")
        is_explicit = False

        if req.workflow_id:
            try:
                site_profiles = load_site_profiles()
                for domain, workflows in site_profiles.items():
                    for wf in workflows:
                        if wf.get("workflow_id") == req.workflow_id:
                            var_values = {}
                            template = wf.get("prompt_template", "")
                            normalized_template = normalize_text(template)
                            pattern, vars_found = template_to_regex(normalized_template)
                            normalized_prompt = normalize_text(req.task)
                            match_obj = pattern.match(normalized_prompt)
                            if match_obj:
                                groups = match_obj.groups()
                                for idx, var_name in enumerate(vars_found):
                                    if idx < len(groups):
                                        var_values[var_name] = groups[idx]

                            workflow_match = build_workflow_match(
                                domain=domain,
                                wf=wf,
                                var_values=var_values,
                                score=100.0,
                                match_type="explicit_id",
                            )
                            is_explicit = True
                            break
                    if workflow_match:
                        break
            except Exception as e:
                print(f"Loi lay workflow theo id: {e}")

        if not workflow_match:
            try:
                workflow_match = find_matching_workflow(user_profile, req.task)
            except Exception as e:
                print(f"Loi kiem tra workflow khop: {e}")

        replay_allowed = False
        if workflow_match:
            replay_allowed, replay_reason = should_replay_workflow(workflow_match, explicit=is_explicit)
            if not replay_allowed:
                wf = workflow_match.get("workflow") or {}
                yield status(
                    f"Workflow '{wf.get('workflow_name', 'workflow da hoc')}' khop nhung bi bo qua: {replay_reason}.",
                    "workflow_skip",
                )

        if workflow_match and replay_allowed:
            workflow = workflow_match.get("workflow") or {}
            wf_name = workflow.get("workflow_name") or "workflow da hoc"
            yield status(f"Tim thay workflow '{wf_name}'. Dang replay truc tiep de tiet kiem token...", "workflow_replay")

            current_url = ""
            try:
                page = await session["browser_context"].get_current_page()
                current_url = page.url or ""
            except Exception:
                current_url = ""

            replay_plan = build_workflow_replay_plan(workflow_match, req.task, current_url)
            replay_actions = [step["action"] for step in replay_plan]
            replay_error = ""
            final_text = replay_final_text(workflow_match, replay_actions, req.task)
            executed_replay_steps = 0
            previous_step_filled_text = False
            local_repair_count = 0
            ai_interventions = 0
            workflow_resumed = False
            terminal_verified = False
            replay_trace: list[dict] = []
            session["current_run"] = task_state

            for idx, plan_step in enumerate(replay_plan, start=1):
                if is_stop_requested() or session.get("stop_requested"):
                    session["current_run"] = None
                    async for chunk in emit_stopped_early("Da dung workflow truoc khi replay xong."):
                        yield chunk
                    return

                action = plan_step.get("action") or {}
                action_name, params = action_name_and_params(action)
                if not action_name:
                    continue
                if action_name == "done":
                    final_text = replay_final_text(workflow_match, replay_actions, req.task)
                    break

                task_state["current_step"] = idx
                executed_replay_steps += 1
                yield sse({
                    "type": "step",
                    "step": idx,
                    "action": translate_action_name(action_name),
                    "goal": "Replay workflow da hoc",
                    "evaluation": "Dang chay truc tiep khong goi Agent neu action khong can LLM.",
                    "memory": f"Workflow: {wf_name}",
                    "text": f"Replay buoc {idx}: {translate_action_name(action_name)}",
                })

                if is_brittle_replay_action(action_name, params) and executed_replay_steps > 1:
                    terminal = await verify_workflow_terminal_state(
                        workflow_match,
                        replay_actions,
                        session["browser_context"],
                    )
                    if terminal.get("ok") and float(terminal.get("confidence") or 0) >= 0.72:
                        terminal_verified = True
                        result = ReplayStepResult(
                            ok=True,
                            action_name=action_name,
                            resolver="terminal_verifier",
                            message=f"Skipped brittle action because terminal state is verified: {terminal.get('reason')}",
                            skipped=True,
                            verification=terminal,
                        )
                        replay_trace.append(replay_trace_entry(idx, plan_step, result))
                        yield sse({
                            "type": "status",
                            "text": result.message,
                            "phase": "workflow_verify",
                        })
                        final_text = replay_final_text(workflow_match, replay_actions, req.task)
                        break

                hint = runtime_hint_for_step(workflow_match, plan_step)
                result = await execute_semantic_replay_step(
                    plan_step,
                    session["browser_context"],
                    after_text_input=previous_step_filled_text,
                    hint=hint,
                )
                replay_trace.append(replay_trace_entry(idx, plan_step, result))

                if not result.ok:
                    step_failure = result.error or result.message or f"{action_name} failed"
                    can_recover, recover_reason = can_attempt_ai_recovery(workflow_match, plan_step)
                    if can_recover and ai_interventions < WORKFLOW_AI_RECOVERY_MAX_INTERVENTIONS:
                        ai_interventions += 1
                        yield status(
                            f"Workflow gap bien co o buoc {idx}. Goi AI recover-only roi se quay lai workflow...",
                            "workflow_ai_recovery",
                        )
                        recovery = await attempt_ai_workflow_recovery(
                            match=workflow_match,
                            replay_actions=replay_actions,
                            plan_step=plan_step,
                            browser_context=session["browser_context"],
                            user_task=req.task,
                            failure_reason=step_failure,
                            llm=make_llm(executor_model, executor_callback),
                        )
                        recovery_entry = ReplayStepResult(
                            ok=recovery.ok,
                            action_name=action_name,
                            resolver="ai_recover_only",
                            message=recovery.message,
                            error=recovery.error,
                            repaired=True,
                            verification=recovery.verification,
                        )
                        replay_trace.append(replay_trace_entry(idx, plan_step, recovery_entry))
                        session["last_usage"] = current_usage_summary()
                        yield sse({"type": "usage", "usage": session["last_usage"]})
                        if recovery.ok:
                            workflow_resumed = True
                            yield status(
                                "AI da khoi phuc checkpoint. Tra quyen lai cho workflow de chay tiep...",
                                "workflow_resumed",
                            )
                            previous_step_filled_text = replay_capability_for(action_name) == "fill"
                            await wait_for_replay_settle(action_name, session["browser_context"])
                            continue
                        replay_error = recovery.error or step_failure
                        break
                    replay_error = step_failure if can_recover else f"{step_failure} ({recover_reason})"
                    break

                if result.repaired:
                    budgeted_repair = should_count_local_repair(result)
                    if budgeted_repair:
                        local_repair_count += 1
                    if local_repair_count > WORKFLOW_LOCAL_REPAIR_LIMIT:
                        step_failure = "workflow local repair limit exceeded"
                        can_recover, recover_reason = can_attempt_ai_recovery(workflow_match, plan_step)
                        if can_recover and ai_interventions < WORKFLOW_AI_RECOVERY_MAX_INTERVENTIONS:
                            ai_interventions += 1
                            yield status(
                                f"Workflow can local repair qua gioi han o buoc {idx}. Goi AI recover-only...",
                                "workflow_ai_recovery",
                            )
                            recovery = await attempt_ai_workflow_recovery(
                                match=workflow_match,
                                replay_actions=replay_actions,
                                plan_step=plan_step,
                                browser_context=session["browser_context"],
                                user_task=req.task,
                                failure_reason=step_failure,
                                llm=make_llm(executor_model, executor_callback),
                            )
                            recovery_entry = ReplayStepResult(
                                ok=recovery.ok,
                                action_name=action_name,
                                resolver="ai_recover_only",
                                message=recovery.message,
                                error=recovery.error,
                                repaired=True,
                                verification=recovery.verification,
                            )
                            replay_trace.append(replay_trace_entry(idx, plan_step, recovery_entry))
                            session["last_usage"] = current_usage_summary()
                            yield sse({"type": "usage", "usage": session["last_usage"]})
                            if recovery.ok:
                                workflow_resumed = True
                                yield status(
                                    "AI da khoi phuc checkpoint. Tra quyen lai cho workflow de chay tiep...",
                                    "workflow_resumed",
                                )
                                previous_step_filled_text = replay_capability_for(action_name) == "fill"
                                await wait_for_replay_settle(action_name, session["browser_context"])
                                continue
                            replay_error = recovery.error or step_failure
                            break
                        replay_error = step_failure if can_recover else f"{step_failure} ({recover_reason})"
                        break
                    yield sse({
                        "type": "status",
                        "text": result.message or f"Replayed {action_name} with local repair",
                        "phase": "workflow_repair" if budgeted_repair else "workflow_resolver",
                    })
                elif result.resolver not in {"native", "legacy_index_activate", "legacy_index_fill", "done"}:
                    yield sse({
                        "type": "status",
                        "text": result.message or f"Replayed {action_name} with {result.resolver}",
                        "phase": "workflow_resolver",
                    })

                if result.done:
                    final_text = result.final_text or final_text
                    break

                previous_step_filled_text = replay_capability_for(action_name) == "fill"
                await wait_for_replay_settle(action_name, session["browser_context"])

                usage_now = current_usage_summary()
                session["last_usage"] = usage_now
                yield sse({"type": "usage", "usage": usage_now})

            if not replay_error:
                usage = current_usage_summary()
                session["last_usage"] = usage
                metadata = {
                    "state": "done",
                    "usage": usage,
                    "workflow_replay": True,
                    "workflow_id": workflow.get("workflow_id"),
                    "workflow_name": wf_name,
                    "workflow_runtime": {
                        "runtime_version": WORKFLOW_RUNTIME_VERSION,
                        "workflow_matched": True,
                        "workflow_replay_attempted": True,
                        "workflow_replay_success": True,
                        "terminal_verified": terminal_verified,
                        "local_repair_count": local_repair_count,
                        "ai_interventions": ai_interventions,
                        "workflow_resumed": workflow_resumed,
                        "ai_fallback": False,
                        "trace": replay_trace[-20:],
                    },
                }
                session["last_replay_workflow"] = metadata
                update_workflow_stats(
                    workflow_match.get("domain") or "",
                    workflow.get("workflow_id"),
                    success=True,
                    tokens=int((usage.get("totals") or {}).get("total_tokens") or 0),
                    example=req.task,
                )
                persist_workflow_runtime_learning(
                    workflow_match.get("domain") or "",
                    workflow.get("workflow_id"),
                    trace=replay_trace,
                    success=True,
                )
                await add_message_async(req.chat_id, "assistant", final_text, metadata)
                session["current_run"] = None
                yield sse({"type": "usage", "usage": usage})
                yield sse({"type": "done", "text": final_text, "usage": usage})
                return

            session["current_run"] = None
            update_workflow_stats(
                workflow_match.get("domain") or "",
                workflow.get("workflow_id"),
                success=False,
                error=replay_error,
                example=req.task,
            )
            persist_workflow_runtime_learning(
                workflow_match.get("domain") or "",
                workflow.get("workflow_id"),
                trace=replay_trace,
                success=False,
                failure_reason=replay_error,
            )
            workflow_runtime_metadata = {
                "runtime_version": WORKFLOW_RUNTIME_VERSION,
                "workflow_matched": True,
                "workflow_replay_attempted": True,
                "workflow_replay_success": False,
                "terminal_verified": terminal_verified,
                "local_repair_count": local_repair_count,
                "ai_interventions": ai_interventions,
                "workflow_resumed": workflow_resumed,
                "ai_fallback": True,
                "fallback_reason": replay_error[:500],
                "trace": replay_trace[-20:],
            }
            workflow_replay_hint = (
                f"[Workflow replay failed after {executed_replay_steps} step(s). "
                f"Workflow: {wf_name}. Error: {replay_error[:300]}]\n"
                "Please solve the user's task normally. Prefer stable text/URL based actions over brittle element indexes."
            )
            yield status(
                f"Workflow replay gap loi: {replay_error[:160]}. Dang fallback sang Agent tong quat...",
                "workflow_fallback",
            )

        try:
            os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY
            yield status("Agent đang thực thi trên trình duyệt...", "running")

            # FIX 8: Improved system prompt for long-running tasks.
            # The original prompt was defensive ("don't loop"). This version adds
            # explicit guidance for marathon tasks: checkpoint progress, use
            # extract_content proactively, and prefer explicit waits over spin-loops.
            extend_msg = (
                "Luôn mở trang web cụ thể trước khi thao tác. "
                "Nếu đang ở about:blank hoặc trang trống, hãy go_to_url hoặc search_google ngay. "
                "Ưu tiên thao tác qua danh sách phần tử interactive (index) trong DOM; "
                "khi có ảnh màn hình, dùng vision để xác nhận layout hoặc nút khó thấy trong DOM. "
                "Nếu cần cuộn (scroll) bên trong một phần tử cụ thể như dropdown menu, ô chọn chi nhánh hoặc danh sách con bị cuộn, "
                "hãy dùng công cụ `scroll_element` với index của phần tử đó thay vì cuộn toàn trang. "
                "Với tác vụ dài nhiều giờ: ưu tiên ghi nhớ trạng thái hiện tại qua extract_content "
                "sau mỗi bước quan trọng, dùng wait thay vì lặp click khi trang đang tải, "
                "và tóm tắt tiến độ trong next_goal để planner giữ hướng đúng. "
                "Nếu bị lặp hành động hoặc chưa tìm ra mục tiêu sau vài lần thử, "
                "hãy ưu tiên làm rõ tình trạng thay vì cố thử đi thử lại mù quáng."
            )

            initial_actions_to_use = infer_initial_actions(req.task) if fresh_browser_context else None
            if workflow_replay_hint:
                message_context = (workflow_replay_hint + "\n" + (message_context or "")).strip()

            agent = Agent(
                task=req.task,
                llm=make_llm(executor_model, executor_callback),
                planner_llm=make_llm(planner_model, planner_callback) if use_planner else None,
                planner_interval=PLANNER_INTERVAL,
                max_actions_per_step=MAX_ACTIONS_PER_STEP,
                max_input_tokens=MAX_INPUT_TOKENS,
                max_failures=MAX_FAILS_BEFORE_ASK + 1,
                use_vision=initial_use_vision(vision_mode),
                enable_memory=False,
                browser_context=session.get("browser_context"),
                message_context=message_context or None,
                initial_actions=initial_actions_to_use,
                register_new_step_callback=on_new_step,
                extend_system_message=extend_msg,
                controller=controller,
            )
            session["current_agent"] = agent
            session["current_run"]   = task_state

            async def run_agent():
                try:
                    result = await agent.run(max_steps=MAX_AGENT_STEPS, on_step_end=on_step_end)
                    session["last_usage"] = current_usage_summary()
                    if is_stop_requested() or session.get("stop_requested"):
                        step_n = task_state.get("current_step", 0)
                        stop_text = (
                            session.get("last_progress")
                            or f"Đã dừng task sau {step_n} bước. Trình duyệt vẫn mở — bạn có thể gửi lệnh mới."
                        )
                        await event_queue.put({
                            "type":  "stopped",
                            "text":  stop_text,
                            "usage": current_usage_summary(),
                        })
                        return
                    if task_state["awaiting_user"]:
                        return
                    final = result.final_result() or "Hoan thanh"
                    await event_queue.put({
                        "type":  "done",
                        "text":  final,
                        "usage": current_usage_summary(),
                    })
                except asyncio.CancelledError:
                    await event_queue.put({
                        "type":  "stopped",
                        "text":  "Đã dừng task theo yêu cầu của bạn.",
                        "usage": current_usage_summary(),
                    })
                except Exception as e:
                    if is_stop_requested() or session.get("stop_requested"):
                        await event_queue.put({
                            "type":  "stopped",
                            "text":  "Đã dừng task theo yêu cầu của bạn.",
                            "usage": current_usage_summary(),
                        })
                    else:
                        await event_queue.put({
                            "type":  "error",
                            "text":  f"Loi: {str(e)}",
                            "usage": current_usage_summary(),
                        })
                finally:
                    if agent:
                        session["last_history"] = agent.state.history.history
                        session["task"] = req.task
                    stop_requests.discard(req.chat_id)
                    session["stop_requested"] = False
                    session["current_agent"] = None
                    session["current_run"]   = None

            if is_stop_requested():
                safe_agent_stop(agent)
                stop_text = "Đã dừng task trước khi agent bắt đầu chạy."
                await add_message_async(req.chat_id, "assistant", stop_text, {"state": "stopped"})
                yield sse({"type": "stopped", "text": stop_text})
                stop_requests.discard(req.chat_id)
                session["stop_requested"] = False
                session["current_agent"] = None
                session["current_run"] = None
                return

            agent_task = asyncio.create_task(run_agent())

            # SSE heartbeat: giữ kết nối sống khi agent chờ Gemini (5–15s/bước).
            # Proxy/firewall thường cắt idle stream sau ~60–120s; dòng ": ping" là comment SSE.
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    if is_stop_requested():
                        active = session.get("current_agent")
                        if active is not None:
                            safe_agent_stop(active)
                    yield ": ping\n\n"
                    continue

                yield sse(event)
                if event["type"] in {"done", "error", "needs_input", "stopped"}:
                    final_text = event["text"]
                    agent_task_joined = False
                    repair_result = None
                    if event["type"] == "done" and workflow_replay_hint and workflow_match:
                        await agent_task
                        agent_task_joined = True
                        usage_totals = ((event.get("usage") or {}).get("totals") or {})
                        repair_result = auto_repair_workflow_from_history(
                            workflow_match,
                            original_task=req.task,
                            history_list=session.get("last_history") or [],
                            final_text=final_text,
                            tokens=int(usage_totals.get("total_tokens") or 0),
                        )
                    metadata   = {
                        "state": event["type"],
                        "usage": event.get("usage"),
                    }
                    if workflow_runtime_metadata:
                        metadata["workflow_runtime"] = workflow_runtime_metadata
                    if repair_result:
                        metadata["workflow_auto_repair"] = repair_result
                    await add_message_async(req.chat_id, "assistant", final_text, metadata)

                    # FIX 1 (context refresh): load_history() reads from cache — no disk I/O
                    refreshed_history  = await load_history_async()
                    refreshed_messages = refreshed_history.get(req.chat_id, {}).get("messages", [])
                    session["context"] = build_message_context(refreshed_messages, "")
                    session["last_usage"] = event.get("usage")
                    stop_requests.discard(req.chat_id)
                    session["stop_requested"] = False
                    if event["type"] in {"done", "error", "stopped"}:
                        session["last_progress"] = None

                    if not agent_task_joined:
                        await agent_task
                    return

        except Exception as e:
            err = f"Loi: {str(e)}"
            await add_message_async(req.chat_id, "assistant", err, {"state": "error"})
            yield sse({"type": "error", "text": err})

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/reset_browser")
async def reset_browser():
    """Đóng và reset browser instance."""
    global browser_instance

    if browser_instance:
        try:
            await browser_instance.close()
        except Exception:
            pass
        browser_instance = None

    for chat_id, session in list(sessions.items()):
        stop_requests.discard(chat_id)
        session["context"]        = ""
        session["awaiting_user"]  = False
        session["awaiting_reason"]= None
        session["last_progress"]  = None
        session["stop_requested"] = False
        current_agent = session.get("current_agent")
        if current_agent:
            safe_agent_stop(current_agent)
        session["current_agent"] = None
        session["current_run"]   = None
        session.pop("browser_context", None)

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    print("\nServer chay tai: http://localhost:8000\n")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
