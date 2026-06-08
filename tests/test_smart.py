"""Logic tests for the Smart Agent Optimization features.

Runs without a live browser or LLM (LLM calls are mocked). Covers:
  - adaptive planner interval (req #2)
  - smart_scroll registration + JS validity (req #1)
  - planner-authored generalization helpers (req #3)
  - rate-to-learn auto-promotion + dislike decay/forget (req #4)
  - resync after incident recovery + adaptive recovery budget (req #5)
  - context-aware matching + workflow composition planning (req #6)

Run:  python tests/test_smart.py     (or: pytest tests/test_smart.py)
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def _isolate_storage():
    """Point persistence at a throwaway temp dir so tests never touch real data."""
    tmp = Path(tempfile.mkdtemp())
    server.SITE_PROFILES_FILE = tmp / "site_profiles.json"
    server.AUTO_LEARNING_FILE = tmp / "auto_learning.json"
    return tmp


# ── Fakes ─────────────────────────────────────────────────────────────────────
class _FakeLocator:
    def __init__(self, text):
        self._text = text

    async def inner_text(self, timeout=2500):
        return self._text


class FakePage:
    def __init__(self, url="", title="", text=""):
        self.url = url
        self._title = title
        self._text = text

    async def title(self):
        return self._title

    def locator(self, _selector):
        return _FakeLocator(self._text)


class FakeCtx:
    def __init__(self, page):
        self._page = page

    async def get_current_page(self):
        return self._page


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, _prompt):
        return _FakeResp(self._content)


# ── Tests ─────────────────────────────────────────────────────────────────────
def test_resolve_planner_interval():
    assert server.resolve_planner_interval("theo dõi inbox liên tục cả ngày") == server.PLANNER_INTERVAL_HARD
    assert server.resolve_planner_interval("monitor the dashboard and repeat") == server.PLANNER_INTERVAL_HARD
    assert server.resolve_planner_interval("đăng nhập rồi soạn một email") == server.PLANNER_INTERVAL_NORMAL
    print("PASS test_resolve_planner_interval")


def test_smart_scroll_registered():
    actions = server.controller.registry.registry.actions
    assert "smart_scroll" in actions
    assert "scroll_element" in actions
    assert "list_scrollable_regions" in actions
    # JS strings must be present and balanced-ish (start with arrow fn)
    assert server.SMART_SCROLL_PAGE_JS.strip().startswith("(opts)")
    assert server.SMART_SCROLL_ELEMENT_JS.strip().startswith("(el, opts)")
    assert server.LIST_SCROLLABLE_REGIONS_JS.strip().startswith("()")
    print("PASS test_smart_scroll_registered")


def test_describe_smart_scroll():
    assert "toàn trang" in server.describe_smart_scroll("page", "down")
    assert "vùng cuộn riêng" in server.describe_smart_scroll("region:div", "down")
    assert "tầm nhìn" in server.describe_smart_scroll("region-reveal:ul", "down", target_text="Chi nhánh A")
    assert "tầm nhìn" in server.describe_smart_scroll("reveal", "down", target_text="Chi nhánh A")
    assert "Không tìm thấy" in server.describe_smart_scroll("notfound", "down", target_text="X")
    wheel_msg = server.describe_smart_scroll("wheel:ul", "down", index=8)
    assert "wheel" in wheel_msg.lower() or "chuột" in wheel_msg
    assert "thay đổi" in wheel_msg
    # Honest "no movement" outcome must NOT claim a successful scroll.
    msg = server.describe_smart_scroll("noscroll", "down")
    assert "KHÔNG cuộn" in msg
    assert "Đã cuộn" not in msg
    assert "list_scrollable_regions" in msg
    print("PASS test_describe_smart_scroll")


def test_format_scrollable_regions_report():
    empty = server.format_scrollable_regions_report([])
    assert "Không phát hiện" in empty
    sample = server.format_scrollable_regions_report([
        {"tag": "page", "canDown": True, "canUp": False, "scrollTop": 0, "maxScroll": 1200, "area": 900000},
        {"tag": "ul", "role": "listbox", "canDown": True, "canUp": True,
         "scrollTop": 100, "maxScroll": 400, "area": 18000, "sample": "Chi nhánh A Chi nhánh B"},
    ])
    assert "TOÀN TRANG" in sample
    assert "<ul>" in sample
    assert "smart_scroll" in sample
    print("PASS test_format_scrollable_regions_report")


def test_json_and_sanitize():
    fence = chr(96) * 3
    obj = server._extract_json_object("noise " + fence + "json\n{\"a\": 1}\n" + fence)
    assert obj == {"a": 1}
    steps = server.sanitize_llm_workflow_steps([
        {"go_to_url": {"url": "x"}},
        {"not_an_action": {}},
        {"click_element_by_text": {"text": "Go"}},
        "garbage",
    ])
    assert steps == [{"go_to_url": {"url": "x"}}, {"click_element_by_text": {"text": "Go"}}]
    print("PASS test_json_and_sanitize")


def test_adaptive_recovery_budget():
    assert server.adaptive_recovery_budget([0] * 6) == 2
    assert server.adaptive_recovery_budget([0] * 16) == 4
    assert server.adaptive_recovery_budget([0] * 40) == 5  # capped
    print("PASS test_adaptive_recovery_budget")


def test_context_aware_scoring():
    _isolate_storage()
    # variable-less workflow → eligible for fuzzy scoring + context boost
    wf = {
        "workflow_id": "w1",
        "workflow_name": "Mo hop thu",
        "user_profile": "Default",
        "prompt_template": "mo hop thu den",
        "variables": [],
        "steps": [{"go_to_url": {"url": "https://mail.google.com"}}, {"done": {"text": "ok"}}],
        "metadata": {"intent": "mo hop thu", "workflow_scope": "general"},
        "stats": {}, "confidence": 0.8,
    }
    base = server.score_workflow_candidate(wf, "mail.google.com", "mo hop thu den")[0]
    boosted = server.score_workflow_candidate(wf, "mail.google.com", "mo hop thu den",
                                              "https://mail.google.com/u/0/")[0]
    assert boosted > base, f"expected context boost, got base={base} boosted={boosted}"
    print(f"PASS test_context_aware_scoring (base={base:.3f} boosted={boosted:.3f})")


def test_auto_learning_promotion_and_match():
    _isolate_storage()
    gen = {
        "prompt_template": "tim kiem {search_query} tren web",
        "variables": ["search_query"],
        "steps": [
            {"go_to_url": {"url": "https://example.com"}},
            {"input_text": {"text": "{search_query}"}},
            {"click_element_by_text": {"text": "Search"}},
            {"done": {"text": "done"}},
        ],
        "stats": {}, "confidence": 0.72, "metadata": {},
    }
    domain = "example.com"
    sig = server.auto_learning_signature(gen, "tim kiem abc tren web")

    c1 = server.record_auto_learning(domain, sig, "tim kiem abc tren web", gen)
    assert c1["satisfied_count"] == 1 and not c1["promoted"]
    c2 = server.record_auto_learning(domain, sig, "tim kiem xyz tren web", gen)
    assert c2["satisfied_count"] == 2

    wid = server.promote_auto_learned_workflow(domain, gen, "tim kiem abc tren web", "Default")
    assert wid, "promotion should succeed"
    server.mark_auto_learning_promoted(domain, sig, wid)

    wf = server.load_site_profiles()[domain][0]
    assert wf["metadata"]["auto_learned"] is True

    # matcher now resolves the learned pattern (0-LLM path)
    match = server.find_matching_workflow("Default", "tim kiem hello tren web")
    assert match and match["workflow"]["workflow_id"] == wid

    # dislike decays confidence
    before = server.load_site_profiles()[domain][0]["confidence"]
    server.adjust_workflow_confidence(wid, -0.2, reason="user_dislike")
    after = server.load_site_profiles()[domain][0]["confidence"]
    assert after < before
    print("PASS test_auto_learning_promotion_and_match")


def test_forget_auto_learning():
    _isolate_storage()
    gen = {"prompt_template": "lam viec abc", "variables": [],
           "steps": [{"go_to_url": {"url": "https://x.com"}}, {"done": {"text": "ok"}}],
           "stats": {}, "confidence": 0.7, "metadata": {}}
    sig = server.auto_learning_signature(gen, "lam viec abc")
    server.record_auto_learning("x.com", sig, "lam viec abc", gen)
    server.forget_auto_learning("x.com", sig)
    store = server.load_auto_learning()
    sigs = [c["signature"] for c in store.get("x.com", [])]
    assert sig not in sigs
    print("PASS test_forget_auto_learning")


def test_resync_replay_index():
    page = FakePage(url="https://example.com/page2")
    ctx = FakeCtx(page)
    match = {"domain": "example.com", "variables": {}}  # no variables → terminal not verified
    replay_plan = [
        {"action": {"input_text": {"text": "hi"}}, "capability": "fill", "source_index": 1},
        {"action": {"go_to_url": {"url": "https://example.com/page2"}}, "capability": "navigate", "source_index": 2},
        {"action": {"click_element_by_text": {"text": "Next"}}, "capability": "activate", "source_index": 3},
    ]
    # recovered at step 1; step 2 is a navigate already satisfied by current URL → skip to step 3
    result = asyncio.run(server.resync_replay_index(match, replay_plan, ctx, 1))
    assert result["resume_idx"] == 3, result
    assert result["skipped"] == 1, result
    assert result["done"] is False
    print("PASS test_resync_replay_index")


def test_composition_planning():
    _isolate_storage()
    profiles = {
        "mail.google.com": [{
            "workflow_id": "wfmail", "workflow_name": "Gui email", "user_profile": "Default",
            "prompt_template": "gui email cho {recipient}", "variables": ["recipient"],
            "steps": [{"go_to_url": {"url": "https://mail.google.com"}}, {"done": {"text": "ok"}}],
            "metadata": {"intent": "gui email"}, "stats": {}, "confidence": 0.8,
        }],
        "drive.google.com": [{
            "workflow_id": "wfdrive", "workflow_name": "Tai len Drive", "user_profile": "Default",
            "prompt_template": "tai file {filename} len drive", "variables": ["filename"],
            "steps": [{"go_to_url": {"url": "https://drive.google.com"}}, {"done": {"text": "ok"}}],
            "metadata": {"intent": "upload"}, "stats": {}, "confidence": 0.8,
        }],
    }
    server.save_site_profiles(profiles)

    catalog = server.build_workflow_catalog("Default")
    assert len(catalog) == 2

    server.make_llm = lambda *a, **k: _FakeLLM(
        '{"steps": [{"workflow_id": "wfmail", "task": "gui email cho sep"}, '
        '{"workflow_id": "wfdrive", "task": "tai file bc.pdf len drive"}, '
        '{"workflow_id": "NOPE", "task": "x"}]}'
    )
    plan = asyncio.run(server.plan_workflow_composition("viec lon", catalog, "gemini-3.5-flash"))
    assert [p["workflow_id"] for p in plan] == ["wfmail", "wfdrive"], plan
    print("PASS test_composition_planning")


def test_composition_needs_agent_continuation():
    task = "tong hop 10 thong tin chung khoan hom qua roi tao 1 file google doc"
    plan = [{"workflow_id": "x", "task": "vao google tim thong tin chung khoan"}]
    assert server.composition_needs_agent_continuation(task, plan) is True
    assert server.composition_needs_agent_continuation("mo youtube", plan) is False
    print("PASS test_composition_needs_agent_continuation")


def test_orchestrator_helpers():
    original = "tim google roi tong hop roi tao doc"
    completed = [{"kind": "workflow", "summary": "Da mo google", "workflow_name": "Tim Google"}]
    sub = server.orchestrator_agent_sub_task(original, completed)
    assert "tim google" in sub.lower()
    assert "Da mo google" in sub
    summary = server.build_orchestrator_completed_summary(completed)
    assert "workflow" in summary.lower() or "Tim Google" in summary
    assert server.workflow_phase_covers_task(
        "Vào Google tìm tin tức hôm nay",
        "Vào Google tìm tin tức hôm nay",
    )
    assert not server.orchestrator_should_finish_after_workflow(
        "Vào Google tìm tin tức hôm nay",
        "Vào Google tìm tin tức hôm nay",
        [{"kind": "workflow", "summary": "ok", "sub_task": "Vào Google tìm tin tức hôm nay"}],
        use_orchestrator_planner=True,
    )
    assert not server.orchestrator_should_finish_after_workflow(
        "tim google roi tong hop roi tao doc",
        "tim google",
        [{"kind": "workflow", "summary": "ok", "sub_task": "tim google"}],
        use_orchestrator_planner=True,
    )
    print("PASS test_orchestrator_helpers")


def test_orchestrator_stop_guard():
    task = "tong hop gia vang roi tao google doc"
    phases = [
        {"kind": "workflow", "summary": "Da tim gia vang tren Google"},
        {"kind": "agent", "summary": "Khong the tao Google Doc vi can dang nhap", "sub_task": "tao doc"},
        {"kind": "agent", "summary": "Khong the tao Google Doc vi can dang nhap", "sub_task": "tao doc"},
    ]
    assert server.agent_signals_blocked("Không thể tạo Google Doc vì cần đăng nhập")
    blocked = server.guard_orchestrator_route(
        {"action": "agent", "sub_task": "tao google doc"},
        original_task=task,
        completed_phases=phases,
        latest_agent_text=phases[-1]["summary"],
    )
    assert blocked["action"] == "done"
    assert "agent_blocked" in blocked.get("reason", "") or "Dừng" in blocked.get("sub_task", "")

    loop_phases = [
        {"kind": "agent", "summary": "Dang thu tao google doc nhung chua xong"},
        {"kind": "agent", "summary": "Dang thu tao google doc nhung chua xong"},
    ]
    looping, reason = server.detect_orchestrator_agent_loop(
        loop_phases, "agent", "tao google doc ghi lai",
    )
    assert looping
    assert reason == "orchestrator_agent_repeat_summary"

    max_phases = [{"kind": "agent", "summary": f"try {i}"} for i in range(3)]
    looping3, reason3 = server.detect_orchestrator_agent_loop(max_phases, "agent", "tiep tuc")
    assert looping3
    assert reason3 == "orchestrator_max_agent_phases"

    guarded = server.guard_orchestrator_route(
        {"action": "agent", "sub_task": "tao google doc"},
        original_task=task,
        completed_phases=max_phases,
    )
    assert guarded["action"] == "done"
    print("PASS test_orchestrator_stop_guard")


def test_relaxed_template_match_optional_tai():
    _isolate_storage()
    profiles = {
        "unknown_domain": [{
            "workflow_id": "wfweather",
            "workflow_name": "Tự học: Kiểm tra thời tiết Hồ Chí Minh",
            "user_profile": "Default",
            "prompt_template": "Kiểm tra thời tiết tại {location}",
            "variables": ["location"],
            "steps": [{"search_google": {"query": "thời tiết {location}"}}, {"done": {"text": "ok"}}],
            "metadata": {
                "intent": "weather",
                "examples": ["Kiểm tra thời tiết Hồ Chí Minh", "Kiểm tra thời tiết tại {location}"],
            },
            "stats": {"success_count": 1, "fail_count": 0},
            "confidence": 0.82,
        }],
    }
    server.save_site_profiles(profiles)
    match = server.find_matching_workflow("Default", "Kiểm tra thời tiết Hồ Chí Minh")
    assert match, "should match without 'tại' in user prompt"
    assert match["match_type"] in ("template_relaxed", "example_literal"), match
    assert "ho chi minh" in server.semantic_prompt(match["variables"].get("location", ""))
    route = server.try_fast_workflow_route("Default", "Kiểm tra thời tiết Hồ Chí Minh")
    assert route and route.get("action") == "workflow", route
    print("PASS test_relaxed_template_match_optional_tai")


def test_accent_insensitive_workflow_match():
    _isolate_storage()
    profiles = {
        "www.google.com": [{
            "workflow_id": "wfgoogle",
            "workflow_name": "Tim Google",
            "user_profile": "Default",
            "prompt_template": "Vào Google tìm {param}",
            "variables": ["param"],
            "steps": [{"go_to_url": {"url": "https://www.google.com"}}, {"done": {"text": "ok"}}],
            "metadata": {"intent": "Vào Google tìm {param}"},
            "stats": {"success_count": 2, "fail_count": 0},
            "confidence": 0.8,
        }],
    }
    server.save_site_profiles(profiles)
    match = server.find_matching_workflow("Default", "Vao Google tim tin tuc hom nay")
    assert match and match["variables"].get("param") == "tin tuc hom nay", match
    route = server.try_fast_workflow_route("Default", "vao google tim bao moi")
    assert route and route.get("action") == "workflow", route
    print("PASS test_accent_insensitive_workflow_match")


def test_split_task_and_capability_card():
    segs = server.split_task_into_segments(
        "tong hop 10 tin chung khoan roi tao 1 file google doc"
    )
    assert len(segs) >= 2
    card = server.build_workflow_capability_card({
        "workflow_id": "wf1",
        "workflow_name": "Tim Google",
        "prompt_template": "Vao Google tim {param}",
        "variables": ["param"],
        "steps": [
            {"go_to_url": {"url": "https://www.google.com"}},
            {"input_text": {"index": 1, "text": "{param}"}},
            {"extract_content": {"goal": "lay ket qua"}},
        ],
        "metadata": {"intent": "Vao Google tim {param}"},
        "stats": {"success_count": 3, "fail_count": 0},
        "confidence": 0.85,
    }, "www.google.com")
    assert card["can_do"]
    assert any("Doc" in x or "doc" in x.lower() for x in card["cannot_do"])
    print("PASS test_split_task_and_capability_card")


def test_segment_workflow_route():
    _isolate_storage()
    profiles = {
        "www.google.com": [{
            "workflow_id": "wfgoogle",
            "workflow_name": "Tim Google",
            "user_profile": "Default",
            "prompt_template": "Vao Google tim {param}",
            "variables": ["param"],
            "steps": [{"go_to_url": {"url": "https://www.google.com"}}, {"done": {"text": "ok"}}],
            "metadata": {"intent": "Vao Google tim {param}"},
            "stats": {"success_count": 2, "fail_count": 0},
            "confidence": 0.8,
        }],
    }
    server.save_site_profiles(profiles)
    cands = server.infer_workflow_subtask_candidates(
        "tong hop 10 thong tin chung khoan hom qua roi tao google doc",
        [],
    )
    route = server.try_segment_workflow_route("Default", cands)
    assert route and route.get("action") == "workflow", route
    print("PASS test_segment_workflow_route")


def test_resolve_orchestrator_ai_decides():
    """Routing must go through AI planner — not mechanical fast_route."""
    _isolate_storage()
    profiles = {
        "www.google.com": [{
            "workflow_id": "wfgoogle",
            "workflow_name": "Tim Google",
            "user_profile": "Default",
            "prompt_template": "Vao Google tim {param}",
            "variables": ["param"],
            "steps": [{"go_to_url": {"url": "https://www.google.com"}}, {"done": {"text": "ok"}}],
            "metadata": {"intent": "search"},
            "stats": {"success_count": 2, "fail_count": 0},
            "confidence": 0.8,
        }],
    }
    server.save_site_profiles(profiles)
    catalog = server.build_workflow_catalog("Default")
    planner_calls: list[str] = []

    async def fake_plan(*_a, **_k):
        planner_calls.append("called")
        return {
            "action": "workflow",
            "sub_task": "Vao Google tim bao",
            "workflow_id": "wfgoogle",
            "variables": {"param": "bao"},
            "reason": "planner_workflow",
            "hint": "",
        }

    original_plan = server.plan_next_orchestrator_phase
    server.plan_next_orchestrator_phase = fake_plan
    try:
        route = asyncio.run(server.resolve_orchestrator_route(
            "Vao Google tim bao",
            [],
            "Default",
            catalog,
            "",
            "",
            "gemini-3.5-flash",
        ))
    finally:
        server.plan_next_orchestrator_phase = original_plan
    assert planner_calls == ["called"], "fast_route must not bypass AI planner"
    assert route.get("action") == "workflow", route
    assert route.get("reason") == "planner_workflow"
    print("PASS test_resolve_orchestrator_ai_decides")


def test_plan_next_orchestrator_phase():
    catalog = [
        {
            "workflow_id": "wf1",
            "name": "Tim kiem",
            "intent": "google search",
            "variables": ["query"],
            "prompt_template": "tim {query}",
            "can_do": ["Tim kiem Google"],
            "cannot_do": ["Khong tao Doc"],
            "step_summary": "mo google -> tim",
            "reliability": "on dinh",
            "examples": [],
        },
    ]
    server.make_llm = lambda *a, **k: _FakeLLM(
        '{"action":"workflow","sub_task":"tim hello tren google","workflow_id":"wf1","reason":"known"}'
    )
    route = asyncio.run(server.plan_next_orchestrator_phase(
        "tim hello tren google roi tong hop",
        [],
        catalog,
        "gemini-3.5-flash",
    ))
    assert route["action"] == "workflow"
    assert route["workflow_id"] == "wf1"
    server.make_llm = lambda *a, **k: _FakeLLM('{"action":"done","sub_task":"Xong","reason":"all_done"}')
    done = asyncio.run(server.plan_next_orchestrator_phase("x", [{"kind": "agent", "summary": "ok"}], catalog, "m"))
    assert done["action"] == "done"
    print("PASS test_plan_next_orchestrator_phase")


def test_scroll_progress_gating():
    import time as _time
    base = {
        "started_at": _time.monotonic(),
        "recent_actions": ["smart_scroll"] * 4,
    }
    # Making progress (scroll_misses=0) -> must NOT interrupt the agent.
    state_ok = {**base, "scroll_misses": 0}
    assert server.build_needs_input_message("smart_scroll", state_ok, 0) is None
    # Genuinely stuck (scroll positions unchanged repeatedly) -> ask for help.
    state_stuck = {**base, "scroll_misses": server.SCROLL_STUCK_MISSES}
    msg = server.build_needs_input_message("smart_scroll", state_stuck, 0)
    assert msg and "không di chuyển" in msg
    print("PASS test_scroll_progress_gating")


ALL_TESTS = [
    test_resolve_planner_interval,
    test_smart_scroll_registered,
    test_describe_smart_scroll,
    test_format_scrollable_regions_report,
    test_composition_needs_agent_continuation,
    test_orchestrator_helpers,
    test_orchestrator_stop_guard,
    test_resolve_orchestrator_ai_decides,
    test_relaxed_template_match_optional_tai,
    test_accent_insensitive_workflow_match,
    test_split_task_and_capability_card,
    test_segment_workflow_route,
    test_plan_next_orchestrator_phase,
    test_scroll_progress_gating,
    test_json_and_sanitize,
    test_adaptive_recovery_budget,
    test_context_aware_scoring,
    test_auto_learning_promotion_and_match,
    test_forget_auto_learning,
    test_resync_replay_index,
    test_composition_planning,
]


def main() -> int:
    failures = 0
    for test in ALL_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            print(f"FAIL {test.__name__}: {exc}")
            traceback.print_exc()
    total = len(ALL_TESTS)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
