from __future__ import annotations

# server.py — FastAPI backend
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

load_dotenv()
logging.getLogger("browser_use").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Lazy imports để tránh crash nếu thiếu thư viện ──────────────────────────
try:
    from browser_use import Agent, Browser, BrowserConfig

    BROWSER_USE_AVAILABLE = True
except ImportError:
    BROWSER_USE_AVAILABLE = False

try:
    from langchain_core.callbacks import UsageMetadataCallbackHandler
    from langchain_google_genai import ChatGoogleGenerativeAI

    GOOGLE_LLM_AVAILABLE = True
except ImportError:
    GOOGLE_LLM_AVAILABLE = False

HISTORY_FILE = Path("chat_history.json")
GEMINI_API_KEY = "AIzaSyDd1n7gS2iVz7SajY5wZ3WhNHhbDJgLnhA"
MAX_CONTEXT_CHARS = 900
MAX_CONTEXT_MESSAGES = 6
BROWSER_START_TIMEOUT = 15
BROWSER_CONTEXT_TIMEOUT = 10
MAX_AGENT_STEPS = 1200
MAX_AGENT_SECONDS = 4 * 60 * 60
MAX_INPUT_TOKENS = 180000
MAX_ACTIONS_PER_STEP = 10
PLANNER_INTERVAL = 4
MAX_REPEAT_ACTIONS = 4
MAX_FAILS_BEFORE_ASK = 2
DEFAULT_USD_TO_VND = 26000.0
EXCHANGE_RATE_CACHE_SECONDS = 900
EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"
GEMINI_PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
GEMINI_PRICING_UPDATED_AT = "2026-05-27"

MODEL_OPTIONS = [
    {
        "id": "gemini-2.5-flash-lite",
        "label": "Gemini 2.5 Flash-Lite",
        "description": "Nhanh nhất, rẻ nhất cho đa số tác vụ browser.",
    },
    {
        "id": "gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
        "description": "Cân bằng giữa tốc độ, độ chính xác và chi phí.",
    },
    {
        "id": "gemini-2.5-pro",
        "label": "Gemini 2.5 Pro",
        "description": "Mạnh nhất, phù hợp tác vụ khó và dài.",
    },
]
MODEL_IDS = {item["id"] for item in MODEL_OPTIONS}
MODEL_DEFAULTS = {
    "executor_model": "gemini-2.5-flash-lite",
    "planner_model": "gemini-2.5-flash",
}
MODEL_CONFIG = dict(MODEL_DEFAULTS)

MODEL_PRICING_USD_PER_1M = {
    "gemini-2.5-flash-lite": {
        "input": 0.10,
        "output": 0.40,
        "cached": 0.01,
    },
    "gemini-2.5-flash": {
        "input": 0.30,
        "output": 2.50,
        "cached": 0.03,
    },
    "gemini-2.5-pro": {
        "input": 1.25,
        "output": 10.00,
        "cached": 0.125,
    },
}

STUCK_ACTIONS = {
    "scroll_down",
    "scroll_up",
    "click_element",
    "extract_content",
    "search_google",
    "wait",
}

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── State ───────────────────────────────────────────────────────────────────
sessions: dict[str, dict[str, Any]] = {}
browser_instance = None
exchange_rate_cache = {
    "usd_to_vnd": DEFAULT_USD_TO_VND,
    "updated_at": None,
    "source": "fallback",
    "stale": True,
    "fetched_at": None,
}


async def with_timeout(awaitable, timeout_seconds: int, operation_name: str):
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"{operation_name} quá thời gian sau {timeout_seconds} giây") from exc
    except Exception as exc:
        raise RuntimeError(f"{operation_name} thất bại: {exc}") from exc


# ── Chat history helpers ────────────────────────────────────────────────────
def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {}


