"""Kiểm tra cấu hình Vertex AI hoạt động không."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import (
    get_vertex_project,
    get_vertex_location,
    get_vertex_credentials,
    use_vertex,
    verify_api_key,
    PROVIDER_REGISTRY,
)


def main():
    print("=== KIỂM TRA VERTEX AI ===\n")

    if not use_vertex():
        print("KHÔNG phát hiện cấu hình Vertex AI.")
        print("Kiểm tra các biến môi trường:")
        print("  - GOOGLE_CLOUD_PROJECT")
        print("  - GOOGLE_CLOUD_LOCATION (mặc định: us-central1)")
        print("  - GOOGLE_APPLICATION_CREDENTIALS (đường dẫn service_account.json)")
        print("  Hoặc file: local/service_account.json")
        return 1

    project = get_vertex_project()
    location = get_vertex_location()
    creds = get_vertex_credentials()

    print(f"Project   : {project}")
    print(f"Location  : {location}")
    print(f"Credentials: {creds}")
    print()

    # Kiểm tra từng provider
    for provider_id in PROVIDER_REGISTRY:
        ok, msg = verify_api_key(provider_id)
        status = "✅ OK" if ok else "❌ FAIL"
        print(f"[{status}] {PROVIDER_REGISTRY[provider_id]['label']} ({provider_id})")
        if not ok:
            print(f"    Lỗi: {msg}")

    return 0 if all(verify_api_key(p)[0] for p in PROVIDER_REGISTRY) else 1


if __name__ == "__main__":
    raise SystemExit(main())
