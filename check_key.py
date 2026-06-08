"""Kiểm tra Gemini API key — exit 0 nếu OK, 1 nếu lỗi (in ra stderr)."""
from config import verify_gemini_api_key, use_vertex

if use_vertex():
    print("Su dung Vertex AI. Bo qua check key Gemini.")
    raise SystemExit(0)

ok, msg = verify_gemini_api_key()
if not ok:
    raise SystemExit(msg or "API key khong hop le.")
print("API key OK")