def save_history(history: dict):
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add_message(chat_id: str, role: str, content: str, metadata: dict | None = None):
    history = load_history()
    if chat_id not in history:
        history[chat_id] = {
            "id": chat_id,
            "title": content[:40] + ("..." if len(content) > 40 else ""),
            "created_at": datetime.now().isoformat(),
            "messages": [],
        }

    message = {
        "role": role,
        "content": content,
        "time": datetime.now().strftime("%H:%M"),
    }
    if metadata is not None:
        message["metadata"] = metadata

    history[chat_id]["messages"].append(message)
    save_history(history)


# ── Config / pricing helpers ────────────────────────────────────────────────
def resolve_model(model_id: str) -> str:
    if model_id not in MODEL_IDS:
        raise ValueError(f"Model không hợp lệ: {model_id}")
    return model_id


def build_config_payload() -> dict:
    return {
        "executor_model": MODEL_CONFIG["executor_model"],
        "planner_model": MODEL_CONFIG["planner_model"],
        "models": MODEL_OPTIONS,
        "max_steps": MAX_AGENT_STEPS,
        "pricing_reference": {
            "source": GEMINI_PRICING_SOURCE,
            "updated_at": GEMINI_PRICING_UPDATED_AT,
        },
    }


def get_model_option(model_id: str) -> dict:
    for item in MODEL_OPTIONS:
        if item["id"] == model_id:
            return item
    return {"id": model_id, "label": model_id, "description": ""}


def make_llm(model: str, callback_handler: UsageMetadataCallbackHandler | None = None):
    callbacks = [callback_handler] if callback_handler else None
    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=GEMINI_API_KEY,
        callbacks=callbacks,
    )


def extract_usage_totals(callback_handler: UsageMetadataCallbackHandler | None) -> dict:
    if not callback_handler:
        return {
            "resolved_models": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        }

    totals = {
        "resolved_models": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
    }

    for model_name, usage in callback_handler.usage_metadata.items():
        details = usage.get("input_token_details") or {}
        cached_tokens = int(details.get("cache_read", 0) or 0)
        totals["resolved_models"].append(model_name)
        totals["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        totals["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        totals["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
        totals["cached_tokens"] += cached_tokens

    return totals


def calculate_cost_usd(model_id: str, input_tokens: int, output_tokens: int, cached_tokens: int) -> float:
    pricing = MODEL_PRICING_USD_PER_1M[model_id]
    paid_input_tokens = max(input_tokens - cached_tokens, 0)
    cost = (
        paid_input_tokens * pricing["input"]
        + output_tokens * pricing["output"]
        + cached_tokens * pricing["cached"]
    ) / 1_000_000
    return round(cost, 6)


def build_role_usage(
    requested_model: str,
    callback_handler: UsageMetadataCallbackHandler | None,
    exchange_rate: float,
) -> dict:
    totals = extract_usage_totals(callback_handler)
    cost_usd = calculate_cost_usd(
        requested_model,
        totals["input_tokens"],
        totals["output_tokens"],
        totals["cached_tokens"],
    )
    return {
        "requested_model": requested_model,
        "resolved_models": totals["resolved_models"],
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cached_tokens": totals["cached_tokens"],
        "total_tokens": totals["total_tokens"],
        "cost_usd": cost_usd,
        "cost_vnd": round(cost_usd * exchange_rate, 2),
    }


def build_usage_summary(
    executor_model: str,
    planner_model: str | None,
    executor_callback: UsageMetadataCallbackHandler,
    planner_callback: UsageMetadataCallbackHandler | None,
    exchange_rate_info: dict,
    elapsed_seconds: int,
    step: int,
    waiting_for_user: bool,
) -> dict:
    rate = float(exchange_rate_info["usd_to_vnd"])
    executor = build_role_usage(executor_model, executor_callback, rate)
    planner = build_role_usage(planner_model, planner_callback, rate) if planner_model else None

    total_input = executor["input_tokens"] + (planner["input_tokens"] if planner else 0)
    total_output = executor["output_tokens"] + (planner["output_tokens"] if planner else 0)
    total_cached = executor["cached_tokens"] + (planner["cached_tokens"] if planner else 0)
    total_tokens = executor["total_tokens"] + (planner["total_tokens"] if planner else 0)
    total_cost_usd = executor["cost_usd"] + (planner["cost_usd"] if planner else 0.0)
    total_cost_vnd = executor["cost_vnd"] + (planner["cost_vnd"] if planner else 0.0)

    return {
        "executor": executor,
        "planner": planner,
        "totals": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cached_tokens": total_cached,
            "total_tokens": total_tokens,
            "cost_usd": round(total_cost_usd, 6),
            "cost_vnd": round(total_cost_vnd, 2),
        },
        "exchange_rate": exchange_rate_info,
        "pricing_reference": {
            "source": GEMINI_PRICING_SOURCE,
            "updated_at": GEMINI_PRICING_UPDATED_AT,
        },
        "elapsed_seconds": elapsed_seconds,
        "step": step,
        "waiting_for_user": waiting_for_user,
    }


def fetch_exchange_rate_sync() -> dict:
    with urllib.request.urlopen(EXCHANGE_RATE_URL, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))

    rate = float(payload["rates"]["VND"])
    now_iso = datetime.now().isoformat()
    return {
        "usd_to_vnd": rate,
        "updated_at": payload.get("time_last_update_utc") or now_iso,
        "source": EXCHANGE_RATE_URL,
        "stale": False,
        "fetched_at": now_iso,
    }


