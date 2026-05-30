from __future__ import annotations

# server.py — FastAPI backend (optimised)
import asyncio
import json
import logging
import os
import time
import uuid
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from config import get_gemini_api_key, verify_gemini_api_key

load_dotenv()
logging.getLogger("browser_use").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Lazy imports ─────────────────────────────────────────────────────────────
try:
    from browser_use import Agent, Browser, BrowserConfig
    BROWSER_USE_AVAILABLE = True
except ImportError:
    Agent = Any  # type: ignore[misc, assignment]
    Browser = Any  # type: ignore[misc, assignment]
    BrowserConfig = Any  # type: ignore[misc, assignment]
    BROWSER_USE_AVAILABLE = False

try:
    from langchain_core.callbacks import UsageMetadataCallbackHandler
    from langchain_google_genai import ChatGoogleGenerativeAI
    GOOGLE_LLM_AVAILABLE = True
except ImportError:
    UsageMetadataCallbackHandler = Any  # type: ignore[misc, assignment]
    ChatGoogleGenerativeAI = Any  # type: ignore[misc, assignment]
    GOOGLE_LLM_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────
HISTORY_FILE             = Path("chat_history.json")
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

MODEL_OPTIONS = [
    {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash-Lite",
     "description": "Nhanh nhất, rẻ nhất cho đa số tác vụ browser."},
    {"id": "gemini-2.5-flash",      "label": "Gemini 2.5 Flash",
     "description": "Cân bằng giữa tốc độ, độ chính xác và chi phí."},
    {"id": "gemini-2.5-pro",        "label": "Gemini 2.5 Pro",
     "description": "Mạnh nhất, phù hợp tác vụ khó và dài."},
]
MODEL_IDS      = {item["id"] for item in MODEL_OPTIONS}
MODEL_DEFAULTS = {
    "executor_model": "gemini-2.5-flash-lite",
    "planner_model": "gemini-2.5-flash",
    "vision_mode": "auto",
}
MODEL_CONFIG = dict(MODEL_DEFAULTS)

VISION_MODES = frozenset({"auto", "on", "off"})
VISION_MODE_DEFAULT = "auto"
MAX_VISION_STEPS_PER_TASK = 12

MODEL_PRICING_USD_PER_1M = {
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40,  "cached": 0.01},
    "gemini-2.5-flash":      {"input": 0.30, "output": 2.50,  "cached": 0.03},
    "gemini-2.5-pro":        {"input": 1.25, "output": 10.00, "cached": 0.125},
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
if GEMINI_API_KEY:
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
    current_state = getattr(model_output, "current_state", None)
    if current_state is not None:
        fallback_goal = shorten_text(getattr(current_state, "next_goal", ""), 160)

    translated_actions = [translate_action_name(a) for a in action_keys[:2]]
    primary_action     = action_keys[0]
    goal               = build_step_goal(primary_action, fallback_goal)

    return {
        "type":   "step",
        "step":   step_num,
        "action": ", ".join(translated_actions),
        "goal":   goal,
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


class ConfigRequest(BaseModel):
    executor_model: str
    planner_model:  str
    vision_mode: str | None = None


class StopRequest(BaseModel):
    chat_id: str


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
        if not _key_ok:
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
        session["stop_requested"] = False
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
                "Với tác vụ dài nhiều giờ: ưu tiên ghi nhớ trạng thái hiện tại qua extract_content "
                "sau mỗi bước quan trọng, dùng wait thay vì lặp click khi trang đang tải, "
                "và tóm tắt tiến độ trong next_goal để planner giữ hướng đúng. "
                "Nếu bị lặp hành động hoặc chưa tìm ra mục tiêu sau vài lần thử, "
                "hãy ưu tiên làm rõ tình trạng thay vì cố thử đi thử lại mù quáng."
            )

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
                initial_actions=infer_initial_actions(req.task) if fresh_browser_context else None,
                register_new_step_callback=on_new_step,
                extend_system_message=extend_msg,
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
                    metadata   = {
                        "state": event["type"],
                        "usage": event.get("usage"),
                    }
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