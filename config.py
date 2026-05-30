"""Cấu hình Gemini API — đọc từ file local sau lần thiết lập đầu."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
KEY_FILE = ROOT / "local" / "gemini.key"


def _read_key_file() -> str:
    if not KEY_FILE.exists():
        return ""
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def get_gemini_api_key() -> str:
    return (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or _read_key_file()
    ).strip()


def save_gemini_api_key(key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("API key trong.")
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key, encoding="utf-8")


def verify_gemini_api_key(key: str | None = None) -> tuple[bool, str]:
    """Kiểm tra key có gọi được Gemini không. Trả (ok, thông báo tiếng Việt)."""
    api_key = (key or get_gemini_api_key()).strip()
    if not api_key:
        return False, (
            "Chưa có API key Gemini. Chạy **Setup Gemini Key.cmd** một lần "
            "(lấy key miễn phí tại https://aistudio.google.com/apikey)."
        )

    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash-lite",
            google_api_key=api_key,
        )
        llm.invoke("OK")
        return True, ""
    except Exception as exc:
        err = str(exc)
        if "leaked" in err.lower() or "403" in err:
            return False, (
                "API key Gemini không dùng được (bị Google vô hiệu hoá hoặc đã lộ). "
                "Tạo key mới tại https://aistudio.google.com/apikey rồi chạy **Setup Gemini Key.cmd**."
            )
        if "API key not valid" in err or "400" in err:
            return False, "API key Gemini không hợp lệ. Chạy **Setup Gemini Key.cmd** để cập nhật."
        return False, f"Không kết nối được Gemini: {err[:200]}"