async def get_exchange_rate_info() -> dict:
    now = time.time()
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
        return {
            "usd_to_vnd": DEFAULT_USD_TO_VND,
            "updated_at": None,
            "source": "fallback",
            "stale": True,
            "fetched_at": None,
        }


# ── LLM / context helpers ───────────────────────────────────────────────────
def should_use_planner(task: str) -> bool:
    normalized = task.lower()
    if len(task) >= 160:
        return True

    multi_step_markers = ["rồi", "sau đó", "tiếp tục", "xong thì", "và ", " rồi "]
    matches = sum(1 for marker in multi_step_markers if marker in normalized)
    return matches >= 2


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
        last_content = prior_messages[-1].get("content", "")
        if last_content == current_task:
            prior_messages = prior_messages[:-1]

    if not prior_messages:
        return ""

    trimmed_messages = prior_messages[-MAX_CONTEXT_MESSAGES:]
    recent_messages = trimmed_messages[-2:]
    older_messages = trimmed_messages[:-2]

    lines: list[str] = []

    if older_messages:
        lines.append("Tom tat phien truoc:")
        for msg in older_messages:
            role = "Nguoi dung" if msg.get("role") == "user" else "AI"
            lines.append(f"- {role}: {shorten_text(msg.get('content', ''), 90)}")

    if recent_messages:
        lines.append("Gan day:")
        for msg in recent_messages:
            role = "Nguoi dung" if msg.get("role") == "user" else "AI"
            lines.append(f"- {role}: {shorten_text(msg.get('content', ''), 220)}")

    context = "\n".join(lines).strip()
    return shorten_text(context, MAX_CONTEXT_CHARS) if context else ""


def translate_action_name(action_name: str) -> str:
    translations = {
        "go_to_url": "mở trang web",
        "open_tab": "mở tab mới",
        "switch_tab": "chuyển tab",
        "close_tab": "đóng tab",
        "go_back": "quay lại",
        "refresh_page": "tải lại trang",
        "scroll_down": "cuộn xuống",
        "scroll_up": "cuộn lên",
        "click_element": "bấm vào phần tử",
        "input_text": "nhập nội dung",
        "send_keys": "gửi phím",
        "search_google": "tìm kiếm trên Google",
        "extract_content": "đọc nội dung trang",
        "wait": "chờ trang phản hồi",
        "done": "hoàn tất",
    }
    return translations.get(action_name, action_name.replace("_", " "))


