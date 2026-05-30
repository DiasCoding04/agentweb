"""Luu Gemini API key tu bien moi truong NEW_GEMINI_KEY."""
import os
import sys

from config import save_gemini_api_key, verify_gemini_api_key

key = (os.environ.get("NEW_GEMINI_KEY") or "").strip()
if not key:
    raise SystemExit("Khong co key de luu.")

save_gemini_api_key(key)
ok, msg = verify_gemini_api_key()
if not ok:
    raise SystemExit(msg or "Key khong hop le.")
print("Key hop le — da luu vao local/gemini.key")