def build_step_goal(action_name: str, fallback_goal: str) -> str:
    goal_map = {
        "go_to_url": "Đang mở trang theo yêu cầu.",
        "open_tab": "Đang tạo tab mới để tiếp tục tác vụ.",
        "switch_tab": "Đang chuyển sang tab phù hợp.",
        "close_tab": "Đang dọn bớt tab không cần thiết.",
        "go_back": "Đang quay lại bước trước đó.",
        "refresh_page": "Đang tải lại trang để cập nhật nội dung.",
        "scroll_down": "Đang cuộn xuống để tìm thêm thông tin hoặc nút cần thao tác.",
        "scroll_up": "Đang cuộn lên để kiểm tra phần trước đó.",
        "click_element": "Đang bấm vào mục cần thao tác.",
        "input_text": "Đang nhập nội dung theo yêu cầu.",
        "send_keys": "Đang gửi phím tắt hoặc xác nhận thao tác.",
        "search_google": "Đang tìm kiếm thông tin trên Google.",
        "extract_content": "Đang đọc và lấy nội dung cần thiết từ trang.",
        "wait": "Đang chờ trang phản hồi.",
        "done": "Tác vụ ở bước này đã hoàn tất.",
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

    translated_actions = [translate_action_name(action_name) for action_name in action_keys[:2]]
    primary_action = action_keys[0]
    goal = build_step_goal(primary_action, fallback_goal)

    return {
        "type": "step",
        "step": step_num,
        "action": ", ".join(translated_actions),
        "goal": goal,
        "text": f"Bước {step_num}: {translated_actions[0]}",
    }


def format_duration_vi(total_seconds: int) -> str:
    hours, remainder = divmod(max(total_seconds, 0), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def build_needs_input_message(primary_action: str, run_state: dict, consecutive_failures: int) -> str | None:
    elapsed_seconds = int(time.monotonic() - run_state["started_at"])
    recent_actions = run_state["recent_actions"]

    if elapsed_seconds >= MAX_AGENT_SECONDS:
        return (
            f"Tác vụ đã chạy khoảng {format_duration_vi(elapsed_seconds)} và cần bạn xác nhận hướng đi tiếp. "
            "Hãy trả lời rõ bước ưu tiên hoặc điều kiện dừng để agent tiếp tục đúng ý bạn."
        )

    if consecutive_failures >= MAX_FAILS_BEFORE_ASK:
        return (
            "Agent đang gặp lỗi liên tiếp khi thao tác trên trang. "
            "Hãy chỉ rõ nút, từ khóa hoặc cách xử lý tiếp theo để tránh thử lặp lại một cách mù quáng."
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


async def close_session(chat_id: str):
    session = sessions.pop(chat_id, None)
    if not session:
        return

    current_agent = session.get("current_agent")
    if current_agent:
        current_agent.stop()

    browser_context = session.get("browser_context")
    if browser_context:
        try:
            await browser_context.close()
        except Exception:
            pass


# ── API models ──────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    chat_id: str
    task: str
    executor_model: str | None = None
    planner_model: str | None = None


class ConfigRequest(BaseModel):
    executor_model: str
    planner_model: str


# ── Endpoints ───────────────────────────────────────────────────────────────
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
    MODEL_CONFIG["planner_model"] = resolve_model(req.planner_model)
    return build_config_payload()


@app.get("/api/history")
async def get_history():
    history = load_history()
    return list(reversed(list(history.values())))


@app.post("/api/new_chat")
async def new_chat():
    chat_id = str(uuid.uuid4())[:8]
    return {"chat_id": chat_id}


@app.delete("/api/history/{chat_id}")
async def delete_chat(chat_id: str):
    history = load_history()
    history.pop(chat_id, None)
    save_history(history)
    await close_session(chat_id)
    return {"ok": True}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Stream phản hồi của AI qua SSE"""

    async def generate() -> AsyncGenerator[str, None]:
        global browser_instance

        def sse(data: dict) -> str:
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        def status(text: str, phase: str) -> str:
            return sse({"type": "status", "text": text, "phase": phase})

        add_message(req.chat_id, "user", req.task)
        yield status("Đã nhận lệnh, đang chuẩn bị xử lý...", "received")

        if not BROWSER_USE_AVAILABLE or not GOOGLE_LLM_AVAILABLE:
            msg = "Thieu thu vien: pip install browser-use langchain-google-genai"
            add_message(req.chat_id, "assistant", msg, {"state": "error"})
            yield sse({"type": "error", "text": msg})
            return

        if not GEMINI_API_KEY:
            msg = "Chua dat GEMINI_API_KEY trong file .env"
            add_message(req.chat_id, "assistant", msg, {"state": "error"})
            yield sse({"type": "error", "text": msg})
            return

        try:
            executor_model = resolve_model(req.executor_model or MODEL_CONFIG["executor_model"])
            planner_model = resolve_model(req.planner_model or MODEL_CONFIG["planner_model"])
        except ValueError as exc:
            msg = str(exc)
            add_message(req.chat_id, "assistant", msg, {"state": "error"})
            yield sse({"type": "error", "text": msg})
            return

        created_browser = False

        if browser_instance is None:
            yield status("Đang khởi động trình duyệt...", "browser")
            try:
                browser_instance = await with_timeout(
                    asyncio.to_thread(
                        Browser,
                        config=BrowserConfig(
                            headless=False,
                            keep_alive=True,
                        ),
                    ),
                    BROWSER_START_TIMEOUT,
                    "Khởi động trình duyệt",
                )
                created_browser = True
            except RuntimeError as exc:
                msg = str(exc)
                add_message(req.chat_id, "assistant", msg, {"state": "error"})
                yield sse({"type": "error", "text": msg})
                return
        else:
            yield status("Đang dùng lại trình duyệt hiện có...", "browser")

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
                add_message(req.chat_id, "assistant", msg, {"state": "error"})
                yield sse({"type": "error", "text": msg})
                return
            session = {
                "context": "",
                "browser_context": browser_context,
                "awaiting_user": False,
                "awaiting_reason": None,
                "current_agent": None,
                "current_run": None,
                "last_usage": None,
            }
            sessions[req.chat_id] = session
        elif session.get("browser_context") is None:
            yield status("Đang khôi phục ngữ cảnh trình duyệt...", "session")
            try:
                session["browser_context"] = await with_timeout(
                    browser_instance.new_context(),
                    BROWSER_CONTEXT_TIMEOUT,
                    "Khôi phục phiên trình duyệt",
                )
            except RuntimeError as exc:
                msg = str(exc)
                add_message(req.chat_id, "assistant", msg, {"state": "error"})
                yield sse({"type": "error", "text": msg})
                return
        else:
            yield status("Đang dùng lại phiên trước đó...", "session")

        if session.get("awaiting_user"):
            yield status("Đang tiếp tục từ phiên đã chờ chỉ dẫn của bạn...", "resume")
        session["awaiting_user"] = False
        session["awaiting_reason"] = None

        history = load_history()
        messages = history.get(req.chat_id, {}).get("messages", [])
        message_context = build_message_context(messages, req.task)
        if message_context:
            session["context"] = message_context
            yield status("Đang nạp ngữ cảnh hội thoại gọn...", "context")
        else:
            session["context"] = ""
            yield status("Không có ngữ cảnh cũ, chạy với lệnh hiện tại...", "context")

        use_planner = should_use_planner(req.task)
        mode_text = "planner" if use_planner else "nhanh"
        executor_option = get_model_option(executor_model)
        planner_option = get_model_option(planner_model)
        yield status(
            f"Đang chọn chế độ {mode_text} với {executor_option['label']} / planner {planner_option['label']}...",
            "mode",
        )

        exchange_rate_info = await get_exchange_rate_info()
        event_queue: asyncio.Queue = asyncio.Queue(maxsize=300)
        executor_callback = UsageMetadataCallbackHandler()
        planner_callback = UsageMetadataCallbackHandler() if use_planner else None
        task_state = {
            "started_at": time.monotonic(),
            "recent_actions": [],
            "awaiting_user": False,
            "question": None,
            "current_step": 0,
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

        agent: Agent | None = None

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

        async def on_step_end(agent_ref: Agent):
            usage_event = {"type": "usage", "usage": current_usage_summary()}
            try:
                event_queue.put_nowait(usage_event)
            except asyncio.QueueFull:
                pass

            if task_state["awaiting_user"]:
                return

            primary_action = task_state["recent_actions"][-1] if task_state["recent_actions"] else "dang_phan_tich"
            question = build_needs_input_message(
                primary_action,
                task_state,
                agent_ref.state.consecutive_failures,
            )
            if question:
                task_state["awaiting_user"] = True
                task_state["question"] = question
                session["awaiting_user"] = True
                session["awaiting_reason"] = question
                session["last_usage"] = current_usage_summary()
                try:
                    event_queue.put_nowait(
                        {
                            "type": "needs_input",
                            "text": question,
                            "usage": current_usage_summary(),
                        }
                    )
                except asyncio.QueueFull:
                    pass
                agent_ref.stop()

        try:
            os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY
            yield status("Agent đang thực thi trên trình duyệt...", "running")

            agent = Agent(
                task=req.task,
                llm=make_llm(executor_model, executor_callback),
                planner_llm=make_llm(planner_model, planner_callback) if use_planner else None,
                planner_interval=PLANNER_INTERVAL,
                max_actions_per_step=MAX_ACTIONS_PER_STEP,
                max_input_tokens=MAX_INPUT_TOKENS,
                max_failures=MAX_FAILS_BEFORE_ASK + 1,
                browser_context=session.get("browser_context"),
                message_context=message_context or None,
                register_new_step_callback=on_new_step,
                extend_system_message=(
                    "Nếu bị lặp hành động hoặc chưa tìm ra mục tiêu sau vài lần thử, hãy ưu tiên làm rõ tình trạng "
                    "thay vì cố thử đi thử lại mù quáng."
                ),
            )
            session["current_agent"] = agent
            session["current_run"] = task_state

            async def run_agent():
                try:
                    result = await agent.run(max_steps=MAX_AGENT_STEPS, on_step_end=on_step_end)
                    session["last_usage"] = current_usage_summary()
                    if task_state["awaiting_user"]:
                        return
                    final = result.final_result() or "Hoan thanh"
                    await event_queue.put(
                        {
                            "type": "done",
                            "text": final,
                            "usage": current_usage_summary(),
                        }
                    )
                except Exception as e:
                    await event_queue.put(
                        {
                            "type": "error",
                            "text": f"Loi: {str(e)}",
                            "usage": current_usage_summary(),
                        }
                    )
                finally:
                    session["current_agent"] = None
                    session["current_run"] = None

            agent_task = asyncio.create_task(run_agent())

            while True:
                event = await event_queue.get()
                yield sse(event)
                if event["type"] in {"done", "error", "needs_input"}:
                    final_text = event["text"]
                    metadata = {
                        "state": event["type"],
                        "usage": event.get("usage"),
                    }
                    add_message(req.chat_id, "assistant", final_text, metadata)

                    refreshed_history = load_history()
                    refreshed_messages = refreshed_history.get(req.chat_id, {}).get("messages", [])
                    session["context"] = build_message_context(refreshed_messages, "")
                    session["last_usage"] = event.get("usage")

                    await agent_task
                    return

        except Exception as e:
            err = f"Loi: {str(e)}"
            add_message(req.chat_id, "assistant", err, {"state": "error"})
            yield sse({"type": "error", "text": err})

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/reset_browser")
async def reset_browser():
    """Đóng và reset browser instance"""
    global browser_instance

    if browser_instance:
        try:
            await browser_instance.close()
        except Exception:
            pass
        browser_instance = None

    for session in sessions.values():
        session["context"] = ""
        session["awaiting_user"] = False
        session["awaiting_reason"] = None
        current_agent = session.get("current_agent")
        if current_agent:
            current_agent.stop()
        session["current_agent"] = None
        session["current_run"] = None
        session.pop("browser_context", None)

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    print("\nServer chay tai: http://localhost:8000\n")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
