from __future__ import annotations

# server.py — FastAPI backend (optimised)
import asyncio
import json
import logging
import os
import shutil
import socket
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

try:
    import psutil
except ImportError:
    psutil = None

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
    from browser_use.browser import browser as browser_use_browser_module
    from browser_use.browser import chrome as browser_use_chrome_module
    BROWSER_USE_AVAILABLE = True
except ImportError:
    Agent = Any  # type: ignore[misc, assignment]
    Browser = Any  # type: ignore[misc, assignment]
    BrowserConfig = Any  # type: ignore[misc, assignment]
    Controller = Any
    ActionResult = Any
    BrowserContext = Any
    browser_use_browser_module = Any
    browser_use_chrome_module = Any
    BROWSER_USE_AVAILABLE = False

try:
    from langchain_core.callbacks import UsageMetadataCallbackHandler
    from langchain_google_genai import ChatGoogleGenerativeAI
    GOOGLE_LLM_AVAILABLE = True
except ImportError:
    UsageMetadataCallbackHandler = Any  # type: ignore[misc, assignment]
    ChatGoogleGenerativeAI = Any  # type: ignore[misc, assignment]
    GOOGLE_LLM_AVAILABLE = False


def configure_browser_use_chrome_args() -> None:
    if not BROWSER_USE_AVAILABLE:
        return
    # browser-use defaults to --no-startup-window, which is useful for pure
    # automation but makes local profile runs look like "nothing happens".
    hidden_window_args = {"--no-startup-window"}
    for module in (browser_use_chrome_module, browser_use_browser_module):
        args = getattr(module, "CHROME_ARGS", None)
        if isinstance(args, list):
            args[:] = [arg for arg in args if arg not in hidden_window_args]


configure_browser_use_chrome_args()

# ── Smart scroll JS (page vs inner-region auto-detection) ─────────────────────
# Shared helpers: detect a genuinely scrollable element, walk up to the nearest
# scrollable ancestor, and compute a sensible step size from the viewport.
_SCROLL_HELPERS_JS = """
  function isScrollable(node, horiz) {
    if (!node || node.nodeType !== 1) return false;
    const style = window.getComputedStyle(node);
    const ov = horiz ? (style.overflowX || style.overflow) : (style.overflowY || style.overflow);
    const canOverflow = ov === 'scroll' || ov === 'auto' || ov === 'overlay';
    const extra = horiz ? (node.scrollWidth > node.clientWidth + 2) : (node.scrollHeight > node.clientHeight + 2);
    return canOverflow && extra;
  }
  function findScrollable(node, horiz) {
    let cur = node;
    while (cur && cur !== document.body && cur !== document.documentElement) {
      if (isScrollable(cur, horiz)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
  function stepSize(amount, horiz) {
    const vh = window.innerHeight || 800, vw = window.innerWidth || 1200;
    return (amount && amount > 0) ? amount : Math.round((horiz ? vw : vh) * 0.8);
  }
"""

# Shared direction-aware movement helpers (work for both element- and page-level).
_SCROLL_MOVE_JS = """
  function scrollerOf(t) {
    return (t === window || t === document) ? (document.scrollingElement || document.documentElement) : t;
  }
  function curPos(t, horiz) { const e = scrollerOf(t); return horiz ? e.scrollLeft : e.scrollTop; }
  function canScrollDir(t, horiz, neg) {
    const e = scrollerOf(t);
    if (horiz) return neg ? (e.scrollLeft > 1) : (e.scrollLeft + e.clientWidth < e.scrollWidth - 1);
    return neg ? (e.scrollTop > 1) : (e.scrollTop + e.clientHeight < e.scrollHeight - 1);
  }
  function applyScroll(t, step, horiz) {
    if (t === window) { horiz ? window.scrollBy(step, 0) : window.scrollBy(0, step); }
    else { horiz ? t.scrollBy(step, 0) : t.scrollBy(0, step); }
  }
  // Scroll the target if it actually moves; returns true when scroll position changed.
  function tryScroll(t, step, horiz) {
    const before = curPos(t, horiz);
    applyScroll(t, step, horiz);
    return Math.abs(curPos(t, horiz) - before) > 0.5;
  }
"""

# element_handle.evaluate(fn, opts) -> fn(element, opts)
SMART_SCROLL_ELEMENT_JS = "(el, opts) => {\n" + _SCROLL_HELPERS_JS + _SCROLL_MOVE_JS + """
  const horiz = !!opts.horizontal, neg = !!opts.negative;
  let step = stepSize(opts.amount, horiz);
  step = neg ? -Math.abs(step) : Math.abs(step);
  let target = isScrollable(el, horiz) ? el : findScrollable(el, horiz);
  if (target && tryScroll(target, step, horiz)) {
    return 'region:' + (target.tagName || '').toLowerCase();
  }
  if (tryScroll(window, step, horiz)) return 'page';
  return 'noscroll';
}"""

# page.evaluate(fn, opts) -> fn(opts): handles target_text reveal + auto region detection.
# Strategy: find every genuinely-scrollable, in-viewport region; try the largest ones first,
# then the page itself; return the first that ACTUALLY moves (so we never fake a scroll).
SMART_SCROLL_PAGE_JS = "(opts) => {\n" + _SCROLL_HELPERS_JS + _SCROLL_MOVE_JS + """
  const horiz = !!opts.horizontal, neg = !!opts.negative;
  let step = stepSize(opts.amount, horiz);
  step = neg ? -Math.abs(step) : Math.abs(step);
  const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;

  const needle = (opts.targetText || '').trim().toLowerCase();
  if (needle) {
    const nodes = Array.from(document.querySelectorAll('body *'));
    let found = null;
    for (const node of nodes) {
      const txt = (node.textContent || '').trim().toLowerCase();
      if (txt && txt.includes(needle)) { found = node; }
    }
    if (found) {
      let changed = true;
      while (changed) {
        changed = false;
        for (const child of found.children) {
          if ((child.textContent || '').toLowerCase().includes(needle)) { found = child; changed = true; break; }
        }
      }
      try { found.scrollIntoView({block:'center', inline:'center'}); } catch (e) { found.scrollIntoView(); }
      return 'reveal';
    }
    return 'notfound';
  }

  // Collect candidate scrollable regions visible in the viewport, largest first.
  const cands = [];
  const all = Array.from(document.querySelectorAll('body *'));
  for (const node of all) {
    if (!isScrollable(node, horiz)) continue;
    const r = node.getBoundingClientRect();
    const visW = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    const visH = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    const area = visW * visH;
    if (area < 600) continue;             // ignore tiny/hidden scrollers (dropdowns ~200x300 ok)
    cands.push({ node: node, area: area });
  }
  cands.sort((a, b) => b.area - a.area);

  const pageDominant = canScrollDir(window, horiz, neg) &&
    (horiz ? (scrollerOf(window).scrollWidth > vw + 2) : (scrollerOf(window).scrollHeight > vh + 2));

  // Try big inner regions first; if the page is the dominant scroller and beats the
  // biggest region in area, give the page first try instead.
  const order = [];
  if (pageDominant && (cands.length === 0 || (vw * vh) >= cands[0].area)) order.push(window);
  for (const c of cands) order.push(c.node);
  if (!order.includes(window)) order.push(window);

  for (const t of order) {
    if (!canScrollDir(t, horiz, neg)) continue;
    if (tryScroll(t, step, horiz)) {
      return (t === window) ? 'page' : ('region:' + ((t.tagName || '').toLowerCase()));
    }
  }
  return 'noscroll';
}"""


# Compact signature of every meaningful scroll position on the page. Used to detect
# whether repeated scroll actions are actually making progress (vs. genuinely stuck).
SCROLL_SIGNATURE_JS = """
() => {
  const se = document.scrollingElement || document.documentElement;
  let sig = Math.round(se.scrollTop) + ',' + Math.round(se.scrollLeft);
  const regs = [];
  for (const n of Array.from(document.querySelectorAll('body *'))) {
    const st = window.getComputedStyle(n);
    const oy = st.overflowY || st.overflow, ox = st.overflowX || st.overflow;
    const sy = (oy === 'auto' || oy === 'scroll' || oy === 'overlay') && n.scrollHeight > n.clientHeight + 2;
    const sx = (ox === 'auto' || ox === 'scroll' || ox === 'overlay') && n.scrollWidth > n.clientWidth + 2;
    if (sy || sx) regs.push(n);
  }
  regs.sort((a, b) => (b.clientWidth * b.clientHeight) - (a.clientWidth * a.clientHeight));
  for (let i = 0; i < Math.min(6, regs.length); i++) {
    sig += '|' + Math.round(regs[i].scrollTop) + ',' + Math.round(regs[i].scrollLeft);
  }
  return sig;
}
"""

# Enumerate scrollable regions so the agent knows WHERE to scroll before verifying content.
LIST_SCROLLABLE_REGIONS_JS = """
() => {
  const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
  const regions = [];
  const seen = new Set();

  function canScrollY(n) {
    const st = window.getComputedStyle(n);
    const oy = st.overflowY || st.overflow;
    return (oy === 'auto' || oy === 'scroll' || oy === 'overlay') && n.scrollHeight > n.clientHeight + 2;
  }
  function canScrollX(n) {
    const st = window.getComputedStyle(n);
    const ox = st.overflowX || st.overflow;
    return (ox === 'auto' || ox === 'scroll' || ox === 'overlay') && n.scrollWidth > n.clientWidth + 2;
  }

  for (const node of Array.from(document.querySelectorAll('body *'))) {
    if (!node || node.nodeType !== 1) continue;
    const vert = canScrollY(node), horiz = canScrollX(node);
    if (!vert && !horiz) continue;
    const r = node.getBoundingClientRect();
    const visW = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    const visH = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    const area = visW * visH;
    if (area < 80) continue;
    const key = (node.tagName || '') + '|' + Math.round(r.top) + '|' + Math.round(r.left) + '|' + node.scrollHeight;
    if (seen.has(key)) continue;
    seen.add(key);
    const sample = (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 72);
    const maxScroll = Math.max(0, Math.round(node.scrollHeight - node.clientHeight));
    regions.push({
      tag: (node.tagName || '').toLowerCase(),
      id: node.id || '',
      role: node.getAttribute('role') || '',
      area: Math.round(area),
      scrollTop: Math.round(node.scrollTop),
      maxScroll: maxScroll,
      scrollHeight: Math.round(node.scrollHeight),
      clientHeight: Math.round(node.clientHeight),
      canDown: node.scrollTop + node.clientHeight < node.scrollHeight - 2,
      canUp: node.scrollTop > 2,
      sample: sample,
      top: Math.round(r.top),
      left: Math.round(r.left),
    });
  }

  const se = document.scrollingElement || document.documentElement;
  const pageMax = Math.max(0, Math.round(se.scrollHeight - se.clientHeight));
  regions.push({
    tag: 'page',
    id: '',
    role: '',
    area: Math.round(vw * vh),
    scrollTop: Math.round(se.scrollTop),
    maxScroll: pageMax,
    scrollHeight: Math.round(se.scrollHeight),
    clientHeight: Math.round(se.clientHeight),
    canDown: se.scrollTop + se.clientHeight < se.scrollHeight - 2,
    canUp: se.scrollTop > 2,
    sample: '',
    top: 0,
    left: 0,
  });

  regions.sort((a, b) => b.area - a.area);
  return regions.slice(0, 14);
}
"""

SCROLL_ACTION_NAMES = {"smart_scroll", "scroll_element", "scroll_down", "scroll_up", "scroll_to_text", "list_scrollable_regions"}


def describe_smart_scroll(outcome: str, direction: str, index: int | None = None, target_text: str = "") -> str:
    outcome = str(outcome or "")
    dir_vi = {"down": "xuống", "up": "lên", "left": "sang trái", "right": "sang phải"}.get(direction, direction)
    target_text = (target_text or "").strip()
    # Honest "did not move" signal so the model never claims a scroll that didn't happen.
    if outcome == "noscroll":
        return (
            "⚠️ KHÔNG cuộn được — vị trí nội dung không đổi. "
            "Gọi list_scrollable_regions để xem vùng nào cuộn được, "
            "rồi smart_scroll với index của một mục TRONG vùng đó (hoặc target_text). "
            "TUYỆT ĐỐI không kết luận đã kiểm tra hết khi nhận thông báo này."
        )
    if outcome.startswith("wheel:"):
        tag = outcome.split(":", 1)[1] or "vùng"
        where = f" (mốc index {index})" if index is not None else ""
        return (
            f"📜 Đã cuộn {dir_vi} bằng chuột (wheel) trong <{tag}>{where} — "
            "vị trí cuộn đã thay đổi. Tiếp tục cuộn hoặc extract_content để xác nhận nội dung mới."
        )
    if outcome in ("reveal", "region-reveal", "page-reveal") or outcome.startswith("region-reveal:"):
        label = f"'{target_text}'" if target_text else "mục cần xem"
        return f"📜 Đã cuộn để đưa {label} vào tầm nhìn"
    if outcome == "notfound":
        return f"⚠️ Không tìm thấy '{target_text}' trên trang để cuộn tới (thử từ khóa khác hoặc cuộn từng phần)"
    if outcome.startswith("region:"):
        tag = outcome.split(":", 1)[1] or "phần tử"
        where = f" (mốc index {index})" if index is not None else ""
        return f"📜 Đã cuộn {dir_vi} bên trong vùng cuộn riêng <{tag}>{where} — vị trí cuộn đã thay đổi"
    if outcome == "page":
        return f"📜 Đã cuộn {dir_vi} toàn trang — vị trí cuộn đã thay đổi"
    return f"📜 smart_scroll {dir_vi}: {outcome}"


def format_scrollable_regions_report(regions: Any) -> str:
    """Human-readable map of scrollable areas for the agent."""
    if not isinstance(regions, list) or not regions:
        return (
            "⚠️ Không phát hiện vùng cuộn nào đang hiển thị. "
            "Thử mở dropdown/modal trước, hoặc cuộn toàn trang một lần rồi gọi lại."
        )
    lines = ["📋 Các vùng CÓ THỂ cuộn trên trang (lớn → nhỏ):"]
    for i, raw in enumerate(regions[:12], 1):
        if not isinstance(raw, dict):
            continue
        tag = str(raw.get("tag") or "?")
        sample = str(raw.get("sample") or "").strip()
        can_parts: list[str] = []
        if raw.get("canDown"):
            can_parts.append("xuống")
        if raw.get("canUp"):
            can_parts.append("lên")
        can_str = "/".join(can_parts) if can_parts else "hết biên"
        pos = f"{raw.get('scrollTop', 0)}/{raw.get('maxScroll', 0)}"
        if tag == "page":
            lines.append(f"  {i}. [TOÀN TRANG] cuộn {can_str} — vị trí {pos}px")
            continue
        hint = f"«{sample[:56]}…»" if len(sample) > 56 else (f"«{sample}»" if sample else "")
        role = raw.get("role") or ""
        role_bit = f" role={role}" if role else ""
        lines.append(
            f"  {i}. <{tag}>{role_bit} ~{raw.get('area', 0)}px², cuộn {can_str}, vị trí {pos}px"
            + (f" — {hint}" if hint else "")
        )
    lines.append(
        "→ Kiểm tra danh sách/dropdown: smart_scroll(direction, index=<mốc TRONG danh sách>) "
        "hoặc target_text='<tên mục cuối>'. Sau mỗi lần cuộn, extract_content hoặc đọc DOM lại. "
        "Chỉ kết luận «đã xem hết» khi canDown=false cho đúng vùng hoặc đã thấy mục cuối."
    )
    return "\n".join(lines)


async def _scroll_signature(page: Any) -> str:
    try:
        return str(await page.evaluate(SCROLL_SIGNATURE_JS))
    except Exception:
        return ""


async def _wheel_scroll_at_handle(page: Any, element_handle: Any, direction: str, amount: int) -> None:
    try:
        box = await element_handle.bounding_box()
        if box:
            await page.mouse.move(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
            )
    except Exception:
        pass
    step = abs(int(amount or 0) or 300)
    horiz = direction in ("left", "right")
    neg = direction in ("up", "left")
    delta = -step if neg else step
    if horiz:
        await page.mouse.wheel(delta, 0)
    else:
        await page.mouse.wheel(0, delta)


async def _wheel_scroll_page(page: Any, direction: str, amount: int) -> None:
    step = abs(int(amount or 0) or 400)
    neg = direction in ("up", "left")
    delta = -step if neg else step
    if direction in ("left", "right"):
        await page.mouse.wheel(delta, 0)
    else:
        await page.mouse.wheel(0, delta)


async def _scroll_at_element_handle(
    page: Any,
    element_handle: Any,
    direction: str,
    amount: int,
    opts: dict,
) -> str:
    """scrollBy on nearest scrollable ancestor; wheel-at-hover if position unchanged."""
    sig_before = await _scroll_signature(page)
    outcome = str(await element_handle.evaluate(SMART_SCROLL_ELEMENT_JS, opts) or "")
    if outcome != "noscroll":
        return outcome
    await _wheel_scroll_at_handle(page, element_handle, direction, amount)
    sig_after = await _scroll_signature(page)
    if sig_after != sig_before:
        tag = "region"
        if outcome.startswith("region:"):
            tag = outcome.split(":", 1)[1] or tag
        return f"wheel:{tag}"
    return "noscroll"


# ── Custom Controller ────────────────────────────────────────────────────────
controller = Controller() if BROWSER_USE_AVAILABLE else None

if BROWSER_USE_AVAILABLE:
    class ScrollElementAction(BaseModel):
        index: int = Field(..., description="Chỉ số (index) của phần tử cần cuộn từ danh sách DOM")
        amount: int = Field(150, description="Số pixel cần cuộn (số dương cho cuộn xuống/phải, số âm cho cuộn lên/trái)")
        direction: str = Field("down", description="Hướng cuộn: 'down', 'up', 'left', hoặc 'right'")

    @controller.registry.action(
        'Scroll a specific scrollable element (e.g. dropdown menu, inner div, scrollable panel) by its index. '
        'Hover on the element and use wheel if native scrollBy fails. Returns noscroll if nothing moved.',
        param_model=ScrollElementAction
    )
    async def scroll_element(index, amount, direction, browser) -> ActionResult:
        try:
            direction = (direction or "down").lower().strip()
            if direction not in ("down", "up", "left", "right"):
                direction = "down"
            selector_map = await browser.get_selector_map()
            if index not in selector_map:
                raise Exception(f"Không tìm thấy phần tử có index {index} trên trang")

            element_node = await browser.get_dom_element_by_index(index)
            element_handle = await browser.get_locate_element(element_node)
            if not element_handle:
                raise Exception(f"Không tìm thấy handle của phần tử index {index}")

            page = await browser.get_current_page()
            opts = {
                "amount": int(amount or 0),
                "horizontal": direction in ("left", "right"),
                "negative": direction in ("up", "left"),
                "targetText": "",
            }
            outcome = await _scroll_at_element_handle(
                page, element_handle, direction, int(amount or 300), opts,
            )
            return ActionResult(
                extracted_content=describe_smart_scroll(outcome, direction, index=index),
                include_in_memory=True,
            )
        except Exception as e:
            return ActionResult(error=str(e))

    class SmartScrollAction(BaseModel):
        direction: str = Field("down", description="Hướng cuộn: 'down', 'up', 'left', hoặc 'right'")
        amount: int = Field(0, description="Số pixel cần cuộn; để 0 để tự động dùng ~80% kích thước khung nhìn")
        target_text: str = Field("", description="(Tùy chọn) Văn bản cần đưa vào tầm nhìn — tự cuộn đúng vùng chứa nó")
        index: int = Field(-1, description="(Tùy chọn) Index phần tử mốc để cuộn đúng vùng cuộn chứa phần tử đó")

    @controller.registry.action(
        'Smartly scroll: automatically scrolls the WHOLE page OR the correct inner scrollable region '
        '(dropdown menu, modal, inner list/panel). Provide target_text to bring a specific item into view, '
        'or index to scroll the region that contains a known element. Prefer this over scroll_down/scroll_up '
        'whenever the content you need might live inside an inner scrollable container.',
        param_model=SmartScrollAction,
    )
    async def smart_scroll(direction, amount, target_text, index, browser) -> ActionResult:
        try:
            direction = (direction or "down").lower().strip()
            if direction not in ("down", "up", "left", "right"):
                direction = "down"
            opts = {
                "amount": int(amount or 0),
                "horizontal": direction in ("left", "right"),
                "negative": direction in ("up", "left"),
                "targetText": str(target_text or ""),
            }

            page = await browser.get_current_page()

            # Anchor on a specific element index: scroll its nearest scrollable region (+ wheel).
            if isinstance(index, int) and index >= 0:
                try:
                    selector_map = await browser.get_selector_map()
                    if index in selector_map:
                        element_node = await browser.get_dom_element_by_index(index)
                        element_handle = await browser.get_locate_element(element_node)
                        if element_handle:
                            outcome = await _scroll_at_element_handle(
                                page,
                                element_handle,
                                direction,
                                int(amount or 300),
                                opts,
                            )
                            return ActionResult(
                                extracted_content=describe_smart_scroll(outcome, direction, index=index),
                                include_in_memory=True,
                            )
                except Exception:
                    pass  # fall through to page-level smart scroll

            outcome = await page.evaluate(SMART_SCROLL_PAGE_JS, opts)
            if outcome == "noscroll":
                sig_before = await _scroll_signature(page)
                await _wheel_scroll_page(page, direction, int(amount or 400))
                sig_after = await _scroll_signature(page)
                if sig_after != sig_before:
                    outcome = "wheel:page"
            return ActionResult(
                extracted_content=describe_smart_scroll(outcome, direction, target_text=target_text),
                include_in_memory=True,
            )
        except Exception as e:
            return ActionResult(error=str(e))

    class ListScrollableRegionsAction(BaseModel):
        pass

    @controller.registry.action(
        'List all scrollable regions visible on the page (whole page, dropdowns, inner lists, panels). '
        'CALL THIS before scrolling when you must verify hidden/long list content. '
        'Shows which areas can scroll up/down and current scroll position.',
        param_model=ListScrollableRegionsAction,
    )
    async def list_scrollable_regions(browser) -> ActionResult:
        try:
            page = await browser.get_current_page()
            regions = await page.evaluate(LIST_SCROLLABLE_REGIONS_JS)
            report = format_scrollable_regions_report(regions)
            return ActionResult(extracted_content=report, include_in_memory=True)
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
AUTO_LEARNING_FILE       = Path("local/auto_learning.json")
AGENT_CHROME_PROFILES_DIR = Path("local/chrome_profiles")
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
# Adaptive planner cadence: marathon/monitor tasks plan often (every 4 steps);
# ordinary multi-step tasks plan less often (every 6) to save planner tokens on
# healthy stretches. The planner pays for itself by keeping the executor on track.
PLANNER_INTERVAL_HARD    = 4
PLANNER_INTERVAL_NORMAL  = 6
MAX_REPEAT_ACTIONS       = 4
SCROLL_STUCK_MISSES      = 2   # consecutive scrolls with zero page movement before asking for help
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
# Rate-to-learn: how many "Hài lòng" ratings on similar commands before the
# system auto-promotes a learned pattern into a 0-LLM replayable workflow (req #4).
AUTO_LEARN_PROMOTE_THRESHOLD = 2
WORKFLOW_LOCAL_REPAIR_LIMIT = 4
WORKFLOW_AI_RECOVERY_MAX_STEPS = 6
WORKFLOW_AI_RECOVERY_MAX_INTERVENTIONS = 2
# Flexible orchestrator: alternate workflow replay and agent phases within one request.
MAX_ORCHESTRATOR_PHASES = 24
MAX_CONSECUTIVE_AGENT_PHASES = 3
WORKFLOW_TEXT_SNAPSHOT_CHARS = 8000

AGENT_BLOCKED_MARKERS = (
    "khong the", "không thể", "khong hoan thanh", "không hoàn thành",
    "can dang nhap", "cần đăng nhập", "yeu cau ban", "yêu cầu bạn",
    "can ban", "cần bạn", "blocked", "unable to", "cannot complete",
    "not possible", "permission denied", "access denied",
)


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

# Cheap model used only for reading/extracting page content (extract_content).
# Routing extraction here means a Pro executor never burns Pro-priced tokens just
# to read text off a page. Falls back to the executor model if unavailable.
EXTRACTION_MODEL = "gemini-2.5-flash-lite"

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
    "scroll_down", "scroll_up", "smart_scroll", "click_element",
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
current_browser_profile = None

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


def profile_key(profile_config: dict[str, Any]) -> str:
    return "|".join([
        str(profile_config.get("name") or ""),
        str(profile_config.get("type") or ""),
        str(profile_config.get("user_data_dir") or ""),
        str(profile_config.get("profile_directory") or ""),
    ])


def profile_debug_port(profile_config: dict[str, Any]) -> int:
    checksum = sum((index + 1) * ord(char) for index, char in enumerate(profile_key(profile_config)))
    return 9300 + (checksum % 500)


def chrome_copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored_names = {
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "ShaderCache",
        "DawnCache",
        "Crashpad",
        "BrowserMetrics",
        "OptimizationHints",
        "Safe Browsing",
        "CertificateRevocation",
        "File System",
        "IndexedDB",
        "Service Worker",
        "Storage",
        "Session Storage",
        "Sessions",
    }
    return {name for name in names if name in ignored_names or name.endswith(".tmp")}


def copy_chrome_profile_dir(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for root, dir_names, file_names in os.walk(source):
        root_path = Path(root)
        ignored = chrome_copy_ignore(str(root_path), dir_names + file_names)
        dir_names[:] = [name for name in dir_names if name not in ignored]

        relative_root = root_path.relative_to(source)
        target_root = destination / relative_root
        target_root.mkdir(parents=True, exist_ok=True)

        for file_name in file_names:
            if file_name in ignored:
                continue
            source_file = root_path / file_name
            target_file = target_root / file_name
            try:
                shutil.copy2(source_file, target_file)
            except Exception:
                continue


def clone_system_profile_for_agent(profile_config: dict[str, Any]) -> dict[str, Any]:
    profile_type = str(profile_config.get("type") or "custom").lower()
    if profile_type != "system":
        return profile_config

    source_user_data_dir = Path(str(profile_config.get("user_data_dir") or "")).expanduser()
    profile_directory = str(profile_config.get("profile_directory") or "Default").strip() or "Default"
    source_profile_dir = source_user_data_dir / profile_directory
    if not source_profile_dir.exists():
        return profile_config

    clone_name = safe_profile_dir_name(f"{profile_config.get('name') or profile_directory}_{profile_directory}")
    clone_user_data_dir = (AGENT_CHROME_PROFILES_DIR / "linked" / clone_name).resolve()
    clone_profile_dir = clone_user_data_dir / profile_directory
    clone_user_data_dir.mkdir(parents=True, exist_ok=True)

    for filename in ("Local State", "First Run"):
        source_file = source_user_data_dir / filename
        if source_file.exists() and not (clone_user_data_dir / filename).exists():
            try:
                shutil.copy2(source_file, clone_user_data_dir / filename)
            except Exception:
                pass

    if not clone_profile_dir.exists():
        copy_chrome_profile_dir(source_profile_dir, clone_profile_dir)

    cloned = dict(profile_config)
    cloned["source_user_data_dir"] = str(source_user_data_dir)
    cloned["source_profile_directory"] = profile_directory
    cloned["user_data_dir"] = str(clone_user_data_dir)
    cloned["profile_directory"] = profile_directory
    cloned["is_profile_clone"] = True
    return cloned


def build_browser_config(profile_config: dict[str, Any]) -> BrowserConfig:
    user_data_dir = Path(str(profile_config.get("user_data_dir") or "")).expanduser()
    profile_directory = str(profile_config.get("profile_directory") or "Default").strip() or "Default"
    profile_type = str(profile_config.get("type") or "custom").lower()
    debug_port = profile_debug_port(profile_config)

    if profile_type == "custom":
        user_data_dir.mkdir(parents=True, exist_ok=True)

    chrome_path = find_chrome_executable()
    extra_args = [
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile_directory}",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]

    kwargs: dict[str, Any] = {
        "headless": False,
        "keep_alive": False,
        "chrome_remote_debugging_port": debug_port,
        "extra_browser_args": extra_args,
    }
    if chrome_path:
        kwargs["browser_binary_path"] = chrome_path
    return BrowserConfig(**kwargs)


def browser_profile_error_message(exc: Exception, profile_config: dict[str, Any]) -> str:
    raw = str(exc)
    lowered = raw.lower()
    profile_name = profile_config.get("name") or "hồ sơ đã chọn"
    profile_type = profile_config.get("type") or "custom"
    lock_markers = [
        "user data directory is already in use",
        "profile appears to be in use",
        "singletonlock",
        "processsingleton",
        "already running",
    ]
    if profile_type == "system" and any(marker in lowered for marker in lock_markers):
        return (
            f"Không mở được hồ sơ Chrome '{profile_name}' vì Chrome đang khóa profile này. "
            "Hãy tắt hoàn toàn mọi cửa sổ Google Chrome thường rồi chạy lại, hoặc dùng hồ sơ độc lập của Agent."
        )
    if "to start chrome in debug mode" in lowered or "connect econnrefused" in lowered:
        return (
            f"Không mở được Chrome với hồ sơ '{profile_name}'. Chrome có thể vẫn còn chạy nền hoặc profile đang bị khóa. "
            "Hãy tắt Chrome trong Task Manager, hoặc dùng hồ sơ độc lập của Agent."
        )
    return raw


async def close_all_sessions() -> None:
    for chat_id in list(sessions.keys()):
        await close_session(chat_id)


def is_tcp_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def is_browser_closed_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = [
        "target page, context or browser has been closed",
        "browser closed",
        "browser is closed or disconnected",
        "no valid pages available",
        "did the browser process quit",
    ]
    return any(marker in text for marker in markers)


async def reset_browser_runtime() -> None:
    global browser_instance, current_browser_profile

    await close_all_sessions()
    if browser_instance:
        try:
            await browser_instance.close()
        except Exception:
            pass
    browser_instance = None
    current_browser_profile = None


def normalized_windows_path(value: str) -> str:
    try:
        return str(Path(value.strip().strip('"')).resolve()).lower()
    except Exception:
        return value.strip().strip('"').lower()


def chrome_processes_using_user_data_dir(user_data_dir: str, debug_port: int | None = None) -> list[int]:
    if psutil is None:
        return []

    target = normalized_windows_path(user_data_dir)
    blocking_pids: list[int] = []
    remote_debug_arg = f"--remote-debugging-port={debug_port}" if debug_port else ""

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name != "chrome.exe":
                continue
            cmdline = proc.info.get("cmdline") or []
            command = " ".join(str(arg) for arg in cmdline)
            if remote_debug_arg and remote_debug_arg in command:
                continue
            # Renderer/utility/crashpad child processes inherit --user-data-dir
            # but do not always include --remote-debugging-port. They belong to
            # the already-running Agent browser and should not block reuse.
            if any(str(arg).startswith("--type=") for arg in cmdline):
                continue
            for arg in cmdline:
                text = str(arg)
                if text.startswith("--user-data-dir="):
                    running_dir = normalized_windows_path(text.split("=", 1)[1])
                    if running_dir == target:
                        blocking_pids.append(int(proc.info["pid"]))
                        break
        except Exception:
            continue

    return blocking_pids


def chrome_process_pids(exclude_debug_port: int | None = None) -> list[int]:
    if psutil is None:
        return []

    pids: list[int] = []
    remote_debug_arg = f"--remote-debugging-port={exclude_debug_port}" if exclude_debug_port else ""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (proc.info.get("name") or "").lower() != "chrome.exe":
                continue
            command = " ".join(str(arg) for arg in (proc.info.get("cmdline") or []))
            if remote_debug_arg and remote_debug_arg in command:
                continue
            pids.append(int(proc.info["pid"]))
        except Exception:
            continue
    return pids


def close_chrome_processes(pids: list[int], timeout_seconds: float = 3.0) -> list[int]:
    if psutil is None:
        return pids

    processes = []
    for pid in pids:
        try:
            processes.append(psutil.Process(pid))
        except Exception:
            continue

    for proc in processes:
        try:
            proc.terminate()
        except Exception:
            pass

    gone, alive = psutil.wait_procs(processes, timeout=timeout_seconds)
    for proc in alive:
        try:
            proc.kill()
        except Exception:
            pass

    if alive:
        gone_after_kill, alive = psutil.wait_procs(alive, timeout=timeout_seconds)

    return [proc.pid for proc in alive if proc.is_running()]


def profile_lock_preflight_message(profile_config: dict[str, Any]) -> str | None:
    user_data_dir = str(profile_config.get("user_data_dir") or "")
    if not user_data_dir:
        return None

    pids = chrome_processes_using_user_data_dir(user_data_dir, profile_debug_port(profile_config))
    if not pids:
        return None

    profile_name = profile_config.get("name") or "hồ sơ đã chọn"
    pid_text = ", ".join(str(pid) for pid in pids[:8])
    if len(pids) > 8:
        pid_text += f", ... (+{len(pids) - 8})"
    return (
        f"Chrome vẫn còn process chạy nền đang giữ hồ sơ '{profile_name}' (PID: {pid_text}). "
        "Hãy tắt toàn bộ chrome.exe trong Task Manager rồi gửi prompt lại, hoặc dùng hồ sơ độc lập của Agent."
    )


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


def set_chat_profile(chat_id: str, user_profile: str) -> None:
    history = load_history()
    if chat_id not in history:
        history[chat_id] = {
            "id": chat_id,
            "title": "Chat mới",
            "created_at": datetime.now().isoformat(),
            "messages": [],
        }
    if not history[chat_id].get("user_profile"):
        history[chat_id]["user_profile"] = user_profile or "Default"
        save_history(history)


async def load_history_async() -> dict:
    async with _history_lock:
        return load_history()


async def add_message_async(
    chat_id: str, role: str, content: str, metadata: dict | None = None
) -> None:
    async with _history_lock:
        add_message(chat_id, role, content, metadata)


async def set_chat_profile_async(chat_id: str, user_profile: str) -> None:
    async with _history_lock:
        set_chat_profile(chat_id, user_profile)


async def save_history_async(history: dict) -> None:
    async with _history_lock:
        save_history(history)


# ── User and Site Profile helpers (Behavioral Training Loop) ─────────────────
def safe_profile_dir_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Default"


def normalize_user_profile(profile: Any) -> dict[str, Any] | None:
    if isinstance(profile, str):
        name = profile.strip()
        if not name:
            return None
        return {
            "name": name,
            "type": "custom",
            "user_data_dir": str((AGENT_CHROME_PROFILES_DIR / safe_profile_dir_name(name)).resolve()),
            "profile_directory": "Default",
        }

    if not isinstance(profile, dict):
        return None

    name = str(profile.get("name", "")).strip()
    if not name:
        return None

    profile_type = str(profile.get("type") or "custom").strip().lower()
    if profile_type not in {"custom", "system"}:
        profile_type = "custom"

    normalized = {
        "name": name,
        "type": profile_type,
        "user_data_dir": str(profile.get("user_data_dir") or "").strip(),
        "profile_directory": str(profile.get("profile_directory") or "Default").strip() or "Default",
    }
    if profile_type == "custom" and not normalized["user_data_dir"]:
        normalized["user_data_dir"] = str((AGENT_CHROME_PROFILES_DIR / safe_profile_dir_name(name)).resolve())
    return normalized


def normalize_user_profiles(profiles: Any) -> tuple[list[dict[str, Any]], bool]:
    raw_profiles = profiles if isinstance(profiles, list) else ["Default"]
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    changed = not isinstance(profiles, list)

    for profile in raw_profiles:
        item = normalize_user_profile(profile)
        if not item:
            changed = True
            continue
        if item["name"] in seen:
            changed = True
            continue
        seen.add(item["name"])
        normalized.append(item)
        changed = changed or item != profile

    if not normalized:
        normalized = [normalize_user_profile("Default")]  # type: ignore[list-item]
        changed = True

    return normalized, changed


def load_user_profiles() -> list[dict[str, Any]]:
    if not USER_PROFILES_FILE.exists():
        USER_PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
        profiles, _ = normalize_user_profiles(["Default"])
        save_user_profiles(profiles)
        return profiles
    try:
        profiles = json.loads(USER_PROFILES_FILE.read_text(encoding="utf-8"))
        normalized, _ = normalize_user_profiles(profiles)
        return normalized
    except Exception:
        profiles, _ = normalize_user_profiles(["Default"])
        return profiles


def save_user_profiles(profiles: list[dict[str, Any]]) -> None:
    USER_PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_PROFILES_FILE.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")


def get_profile_config(profile_name: str | None) -> dict[str, Any]:
    profiles = load_user_profiles()
    requested_name = (profile_name or "Default").strip() or "Default"
    for profile in profiles:
        if profile.get("name") == requested_name:
            return profile
    return profiles[0]


def find_chrome_executable() -> str | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def chrome_user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data"


def chrome_user_data_dirs() -> list[Path]:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data",
        Path(os.environ.get("USERPROFILE", "")) / "AppData/Local/Google/Chrome/User Data",
    ]
    seen: set[str] = set()
    paths: list[Path] = []
    for candidate in candidates:
        if not str(candidate).strip():
            continue
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        paths.append(candidate)
    return paths


def load_json_file(path: Path) -> dict:
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return json.loads(path.read_text(encoding=encoding))
        except Exception:
            continue
    return {}


def is_chrome_profile_dir(path: Path) -> bool:
    markers = [
        "Preferences",
        "History",
        "Bookmarks",
        "Cookies",
        "Login Data",
        "Secure Preferences",
    ]
    return path.is_dir() and any((path / marker).exists() for marker in markers)


def chrome_profile_sort_key(directory: str) -> tuple[int, int | str]:
    if directory == "Default":
        return (0, 0)
    match = re.fullmatch(r"Profile\s+(\d+)", directory)
    if match:
        return (1, int(match.group(1)))
    return (2, directory.lower())


def discover_system_chrome_profiles() -> list[dict[str, str]]:
    profiles: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for user_data_dir in chrome_user_data_dirs():
        if not user_data_dir.exists():
            continue

        local_state = load_json_file(user_data_dir / "Local State")
        info_cache = (
            local_state.get("profile", {}).get("info_cache", {})
            if isinstance(local_state, dict)
            else {}
        )
        if not isinstance(info_cache, dict):
            info_cache = {}

        directories: set[str] = set(info_cache.keys())
        try:
            for profile_dir in user_data_dir.iterdir():
                if profile_dir.is_dir() and (
                    profile_dir.name == "Default"
                    or profile_dir.name.startswith("Profile ")
                    or is_chrome_profile_dir(profile_dir)
                ):
                    directories.add(profile_dir.name)
        except OSError:
            continue

        for directory in sorted(directories, key=chrome_profile_sort_key):
            profile_dir = user_data_dir / directory
            if not profile_dir.exists() and directory not in info_cache:
                continue

            key = (str(user_data_dir).lower(), directory.lower())
            if key in seen:
                continue
            seen.add(key)

            preferences = load_json_file(profile_dir / "Preferences")
            info = info_cache.get(directory, {}) if isinstance(info_cache.get(directory, {}), dict) else {}
            name = (
                info.get("name")
                or preferences.get("profile", {}).get("name")
                or ("Default" if directory == "Default" else directory)
            )
            profiles.append({
                "name": str(name),
                "directory": directory,
                "user_data_dir": str(user_data_dir),
            })

    return profiles


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


TEMPLATE_OPTIONAL_FILLERS = frozenset({
    "tai", "ta", "o", "tren", "cho", "for", "in", "at",
})


def _optional_filler_regex() -> str:
    parts = "|".join(re.escape(f) for f in sorted(TEMPLATE_OPTIONAL_FILLERS, key=len, reverse=True))
    return rf"(?:\s+(?:{parts})\s+)?"


def _strip_trailing_template_filler(folded_static: str) -> tuple[str, bool]:
    text = (folded_static or "").rstrip()
    if not text:
        return text, False
    words = text.split()
    if words and words[-1] in TEMPLATE_OPTIONAL_FILLERS:
        return " ".join(words[:-1]), True
    return text, False


def build_relaxed_template_pattern(template: str) -> tuple[re.Pattern | None, list[str]]:
    """Template regex where connector words before {var} (tại/ở/cho…) are optional."""
    vars_found = re.findall(r"\{([a-zA-Z0-9_]+)\}", template)
    if not vars_found:
        return None, []
    chunks = re.split(r"(\{[a-zA-Z0-9_]+\})", template)
    regex_str = "^"
    var_names: list[str] = []
    filler_re = _optional_filler_regex()
    for chunk in chunks:
        if chunk.startswith("{") and chunk.endswith("}"):
            var_names.append(chunk[1:-1])
            regex_str += filler_re + "(.+?)"
        elif chunk:
            static, had_filler = _strip_trailing_template_filler(semantic_prompt(chunk))
            regex_str += re.escape(static)
            if had_filler:
                pass  # filler_re before the capture group makes it optional
    regex_str += "$"
    return re.compile(regex_str, re.IGNORECASE | re.DOTALL), var_names


def match_prompt_to_template(template: str, user_prompt: str) -> tuple[re.Match | None, dict[str, str]]:
    """Accent- and case-insensitive template match; fills {var} slots from the user prompt."""
    normalized_template = semantic_prompt(template)
    normalized_prompt = semantic_prompt(user_prompt)
    if not normalized_template or not normalized_prompt:
        return None, {}
    pattern, vars_found = template_to_regex(normalized_template)
    match = pattern.match(normalized_prompt)
    if not match:
        return None, {}
    var_values: dict[str, str] = {}
    groups = match.groups()
    for idx, var_name in enumerate(vars_found):
        if idx < len(groups) and groups[idx] is not None:
            var_values[var_name] = str(groups[idx]).strip()
    return match, var_values


def match_prompt_to_template_relaxed(
    template: str,
    user_prompt: str,
) -> tuple[re.Match | None, dict[str, str], str]:
    """Exact template match first; then optional filler words (tại/ở/cho…) before variables."""
    match, var_values = match_prompt_to_template(template, user_prompt)
    if match:
        return match, var_values, "template_exact"
    if not re.search(r"\{[a-zA-Z0-9_]+\}", template or ""):
        return None, {}, ""
    pattern, var_names = build_relaxed_template_pattern(template)
    if not pattern:
        return None, {}, ""
    folded_prompt = semantic_prompt(user_prompt)
    relaxed = pattern.match(folded_prompt)
    if not relaxed:
        return None, {}, ""
    var_values = {}
    groups = relaxed.groups()
    for idx, var_name in enumerate(var_names):
        if idx < len(groups) and groups[idx] is not None:
            var_values[var_name] = str(groups[idx]).strip()
    return relaxed, var_values, "template_relaxed"


def match_via_workflow_examples(wf: dict, user_prompt: str) -> dict[str, str] | None:
    """Match user prompt against literal examples stored when the workflow was learned."""
    template = wf.get("prompt_template") or ""
    if not template:
        return None
    for example in (wf.get("metadata") or {}).get("examples") or []:
        if not isinstance(example, str) or "{" in example:
            continue
        if semantic_prompt(example) != semantic_prompt(user_prompt):
            continue
        _, var_values, _ = match_prompt_to_template_relaxed(template, user_prompt)
        return var_values or {}
    return None


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


def score_workflow_candidate(
    wf: dict,
    domain: str,
    user_prompt: str,
    current_url: str = "",
    context_text: str = "",
) -> tuple[float, str]:
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

    _, vars_found = template_to_regex(semantic_prompt(wf.get("prompt_template") or ""))
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
    # Context-aware boosts (request #6): prefer workflows for the page we're on
    # and that align with the recent conversation context.
    current = (current_url or "").lower()
    if domain and domain != "unknown_domain" and domain in current:
        score += 0.12
    if context_text:
        ctx = normalize_for_match(context_text)
        if ctx:
            ctx_overlap = jaccard_score(token_set(ctx), token_set(haystack))
            score += min(0.1, ctx_overlap * 0.1)
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


def find_matching_workflow(
    user_profile: str,
    user_prompt: str,
    current_url: str = "",
    context_text: str = "",
) -> dict | None:
    site_profiles = load_site_profiles()
    current_url_lc = (current_url or "").lower()
    candidates: list[dict] = []

    for domain, workflows in site_profiles.items():
        for wf in workflows:
            if wf.get("user_profile") != user_profile and wf.get("user_profile") != "Default":
                continue

            template = wf.get("prompt_template", "")
            if not template:
                continue

            match_obj, var_values, match_type = match_prompt_to_template_relaxed(template, user_prompt)
            matched = bool(match_obj)
            if not matched:
                example_vars = match_via_workflow_examples(wf, user_prompt)
                if example_vars is not None:
                    var_values = example_vars
                    match_type = "example_literal"
                    matched = True
            if matched:
                score = 10.0 + workflow_health(wf)
                # Context-aware tie-breaker: prefer a workflow for the current page.
                if domain and domain != "unknown_domain" and domain in current_url_lc:
                    score += 0.2
                candidates.append(build_workflow_match(
                    domain=domain,
                    wf=wf,
                    var_values=var_values,
                    score=score,
                    match_type=match_type or "template_exact",
                ))
                continue

            score, match_type = score_workflow_candidate(wf, domain, user_prompt, current_url, context_text)
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
        if score < WORKFLOW_FUZZY_THRESHOLD and match.get("match_type") not in {
            "template_exact", "template_relaxed", "example_literal",
        }:
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
    if action_name in {"smart_scroll", "scroll_element", "scroll_down", "scroll_up", "scroll_to_text"}:
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
    # Derive direction so replay scrolls the right scope (page vs inner region).
    direction = str(params.get("direction") or "").lower().strip()
    if direction not in ("down", "up", "left", "right"):
        direction = "up" if action_name == "scroll_up" else "down"
    amount = int(params.get("amount") or 0)
    target_text = str(params.get("target_text") or params.get("targetText") or "")
    opts = {
        "amount": amount,
        "horizontal": direction in ("left", "right"),
        "negative": direction in ("up", "left"),
        "targetText": target_text,
    }
    try:
        outcome = await page.evaluate(SMART_SCROLL_PAGE_JS, opts)
        if outcome == "noscroll":
            sig_before = await _scroll_signature(page)
            await _wheel_scroll_page(page, direction, amount or 400)
            sig_after = await _scroll_signature(page)
            if sig_after != sig_before:
                outcome = "wheel:page"
        msg = describe_smart_scroll(outcome, direction, target_text=target_text)
        return ReplayStepResult(
            ok=outcome != "noscroll",
            action_name=action_name,
            resolver="smart_scroll_fallback",
            message=msg,
            repaired=outcome != "noscroll",
            error=None if outcome != "noscroll" else msg,
        )
    except Exception:
        try:
            sig_before = await _scroll_signature(page)
            await _wheel_scroll_page(page, direction, amount or 500)
            sig_after = await _scroll_signature(page)
            outcome = "wheel:page" if sig_after != sig_before else "noscroll"
            msg = describe_smart_scroll(outcome, direction, target_text=target_text)
            return ReplayStepResult(
                ok=outcome != "noscroll",
                action_name=action_name,
                resolver="page_scroll_fallback",
                message=msg,
                repaired=outcome != "noscroll",
                error=None if outcome != "noscroll" else msg,
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


def adaptive_recovery_budget(replay_plan: list[dict]) -> int:
    """Longer workflows get a slightly larger AI-recovery budget so a single
    incident deep in a long flow does not force a full Agent fallback (req #5)."""
    base = WORKFLOW_AI_RECOVERY_MAX_INTERVENTIONS
    return max(base, min(5, base + len(replay_plan) // 8))


async def restore_workflow_page(match: dict, browser_context: Any) -> dict:
    """If an incident pushed the browser off the workflow's domain, navigate back
    so replay can resume on the expected site (request #5)."""
    domain = (match.get("domain") or "").strip().lower()
    if not domain or domain == "unknown_domain":
        return {"restored": False, "reason": "no_domain"}
    try:
        page = await browser_context.get_current_page()
        current_url = (page.url or "").lower()
    except Exception:
        return {"restored": False, "reason": "no_page"}
    if domain in current_url:
        return {"restored": False, "reason": "already_on_domain"}
    try:
        page = await browser_context.get_current_page()
        await page.goto(f"https://{domain}", wait_until="domcontentloaded", timeout=15000)
        return {"restored": True, "reason": f"navigated_back_to_{domain}"}
    except Exception as exc:
        return {"restored": False, "reason": f"nav_failed:{str(exc)[:120]}"}


async def resync_replay_index(
    match: dict,
    replay_plan: list[dict],
    browser_context: Any,
    current_idx: int,
) -> dict:
    """After an incident recovery, decide where to safely resume the workflow
    instead of blindly continuing at the next step (request #5).

    Returns {"resume_idx": <1-based next step>, "done": bool, "skipped": int, "note": str}.
    """
    replay_actions = [s.get("action") or {} for s in replay_plan]
    total = len(replay_plan)

    try:
        terminal = await verify_workflow_terminal_state(match, replay_actions, browser_context)
    except Exception:
        terminal = {"ok": False}
    if terminal.get("ok") and float(terminal.get("confidence") or 0) >= 0.78:
        return {
            "resume_idx": total + 1,
            "done": True,
            "skipped": 0,
            "note": f"da dat trang thai dich ({terminal.get('reason')})",
        }

    resume_idx = current_idx + 1
    skipped = 0
    try:
        page = await browser_context.get_current_page()
        current_url = (page.url or "").lower()
    except Exception:
        current_url = ""
    while resume_idx <= total:
        step = replay_plan[resume_idx - 1]
        action_name, params = action_name_and_params(step.get("action") or {})
        capability = step.get("capability") or replay_capability_for(action_name)
        if capability == "navigate":
            action_url = str(params.get("url") or "").rstrip("/").lower()
            if action_url and action_url in current_url:
                resume_idx += 1
                skipped += 1
                continue
        break
    note = f"tiep tuc tu buoc {resume_idx}"
    if skipped:
        note += f", bo qua {skipped} buoc dieu huong da thoa man"
    return {"resume_idx": resume_idx, "done": False, "skipped": skipped, "note": note}


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
    generalized_override: dict | None = None,
) -> dict | None:
    if not failed_match or not history_list:
        return None
    domain = failed_match.get("domain") or ""
    old_wf = failed_match.get("workflow") or {}
    workflow_id = old_wf.get("workflow_id")
    if not domain or not workflow_id:
        return None

    # Prefer the planner-authored generalization (request #3) when provided;
    # fall back to the deterministic rule-based generalizer otherwise.
    generalized = generalized_override or generalize_history(original_task, history_list)
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

    if action_name == "scroll_element":
        idx = params.get("index")
        direction = str(params.get("direction") or "down").lower()
        amount = int(params.get("amount") or 300)
        if idx is not None:
            try:
                selector_map = await browser_context.get_selector_map()
                if int(idx) in selector_map:
                    element_node = await browser_context.get_dom_element_by_index(int(idx))
                    element_handle = await browser_context.get_locate_element(element_node)
                    if element_handle:
                        opts = {
                            "amount": amount,
                            "horizontal": direction in ("left", "right"),
                            "negative": direction in ("up", "left"),
                            "targetText": "",
                        }
                        outcome = await _scroll_at_element_handle(
                            page, element_handle, direction, amount, opts,
                        )
                        if outcome != "noscroll":
                            return describe_smart_scroll(outcome, direction, index=int(idx))
            except Exception:
                pass
        await _wheel_scroll_page(page, direction, amount)
        return describe_smart_scroll("wheel:page", direction)

    if action_name in {"scroll_down", "scroll_up"}:
        direction = "up" if action_name == "scroll_up" else "down"
        amount = int(params.get("amount") or 500)
        sig_before = await _scroll_signature(page)
        await _wheel_scroll_page(page, direction, amount)
        sig_after = await _scroll_signature(page)
        outcome = "wheel:page" if sig_after != sig_before else "noscroll"
        return describe_smart_scroll(outcome, direction)

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


# ── Planner-authored workflow generalization (self-improvement) ───────────────
# Action vocabulary the learned workflows are allowed to use. Keeps the planner
# from inventing actions the replay engine cannot execute deterministically.
ALLOWED_WORKFLOW_ACTIONS = {
    "go_to_url", "open_tab", "switch_tab", "close_tab", "go_back", "refresh_page",
    "search_google", "input_text", "fill_login_form",
    "click_element_by_text", "click_element_by_index", "click_element",
    "smart_scroll", "scroll_element", "scroll_down", "scroll_up", "scroll_to_text",
    "list_scrollable_regions",
    "send_keys", "extract_content", "wait", "done",
}


def _extract_json_object(text: str) -> dict | None:
    """Best-effort: pull the first JSON object out of an LLM response."""
    if not text:
        return None
    cleaned = str(text).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9]*", "", cleaned).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def sanitize_llm_workflow_steps(steps: Any) -> list[dict]:
    """Keep only well-formed single-action steps drawn from the allowed vocabulary."""
    clean: list[dict] = []
    if not isinstance(steps, list):
        return clean
    for step in steps:
        if not isinstance(step, dict) or not step:
            continue
        name = next(iter(step.keys()), "")
        if name not in ALLOWED_WORKFLOW_ACTIONS:
            continue
        params = step.get(name)
        if params is None:
            params = {}
        if not isinstance(params, dict):
            continue
        clean.append({name: params})
    return clean


def build_generalization_prompt(original_task: str, base: dict) -> str:
    base_steps = base.get("steps") or []
    base_vars = base.get("variables") or []
    steps_json = json.dumps(base_steps, ensure_ascii=False)
    allowed = sorted(ALLOWED_WORKFLOW_ACTIONS)
    return (
        "Bạn là chuyên gia tự động hoá web. Một agent vừa hoàn thành thành công một tác vụ trên trình duyệt.\n"
        "Hãy KHÁI QUÁT HOÁ chuỗi thao tác thành một QUY TRÌNH (workflow) tái sử dụng được cho các lần sau "
        "mà KHÔNG cần gọi lại AI.\n\n"
        f"Tác vụ gốc của người dùng:\n{original_task}\n\n"
        f"Chuỗi bước thô (trích từ lịch sử chạy thật), dạng JSON:\n{steps_json}\n\n"
        f"Biến gợi ý ban đầu: {base_vars}\n\n"
        "YÊU CẦU:\n"
        "1. Thay các giá trị cụ thể (tên đăng nhập, từ khoá tìm, số lượng, người nhận...) bằng biến dạng {ten_bien} "
        "có nghĩa (vd {username}, {password}, {search_query}, {recipient}). TUYỆT ĐỐI không dùng tên kiểu var_1.\n"
        "2. Ưu tiên hành động ỔN ĐỊNH: dùng click_element_by_text (theo nhãn hiển thị) thay cho click theo index; "
        "dùng fill_login_form cho cặp đăng nhập; dùng list_scrollable_regions + smart_scroll (index/target_text) thay cho cuộn mù.\n"
        "3. Loại bỏ bước thừa/lặp; giữ đúng thứ tự tối thiểu cần để hoàn thành tác vụ.\n"
        "4. KHÔNG để mật khẩu thật trong bước; mật khẩu chỉ đi qua biến {password} trong fill_login_form.\n"
        f"5. CHỈ dùng các action sau: {allowed}.\n\n"
        "Trả về DUY NHẤT một object JSON (không kèm giải thích, không markdown) theo schema:\n"
        "{\n"
        '  "prompt_template": "câu lệnh người dùng có chứa {bien}",\n'
        '  "variables": ["username", "password"],\n'
        '  "steps": [{"go_to_url": {"url": "..."}}, {"fill_login_form": {"username": "{username}", "password": "{password}"}}],\n'
        '  "intent": "mô tả ngắn ý định",\n'
        '  "success_criteria": "điều kiện coi là hoàn thành",\n'
        '  "avoid_when": ["tình huống không nên dùng"]\n'
        "}\n"
    )


async def llm_generalize_workflow(
    planner_model: str,
    original_task: str,
    history_list: list,
    *,
    specific: bool = False,
) -> dict | None:
    """Use the PLANNER model to think about how to generalize a successful run
    into a robust, reusable workflow (request #3). Returns a dict in the same
    shape as generalize_history(), or None if the output is unusable so the
    caller can fall back to the deterministic rule-based generalizer.
    """
    if specific:
        return None  # specific workflows stay literal; nothing to generalize
    base = generalize_history(original_task, history_list, specific=False)
    if not (base.get("steps") or []):
        return None

    try:
        prompt = build_generalization_prompt(original_task, base)
        llm = make_llm(planner_model)
        response = await llm.ainvoke(prompt)
        text = getattr(response, "content", None)
        if isinstance(text, list):
            text = " ".join(str(p) for p in text)
        text = text if isinstance(text, str) else str(response)
    except Exception as exc:
        print(f"[llm_generalize_workflow] LLM call failed: {exc}")
        return None

    obj = _extract_json_object(text)
    if not obj:
        return None

    new_steps = sanitize_llm_workflow_steps(obj.get("steps"))
    if not new_steps:
        return None

    variables = [str(v).strip() for v in (obj.get("variables") or []) if str(v).strip()]
    referenced: set[str] = set()
    for step in new_steps:
        _, params = action_name_and_params(step)
        for value in params.values():
            for found in re.findall(r"\{([a-zA-Z0-9_]+)\}", str(value)):
                referenced.add(found)
    for ref in referenced:
        if ref not in variables:
            variables.append(ref)

    prompt_template = str(obj.get("prompt_template") or base.get("prompt_template") or original_task).strip()
    candidate = {"prompt_template": prompt_template, "variables": variables, "steps": new_steps}
    issues = workflow_validation_issues(candidate)
    blocking_issues = {
        "workflow_has_opaque_var_names",
        "credential_workflow_must_use_fill_login_form",
        "password_must_not_use_raw_input_text_index",
        "username_password_values_must_differ",
    }
    if set(issues) & blocking_issues:
        return None  # let the caller fall back to the rule-based generalizer

    input_schema = [variable_schema_for(v) for v in variables]
    intent = str(obj.get("intent") or prompt_template).strip()
    description = str(obj.get("description") or f"Planner-generalized workflow learned from: {original_task}").strip()
    success_criteria = str(obj.get("success_criteria") or "Complete the learned browser task without action errors.").strip()
    avoid_when = obj.get("avoid_when") if isinstance(obj.get("avoid_when"), list) else []

    return {
        "prompt_template": prompt_template,
        "variables": variables,
        "steps": new_steps,
        "metadata": {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "workflow_scope": "general",
            "intent": intent,
            "description": description,
            "inputs": variables,
            "input_schema": input_schema,
            "success_criteria": success_criteria,
            "avoid_when": [str(a) for a in avoid_when][:10],
            "examples": [original_task, prompt_template],
            "action_names": workflow_action_names(new_steps),
            "validation_issues": issues,
            "authored_by": "planner_llm",
        },
        "stats": base.get("stats") or {},
        "confidence": 0.62 if issues else 0.8,
    }


# ── Auto-learning store (rate-to-learn → 0-LLM replay, request #4) ─────────────
def load_auto_learning() -> dict:
    if not AUTO_LEARNING_FILE.exists():
        return {}
    try:
        data = json.loads(AUTO_LEARNING_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_auto_learning(store: dict) -> None:
    AUTO_LEARNING_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTO_LEARNING_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def auto_learning_signature(generalized: dict, original_task: str) -> str:
    """Group tasks that differ only by variable values under one learned pattern.

    Using the generalized prompt_template means 'đăng nhập fb tài khoản A' and
    'đăng nhập fb tài khoản B' share a signature and reinforce the same pattern.
    """
    template = (generalized or {}).get("prompt_template") or original_task
    return normalize_for_match(template)


def record_auto_learning(domain: str, signature: str, original_task: str, generalized: dict) -> dict:
    """Increment the satisfaction counter for a learned pattern; refresh its steps."""
    store = load_auto_learning()
    key = domain or "unknown_domain"
    bucket = store.setdefault(key, [])
    now = datetime.now().isoformat()
    candidate = None
    for item in bucket:
        if item.get("signature") == signature:
            candidate = item
            break
    if candidate is None:
        candidate = {
            "signature": signature,
            "prompt_template": generalized.get("prompt_template"),
            "variables": generalized.get("variables") or [],
            "steps": generalized.get("steps") or [],
            "satisfied_count": 0,
            "examples": [],
            "created_at": now,
            "updated_at": now,
            "promoted": False,
            "promoted_workflow_id": None,
        }
        bucket.append(candidate)
    candidate["satisfied_count"] = int(candidate.get("satisfied_count") or 0) + 1
    candidate["updated_at"] = now
    if generalized.get("steps"):
        candidate["steps"] = generalized["steps"]
        candidate["variables"] = generalized.get("variables") or candidate.get("variables") or []
        candidate["prompt_template"] = generalized.get("prompt_template") or candidate.get("prompt_template")
    examples = list(candidate.get("examples") or [])
    if original_task and original_task not in examples:
        examples.append(original_task)
    candidate["examples"] = examples[-10:]
    save_auto_learning(store)
    return dict(candidate)


def forget_auto_learning(domain: str, signature: str) -> None:
    """Drop an unpromoted learned candidate (used on dislike)."""
    store = load_auto_learning()
    key = domain or "unknown_domain"
    bucket = store.get(key)
    if not bucket:
        return
    new_bucket = [
        item for item in bucket
        if not (item.get("signature") == signature and not item.get("promoted"))
    ]
    if len(new_bucket) != len(bucket):
        store[key] = new_bucket
        save_auto_learning(store)


def mark_auto_learning_promoted(domain: str, signature: str, workflow_id: str) -> None:
    store = load_auto_learning()
    bucket = store.get(domain or "unknown_domain") or []
    for item in bucket:
        if item.get("signature") == signature:
            item["promoted"] = True
            item["promoted_workflow_id"] = workflow_id
            item["updated_at"] = datetime.now().isoformat()
    save_auto_learning(store)


def adjust_workflow_confidence(workflow_id: str, delta: float, reason: str = "") -> bool:
    """Nudge a workflow's confidence up (reinforce) or down (dislike)."""
    if not workflow_id:
        return False
    profiles = load_site_profiles()
    changed = False
    now = datetime.now().isoformat()
    for _domain, workflows in profiles.items():
        for wf in workflows:
            if wf.get("workflow_id") != workflow_id:
                continue
            base = float(wf.get("confidence", 0.62) or 0.62)
            wf["confidence"] = max(0.0, min(0.97, base + delta))
            meta = dict(wf.get("metadata") or {})
            if delta < 0:
                meta["last_dislike_at"] = now
                if reason:
                    meta["dislike_reason"] = reason
            else:
                meta["last_like_at"] = now
            wf["metadata"] = meta
            changed = True
    if changed:
        save_site_profiles(profiles)
    return changed


def promote_auto_learned_workflow(domain: str, generalized: dict, original_task: str, user_profile: str) -> str | None:
    """Create an active, replayable workflow from a learned pattern (0-LLM future runs)."""
    if not (generalized.get("steps") or []):
        return None
    blocking = {
        "workflow_has_opaque_var_names",
        "credential_workflow_must_use_fill_login_form",
        "password_must_not_use_raw_input_text_index",
        "username_password_values_must_differ",
    }
    if set(workflow_validation_issues(generalized)) & blocking:
        return None
    profiles = load_site_profiles()
    key = domain or "unknown_domain"
    bucket = profiles.setdefault(key, [])
    workflow_id = str(uuid.uuid4())[:8]
    metadata = dict(generalized.get("metadata") or {})
    metadata["domain"] = domain
    metadata["workflow_scope"] = "general"
    metadata["auto_learned"] = True
    workflow = {
        "workflow_id": workflow_id,
        "workflow_name": f"Tự học: {shorten_text(original_task, 48)}",
        "user_profile": user_profile or "Default",
        "prompt_template": generalized.get("prompt_template") or original_task,
        "variables": generalized.get("variables") or [],
        "steps": generalized.get("steps") or [],
        "metadata": metadata,
        "stats": generalized.get("stats") or {},
        "confidence": float(generalized.get("confidence", 0.7) or 0.7),
    }
    bucket.append(workflow)
    save_site_profiles(profiles)
    return workflow_id


async def handle_rating_feedback(session: dict, req: "FeedbackRequest") -> dict:
    """Lightweight 'rate to learn' path (request #4): no explicit workflow save.

    Satisfied ratings accumulate per learned pattern and auto-promote to a 0-LLM
    replayable workflow after AUTO_LEARN_PROMOTE_THRESHOLD similar commands.
    """
    user_prof = req.user_profile or session.get("user_profile") or "Default"
    last_history = session.get("last_history")
    original_task = session.get("task")
    last_replay = session.get("last_replay_workflow") or {}

    if not req.satisfied:
        if last_replay.get("workflow_id"):
            adjust_workflow_confidence(last_replay["workflow_id"], -0.2, reason="user_dislike")
        if last_history and original_task:
            try:
                domain = get_domain_from_history(last_history)
                base = generalize_history(original_task, last_history)
                forget_auto_learning(domain, auto_learning_signature(base, original_task))
            except Exception:
                pass
        session.pop("last_history", None)
        return {"ok": True, "message": "Đã ghi nhận chưa hài lòng. Hệ thống sẽ điều chỉnh và hạn chế dùng lại quy trình này."}

    # Satisfied + the run was already a 0-LLM replay → just reinforce it.
    if last_replay.get("workflow_id") and not last_history:
        adjust_workflow_confidence(last_replay["workflow_id"], 0.05)
        return {"ok": True, "message": "Tuyệt! Quy trình này đã chạy không cần gọi AI — hệ thống đã củng cố độ tin cậy."}

    if not last_history or not original_task:
        return {"ok": True, "message": "Đã ghi nhận hài lòng."}

    try:
        domain = get_domain_from_history(last_history)
        base = generalize_history(original_task, last_history)  # rule-based, 0 LLM call
        signature = auto_learning_signature(base, original_task)
        candidate = record_auto_learning(domain, signature, original_task, base)
        count = int(candidate.get("satisfied_count") or 0)

        promoted_id = None
        if candidate.get("promoted"):
            message = "Loại lệnh này đã được tự học trước đó — lần sau tự chạy không cần gọi AI."
        elif count >= AUTO_LEARN_PROMOTE_THRESHOLD:
            # Promote once. Prefer the planner-authored generalization for quality.
            promoted_generalized = None
            try:
                planner_model_for_learn = MODEL_CONFIG.get("planner_model") or MODEL_DEFAULTS["planner_model"]
                promoted_generalized = await llm_generalize_workflow(
                    planner_model_for_learn, original_task, last_history,
                )
            except Exception as exc:
                print(f"[handle_rating_feedback] planner generalization skipped: {exc}")
                promoted_generalized = None
            promoted_generalized = promoted_generalized or base
            promoted_id = promote_auto_learned_workflow(domain, promoted_generalized, original_task, user_prof)
            if promoted_id:
                mark_auto_learning_promoted(domain, signature, promoted_id)
                message = "Đã tự học xong! Lần sau loại lệnh này sẽ tự chạy bằng quy trình đã học (không cần gọi AI)."
            else:
                message = f"Đã ghi nhận hài lòng ({count} lần). Quy trình chưa đủ an toàn để tự kích hoạt — cần chạy ổn định thêm."
        else:
            remaining = max(0, AUTO_LEARN_PROMOTE_THRESHOLD - count)
            message = (
                f"Đã ghi nhận hài lòng. Hệ thống đang tự học ({count}/{AUTO_LEARN_PROMOTE_THRESHOLD}) — "
                f"thêm {remaining} lần nữa cho lệnh tương tự là tự chạy không cần gọi AI."
            )

        session.pop("last_history", None)
        return {
            "ok": True,
            "message": message,
            "auto_learn": {
                "count": count,
                "promoted": bool(promoted_id) or bool(candidate.get("promoted")),
                "threshold": AUTO_LEARN_PROMOTE_THRESHOLD,
            },
        }
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": f"Lỗi khi tự học: {str(exc)}"}


# ── Workflow composition: big task → multiple 0-LLM sub-workflows (request #6) ─
def find_workflow_by_id(workflow_id: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """Look up a saved workflow by id across all site domains."""
    wid = str(workflow_id or "").strip()
    if not wid:
        return None, None
    profiles = load_site_profiles()
    for domain, workflows in profiles.items():
        for wf in workflows:
            if wf.get("workflow_id") == wid:
                return domain, wf
    return None, None


def composition_needs_agent_continuation(task: str, composition_plan: list[dict]) -> bool:
    """True when sub-workflow replays alone are unlikely to finish the full user task.

    The composition router may only map *some* phases to saved workflows (rule 2 in
    plan_workflow_composition). In that case the Agent must continue for the tail.
    """
    if not composition_plan:
        return False
    normalized = semantic_prompt(task or "")
    plan_blob = semantic_prompt(" ".join(str(s.get("task") or "") for s in composition_plan))

    chain_markers = [
        "roi", "sau do", "tiep tuc", "xong thi", "va sau", "dong thoi",
        "and then", "then ",
    ]
    has_chain = any(m in normalized for m in chain_markers)

    remainder_hints = [
        "google doc", "google docs", "docs", "tao file", "tao 1 file", "tai file",
        "tong hop", "liet ke", "email", "gui ", "luu file", "bo tri", "dinh dang",
    ]
    task_wants = [h for h in remainder_hints if h in normalized]
    if task_wants and not any(h in plan_blob for h in task_wants):
        return True
    if has_chain and len(composition_plan) < 2:
        return True
    return False


ACTION_CAPABILITY_VI: dict[str, str] = {
    "navigate": "Mở/điều hướng tới trang web",
    "fill": "Điền ô nhập liệu / form",
    "activate": "Bấm nút, link, menu",
    "keyboard": "Gõ phím (Enter, Tab…)",
    "scroll": "Cuộn danh sách / dropdown",
    "extract": "Trích xuất nội dung trang hiện tại",
    "wait": "Chờ trang tải",
    "done": "Kết thúc và báo cáo",
    "native": "Thao tác trình duyệt khác",
}


def summarize_workflow_steps(wf: dict) -> str:
    """Short human-readable chain of what a saved workflow actually does."""
    steps = wf.get("steps") or []
    parts: list[str] = []
    for step in steps[:8]:
        if not isinstance(step, dict) or not step:
            continue
        action_name = next(iter(step.keys()), "")
        params = step.get(action_name) or {}
        cap = ACTION_CAPABILITY_VI.get(
            replay_capability_for(action_name),
            action_name,
        )
        if action_name == "go_to_url" and isinstance(params, dict):
            url = str(params.get("url") or "")[:48]
            parts.append(f"{cap} ({url})" if url else cap)
        elif action_name == "extract_content" and isinstance(params, dict):
            goal = str(params.get("goal") or "")[:72]
            parts.append(f"{cap}: {goal}" if goal else cap)
        elif action_name == "done":
            continue
        else:
            parts.append(cap)
    if len(steps) > 8:
        parts.append(f"…(+{len(steps) - 8} bước)")
    return " → ".join(parts) if parts else "(chưa có bước)"


def infer_workflow_capability_bounds(wf: dict, domain: str = "") -> tuple[list[str], list[str]]:
    """What a workflow can and cannot do — helps the router pick workflow vs agent."""
    metadata = wf.get("metadata") or {}
    action_names: set[str] = set(metadata.get("action_names") or [])
    for step in wf.get("steps") or []:
        if isinstance(step, dict) and step:
            action_names.add(next(iter(step.keys()), ""))

    caps: set[str] = set()
    for name in action_names:
        caps.add(replay_capability_for(name))

    can_do: list[str] = []
    if "navigate" in caps:
        dom = domain or metadata.get("domain") or ""
        can_do.append(f"Mở trang {dom}" if dom else "Mở trang web cụ thể")
    if "fill" in caps and "activate" in caps:
        can_do.append("Điền form và gửi (đăng nhập, tìm kiếm…)")
    elif "fill" in caps:
        can_do.append("Điền ô nhập / tìm kiếm")
    if "extract" in caps:
        can_do.append("Lấy nội dung từ trang hiện tại (không phân tích sâu)")
    if "scroll" in caps:
        can_do.append("Cuộn danh sách / dropdown")
    if "activate" in caps and "fill" not in caps:
        can_do.append("Bấm nút / chọn mục menu")

    intent = str(metadata.get("intent") or wf.get("prompt_template") or "").lower()
    if "google" in intent or "tim" in intent:
        can_do.append("Tìm kiếm trên Google theo mẫu đã học")

    cannot_do: list[str] = []
    dom_lc = (domain or "").lower()
    if "docs.google" not in dom_lc and "document" not in intent:
        cannot_do.append("Không tạo / định dạng Google Doc")
    if "extract" not in caps:
        cannot_do.append("Không tổng hợp hay viết lại nội dung dài")
    if len(action_names) <= 5 and "extract" in caps and "fill" in caps:
        cannot_do.append("Không gánh cả chuỗi nhiều bước — chỉ đúng phần đã học")
    cannot_do.extend(str(x) for x in (metadata.get("avoid_when") or []) if x)

    return can_do or ["Thao tác trình duyệt theo kịch bản đã lưu"], cannot_do


def workflow_reliability_label(wf: dict) -> str:
    stats = wf.get("stats") or {}
    success = int(stats.get("success_count") or 0)
    fail = int(stats.get("fail_count") or 0)
    conf = float(wf.get("confidence") or 0)
    if success >= 2 and fail == 0:
        return f"ổn định ({success} lần thành công)"
    if fail >= 2 and success == 0:
        return f"hay lỗi ({fail} lần thất bại)"
    if conf >= 0.75:
        return f"khá tin cậy (confidence {conf:.0%})"
    return f"confidence {conf:.0%}"


def build_workflow_capability_card(wf: dict, domain: str) -> dict[str, Any]:
    """Rich capability description for orchestrator / planner routing."""
    metadata = wf.get("metadata") or {}
    can_do, cannot_do = infer_workflow_capability_bounds(wf, domain)
    return {
        "workflow_id": wf.get("workflow_id"),
        "name": wf.get("workflow_name"),
        "domain": domain,
        "prompt_template": wf.get("prompt_template"),
        "variables": wf.get("variables") or [],
        "intent": metadata.get("intent") or wf.get("prompt_template"),
        "description": metadata.get("description") or "",
        "examples": (metadata.get("examples") or [])[:4],
        "step_summary": summarize_workflow_steps(wf),
        "can_do": can_do,
        "cannot_do": cannot_do,
        "reliability": workflow_reliability_label(wf),
        "success_criteria": metadata.get("success_criteria") or "",
    }


def split_task_into_segments(task: str) -> list[str]:
    """Split a compound user task into segments that may map to one workflow each."""
    text = (task or "").strip()
    if not text:
        return []
    pattern = (
        r"\s+(?:rồi|roi|sau đó|sau do|xong thì|xong thi|tiếp tục|tiep tuc|"
        r"và sau|va sau|đồng thời|dong thoi|and then|then)\s+"
    )
    parts = re.split(pattern, text, flags=re.IGNORECASE)
    segments: list[str] = []
    seen: set[str] = set()
    for part in parts:
        clean = part.strip()
        key = semantic_prompt(clean)
        if clean and key not in seen:
            seen.add(key)
            segments.append(clean)
    return segments


def infer_workflow_subtask_candidates(original_task: str, completed_phases: list[dict]) -> list[str]:
    """Generate sub-task phrases to match against saved workflow templates."""
    remaining = (
        orchestrator_agent_sub_task(original_task, completed_phases)
        if completed_phases
        else original_task
    )
    candidates: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        clean = (text or "").strip()
        key = semantic_prompt(clean)
        if clean and key not in seen and len(clean) <= 280:
            seen.add(key)
            candidates.append(clean)

    add(remaining)
    for seg in split_task_into_segments(original_task):
        add(seg)
    for seg in split_task_into_segments(remaining):
        add(seg)

    folded = semantic_prompt(original_task)
    if any(m in folded for m in ("google", "tim kiem", "tim ", "search", "tìm")):
        slots = extract_semantic_slots(original_task)
        query = slots.get("search_query")
        if query:
            add(f"Vao Google tim {query}")
        match = re.search(
            r"(?:tim|tìm|search)\s+(.+?)(?:\s+(?:roi|rồi|va|và|sau)|$)",
            original_task,
            flags=re.IGNORECASE,
        )
        if match:
            topic = normalize_text(match.group(1))
            if topic:
                add(f"Vao Google tim {topic}")

    research_markers = (
        "tong hop", "tổng hợp", "thong tin", "thông tin", "tim hieu", "tìm hiểu",
        "chi tiet ve", "chi tiết về", "ve chung khoan", "về chứng khoán",
    )
    if any(m in folded for m in research_markers):
        segs = split_task_into_segments(original_task)
        first_seg = segs[0] if segs else original_task
        topic = re.sub(
            r"^(?:tong hop|tổng hợp|tim hieu|tìm hiểu)\s*(?:\d+\s+)?",
            "",
            first_seg.strip(),
            flags=re.IGNORECASE,
        ).strip()
        topic = re.sub(r"\s+(?:roi|rồi).*$", "", topic, flags=re.IGNORECASE).strip()
        if topic and len(topic) >= 4:
            add(f"Vao Google tim {topic}")
    return candidates


def try_segment_workflow_route(
    user_profile: str,
    candidates: list[str],
    current_url: str = "",
    context_text: str = "",
) -> dict | None:
    """Try matching workflow templates against decomposed sub-tasks (no LLM)."""
    for cand in candidates:
        route = try_fast_workflow_route(
            user_profile,
            cand,
            current_url=current_url,
            context_text=context_text,
        )
        if route and route.get("action") == "workflow":
            route["sub_task"] = cand
            route["reason"] = "segment_workflow_match"
            return route
    return None


def format_catalog_for_planner(catalog: list[dict]) -> str:
    """Readable capability list so the planner knows what each workflow can do."""
    if not catalog:
        return "(Không có workflow đã học — mọi bước phải dùng Agent.)"
    blocks: list[str] = []
    for i, c in enumerate(catalog, start=1):
        can = "; ".join(c.get("can_do") or [])
        cannot = "; ".join(c.get("cannot_do") or [])
        examples = ", ".join(f"«{e}»" for e in (c.get("examples") or [])[:3])
        blocks.append(
            f"{i}. [{c.get('workflow_id')}] {c.get('name')}\n"
            f"   Mẫu lệnh: {c.get('prompt_template')}\n"
            f"   Biến: {', '.join(c.get('variables') or []) or '(không)'}\n"
            f"   Làm được: {can}\n"
            f"   KHÔNG làm được: {cannot}\n"
            f"   Các bước: {c.get('step_summary')}\n"
            f"   Độ tin cậy: {c.get('reliability')}\n"
            f"   Ví dụ khớp: {examples or '(theo mẫu lệnh)'}"
        )
    return "\n\n".join(blocks)


def build_agent_routing_hint(
    original_task: str,
    completed_phases: list[dict],
    catalog: list[dict],
    sub_task: str,
    reason: str,
) -> str:
    """Tell the agent what to do now and which workflows were intentionally skipped."""
    done = build_orchestrator_completed_summary(completed_phases)
    wf_lines = []
    for c in (catalog or [])[:12]:
        cannot = ", ".join((c.get("cannot_do") or [])[:2])
        wf_lines.append(
            f"- {c.get('name')}: làm được «{'; '.join((c.get('can_do') or [])[:2])}»"
            + (f"; không: {cannot}" if cannot else "")
        )
    wf_block = "\n".join(wf_lines) if wf_lines else "(không có workflow)"
    return (
        f"[Điều phối — pha Agent]\n"
        f"Lý do chọn Agent: {reason}\n"
        f"Công việc GỐC: «{original_task}»\n"
        f"Đã xong:\n{done or '(chưa có)'}\n"
        f"Workflow có sẵn (đã cân nhắc, không dùng cho pha này):\n{wf_block}\n"
        f"PHA NÀY chỉ làm: «{sub_task}» — không lặp phần đã xong, không kết luận done sớm."
    )


def build_orchestrator_completed_summary(completed_phases: list[dict]) -> str:
    """Compact log of finished orchestrator phases for routing and agent context."""
    if not completed_phases:
        return ""
    lines: list[str] = []
    for i, phase in enumerate(completed_phases, start=1):
        kind = str(phase.get("kind") or "phase")
        summary = str(phase.get("summary") or "").strip()
        if len(summary) > 320:
            summary = summary[:317] + "..."
        name = phase.get("workflow_name") or phase.get("workflow_id") or ""
        prefix = f"[{kind}]"
        if name:
            prefix = f"[{kind}:{name}]"
        lines.append(f"{i}. {prefix} {summary}" if summary else f"{i}. {prefix}")
    return "\n".join(lines)


def workflow_phase_covers_task(original_task: str, phase_sub_task: str) -> bool:
    """True when a workflow phase sub-task satisfies the whole user request."""
    orig = semantic_prompt(original_task)
    sub = semantic_prompt(phase_sub_task)
    if not orig or not sub:
        return False
    if sub == orig:
        return True
    if len(split_task_into_segments(original_task)) <= 1:
        return orig.startswith(sub) or sub.startswith(orig)
    return False


def orchestrator_should_finish_after_workflow(
    original_task: str,
    phase_sub_task: str,
    completed_phases: list[dict],
    use_orchestrator_planner: bool,
) -> bool:
    """When AI orchestrator is on, always let it decide the next phase (including done)."""
    if use_orchestrator_planner:
        return False
    if not completed_phases:
        return False
    if workflow_phase_covers_task(original_task, phase_sub_task):
        return True
    return True


def orchestrator_agent_sub_task(original_task: str, completed_phases: list[dict]) -> str:
    """Scope the agent to whatever is left after prior workflow/agent phases."""
    if not completed_phases:
        return original_task
    done_summary = build_orchestrator_completed_summary(completed_phases)
    return (
        f"Công việc gốc: «{original_task}»\n"
        f"Đã hoàn thành:\n{done_summary}\n"
        "Hãy tiếp tục từ trạng thái trình duyệt hiện tại và hoàn thành phần còn lại."
    )


def count_trailing_agent_phases(completed_phases: list[dict]) -> int:
    count = 0
    for phase in reversed(completed_phases):
        if phase.get("kind") == "agent":
            count += 1
        else:
            break
    return count


def agent_summaries_similar(left: str, right: str) -> bool:
    sa = semantic_prompt(left)
    sb = semantic_prompt(right)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    words_a = set(sa.split())
    words_b = set(sb.split())
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
    return overlap >= 0.62


def agent_signals_blocked(final_text: str) -> bool:
    folded = semantic_prompt(final_text)
    return any(marker in folded for marker in AGENT_BLOCKED_MARKERS)


def detect_orchestrator_agent_loop(
    completed_phases: list[dict],
    next_action: str,
    next_sub_task: str = "",
) -> tuple[bool, str]:
    """True when the orchestrator would spin on repeated agent phases."""
    if str(next_action or "").strip().lower() != "agent":
        return False, ""
    trailing = count_trailing_agent_phases(completed_phases)
    if trailing >= MAX_CONSECUTIVE_AGENT_PHASES:
        return True, "orchestrator_max_agent_phases"
    agent_phases = [p for p in completed_phases if p.get("kind") == "agent"]
    if trailing >= 2 and len(agent_phases) >= 2:
        recent = agent_phases[-2:]
        if agent_summaries_similar(
            str(recent[0].get("summary") or ""),
            str(recent[1].get("summary") or ""),
        ):
            return True, "orchestrator_agent_repeat_summary"
    last_agent = agent_phases[-1] if agent_phases else None
    if last_agent and trailing >= 1:
        last_key = semantic_prompt(
            str(last_agent.get("sub_task") or last_agent.get("summary") or ""),
        )
        next_key = semantic_prompt(str(next_sub_task or ""))
        if last_key and next_key and (
            last_key == next_key
            or last_key in next_key
            or next_key in last_key
        ):
            return True, "orchestrator_same_subtask"
    return False, ""


def build_orchestrator_stop_message(
    original_task: str,
    completed_phases: list[dict],
    reason: str,
    latest_text: str = "",
) -> str:
    summaries = [str(p.get("summary") or "").strip() for p in completed_phases if p.get("summary")]
    reason_vi = {
        "orchestrator_agent_repeat_summary": "AI lặp lại cùng kết quả nhiều lần, không tiến triển thêm",
        "orchestrator_max_agent_phases": (
            f"Đã chạy {MAX_CONSECUTIVE_AGENT_PHASES} pha Agent liên tiếp mà vẫn chưa hoàn tất toàn bộ"
        ),
        "orchestrator_same_subtask": "Planner muốn chạy lại đúng phần việc vừa thử",
        "agent_blocked": "Agent báo không thể tiếp tục (cần đăng nhập, quyền, hoặc trang chặn thao tác)",
    }.get(reason, reason)
    parts: list[str] = []
    if summaries:
        parts.append("Đã làm được:\n" + "\n".join(f"• {s}" for s in summaries if s))
    if latest_text and latest_text.strip() not in summaries:
        parts.append(latest_text.strip())
    parts.append(f"⚠️ Dừng điều phối: {reason_vi}.")
    if len(split_task_into_segments(original_task)) > 1:
        parts.append(
            "Gợi ý: tách lệnh thành từng bước riêng (vd. tìm thông tin trước, tạo Google Doc sau)."
        )
    return "\n\n".join(p for p in parts if p).strip()


def guard_orchestrator_route(
    route: dict,
    *,
    original_task: str,
    completed_phases: list[dict],
    latest_agent_text: str = "",
) -> dict:
    """Override planner/agent routing when the orchestrator is clearly stuck."""
    if agent_signals_blocked(latest_agent_text):
        return {
            "action": "done",
            "sub_task": build_orchestrator_stop_message(
                original_task, completed_phases, "agent_blocked", latest_agent_text,
            ),
            "workflow_id": None,
            "reason": "agent_blocked",
            "hint": "",
        }
    looping, loop_reason = detect_orchestrator_agent_loop(
        completed_phases,
        str(route.get("action") or ""),
        str(route.get("sub_task") or ""),
    )
    if looping:
        return {
            "action": "done",
            "sub_task": build_orchestrator_stop_message(
                original_task, completed_phases, loop_reason, latest_agent_text,
            ),
            "workflow_id": None,
            "reason": loop_reason,
            "hint": "",
        }
    return route


def try_fast_workflow_route(
    user_profile: str,
    sub_task: str,
    current_url: str = "",
    context_text: str = "",
    *,
    explicit_workflow_id: str | None = None,
) -> dict | None:
    """Zero-LLM route: return a workflow phase when match quality is sufficient."""
    workflow_match: dict | None = None
    is_explicit = False

    if explicit_workflow_id:
        domain, wf = find_workflow_by_id(explicit_workflow_id)
        if wf:
            template = wf.get("prompt_template", "")
            try:
                _, var_values, _ = match_prompt_to_template_relaxed(template, sub_task)
            except Exception:
                var_values = {}
            workflow_match = build_workflow_match(
                domain=domain,
                wf=wf,
                var_values=var_values,
                score=100.0,
                match_type="explicit_id",
            )
            is_explicit = True

    if not workflow_match:
        workflow_match = find_matching_workflow(
            user_profile,
            sub_task,
            current_url=current_url,
            context_text=context_text,
        )

    if not workflow_match:
        return None

    replay_allowed, replay_reason = should_replay_workflow(workflow_match, explicit=is_explicit)
    if not replay_allowed:
        return {
            "action": "agent",
            "sub_task": sub_task,
            "workflow_id": None,
            "match": workflow_match,
            "reason": f"workflow_skip:{replay_reason}",
            "hint": (
                f"[Workflow '{(workflow_match.get('workflow') or {}).get('workflow_name', 'workflow')}' "
                f"khớp nhưng bị bỏ qua: {replay_reason}]\n"
                "Hãy xử lý bằng Agent."
            ),
        }

    wf = workflow_match.get("workflow") or {}
    return {
        "action": "workflow",
        "sub_task": sub_task,
        "workflow_id": wf.get("workflow_id"),
        "match": workflow_match,
        "reason": "fast_workflow_match",
        "hint": "",
    }


def build_workflow_match_hints(
    user_profile: str,
    original_task: str,
    completed_phases: list[dict],
    *,
    current_url: str = "",
    context_text: str = "",
) -> str:
    """Technical match hints for the orchestrator — suggestions only, not decisions."""
    lines: list[str] = []
    seen_ids: set[str] = set()
    remaining = (
        orchestrator_agent_sub_task(original_task, completed_phases)
        if completed_phases
        else original_task
    )
    for label, task in [("phần còn lại", remaining), ("công việc gốc", original_task)]:
        match = find_matching_workflow(
            user_profile, task, current_url=current_url, context_text=context_text,
        )
        if not match:
            continue
        wf = match.get("workflow") or {}
        wid = str(wf.get("workflow_id") or "")
        if not wid or wid in seen_ids:
            continue
        seen_ids.add(wid)
        lines.append(
            f"- [{wid}] {wf.get('workflow_name')} — khớp mẫu kỹ thuật ({match.get('match_type')}) "
            f"cho «{label}»; biến gợi ý: {match.get('variables') or {}}"
        )
    for cand in infer_workflow_subtask_candidates(original_task, completed_phases)[:6]:
        match = find_matching_workflow(
            user_profile, cand, current_url=current_url, context_text=context_text,
        )
        if not match:
            continue
        wf = match.get("workflow") or {}
        wid = str(wf.get("workflow_id") or "")
        if not wid or wid in seen_ids:
            continue
        seen_ids.add(wid)
        lines.append(f"- [{wid}] {wf.get('workflow_name')} — đoạn con «{cand[:96]}»")
    if not lines:
        return (
            "(Không có gợi ý khớp mẫu tự động — quyết định theo ý định người dùng và capability cards.)"
        )
    return (
        "\n".join(lines)
        + "\n\n(Lưu ý: gợi ý trên chỉ từ khớp kỹ thuật — BẠN là người quyết định cuối cùng.)"
    )


def finalize_planned_orchestrator_route(
    planned: dict,
    *,
    original_task: str,
    completed_phases: list[dict],
    catalog: list[dict],
    remaining_task: str,
) -> dict:
    """Turn planner JSON into an executable route (workflow replay, agent, or done)."""
    if planned.get("action") == "done":
        return planned

    if planned.get("action") == "workflow" and planned.get("workflow_id"):
        domain, wf = find_workflow_by_id(planned.get("workflow_id"))
        if wf:
            var_values = dict(planned.get("variables") or {})
            if not var_values:
                template = wf.get("prompt_template", "")
                try:
                    _, var_values, _ = match_prompt_to_template_relaxed(
                        template, planned.get("sub_task") or "",
                    )
                except Exception:
                    var_values = {}
            match = build_workflow_match(
                domain=domain,
                wf=wf,
                var_values=var_values,
                score=100.0,
                match_type="orchestrator",
            )
            replay_allowed, replay_reason = should_replay_workflow(match, explicit=False)
            if replay_allowed:
                planned["match"] = match
                return planned
            planned = {
                "action": "agent",
                "sub_task": planned.get("sub_task") or remaining_task,
                "workflow_id": None,
                "reason": f"workflow_skip:{replay_reason}",
                "hint": (
                    f"[Workflow '{wf.get('workflow_name', planned.get('workflow_id'))}' "
                    f"AI chọn nhưng không replay được: {replay_reason}]\n"
                    "Hãy xử lý pha này bằng Agent."
                ),
            }

    if planned.get("action") == "agent":
        if not planned.get("hint"):
            planned["hint"] = build_agent_routing_hint(
                original_task,
                completed_phases,
                catalog,
                planned.get("sub_task") or remaining_task,
                planned.get("reason") or "planner_agent",
            )
        return planned

    return {
        "action": "agent",
        "sub_task": planned.get("sub_task") or remaining_task,
        "workflow_id": None,
        "reason": planned.get("reason") or "planner_fallback_agent",
        "hint": build_agent_routing_hint(
            original_task,
            completed_phases,
            catalog,
            planned.get("sub_task") or remaining_task,
            planned.get("reason") or "planner_fallback_agent",
        ),
    }


async def plan_next_orchestrator_phase(
    original_task: str,
    completed_phases: list[dict],
    catalog: list[dict],
    planner_model: str,
    current_url: str = "",
    callback_handler: Any = None,
    match_hints: str = "",
) -> dict:
    """AI orchestrator: sole decision-maker for workflow vs agent vs done."""
    completed_summary = build_orchestrator_completed_summary(completed_phases)
    remaining = orchestrator_agent_sub_task(original_task, completed_phases)
    segment_hints = infer_workflow_subtask_candidates(original_task, completed_phases)
    catalog_text = format_catalog_for_planner(catalog)
    prompt = (
        "Bạn là BỘ ĐIỀU PHỐI AI — người quyết định DUY NHẤT cho pha kế tiếp.\n"
        "Hệ thống KHÔNG tự chọn workflow; chỉ bạn mới quyết định dùng workflow đã học hay Agent.\n"
        "So khớp theo Ý ĐỊNH người dùng và KHẢ NĂNG workflow — KHÔNG cần khớp chữ từng chữ một với mẫu lệnh.\n"
        "Ví dụ: «Kiểm tra thời tiết Hồ Chí Minh» = «Kiểm tra thời tiết tại Hồ Chí Minh» nếu workflow biết làm việc đó.\n\n"
        f"CÔNG VIỆC GỐC:\n{original_task}\n\n"
        f"PHẦN CÒN LẠI (sau các pha đã xong):\n{remaining}\n\n"
        f"URL trình duyệt: {current_url or '(không rõ)'}\n\n"
        f"ĐÃ HOÀN THÀNH:\n{completed_summary or '(chưa có gì)'}\n\n"
        f"GỢI Ý ĐOẠN CON (tham khảo):\n"
        + ("\n".join(f"- {s}" for s in segment_hints[:8]) or "(không tách được)") + "\n\n"
        f"GỢI Ý KHỚP KỸ THUẬT (tham khảo, KHÔNG bắt buộc):\n{match_hints or '(không có)'}\n\n"
        f"DANH SÁCH WORKFLOW & KHẢ NĂNG:\n{catalog_text}\n\n"
        "CÁCH SUY LUẬN (bắt buộc theo thứ tự):\n"
        "1. Hiểu người dùng THỰC SỰ muốn gì — kể cả cách diễn đạt khác mẫu lệnh.\n"
        "2. Với TỪNG workflow: đọc «Làm được» và «KHÔNG làm được». "
        "Chọn workflow chỉ khi pha kế tiếp nằm TRONG «Làm được».\n"
        "3. Một workflow chỉ cover MỘT đoạn — không gán cả chuỗi nhiều bước vào workflow một bước.\n"
        "4. Chọn workflow → điền workflow_id, sub_task mô tả pha, và variables (biến thật).\n"
        "5. Chọn agent khi không có workflow phù hợp, workflow không tin cậy, hoặc cần sáng tạo/xử lý lỗi.\n"
        "6. done khi MỌI mục tiêu gốc đã xong — HOẶC khi phần còn lại không làm được tự động "
        "(cần đăng nhập/quyền, Google Doc, thanh toán…) và đã thử agent ít nhất một lần.\n"
        "7. KHÔNG gửi agent lại nếu pha agent vừa rồi đã báo không thể / cần người dùng — trả done "
        "và ghi rõ phần chưa làm được.\n"
        "8. KHÔNG lặp cùng sub_task agent — nếu không tiến triển, trả done thay vì agent lần nữa.\n\n"
        "Trả về DUY NHẤT JSON:\n"
        '{"action":"workflow|agent|done",'
        '"sub_task":"lệnh cụ thể cho pha này",'
        '"workflow_id":"id hoặc null",'
        '"variables":{"tên_biến":"giá trị"} hoặc {},'
        '"reason":"giải thích ngắn: mục tiêu + vì sao workflow/agent/done",'
        '"remaining_after":"còn phải làm gì sau pha này (hoặc rỗng nếu done)"}\n'
    )
    try:
        llm = make_llm(planner_model, callback_handler)
        response = await llm.ainvoke(prompt)
        text = getattr(response, "content", None)
        if isinstance(text, list):
            text = " ".join(str(p) for p in text)
        text = text if isinstance(text, str) else str(response)
    except Exception as exc:
        print(f"[plan_next_orchestrator_phase] LLM failed: {exc}")
        return {
            "action": "agent",
            "sub_task": orchestrator_agent_sub_task(original_task, completed_phases),
            "workflow_id": None,
            "reason": "planner_failed",
            "hint": "",
        }

    obj = _extract_json_object(text)
    if not obj:
        return {
            "action": "agent",
            "sub_task": orchestrator_agent_sub_task(original_task, completed_phases),
            "workflow_id": None,
            "reason": "planner_parse_failed",
            "hint": "",
        }

    action = str(obj.get("action") or "agent").strip().lower()
    sub_task = str(obj.get("task") or obj.get("sub_task") or "").strip()
    workflow_id = obj.get("workflow_id")
    reason = str(obj.get("reason") or "").strip()

    if action == "done":
        return {
            "action": "done",
            "sub_task": sub_task or "Hoàn thành công việc.",
            "workflow_id": None,
            "reason": reason or "planner_done",
            "hint": "",
        }

    valid_ids = {c["workflow_id"] for c in (catalog or [])}
    var_values: dict[str, str] = {}
    raw_vars = obj.get("variables")
    if isinstance(raw_vars, dict):
        var_values = {str(k): str(v) for k, v in raw_vars.items() if v is not None and str(v).strip()}
    if action == "workflow" and workflow_id in valid_ids and sub_task:
        return {
            "action": "workflow",
            "sub_task": sub_task,
            "workflow_id": workflow_id,
            "variables": var_values,
            "match": None,
            "reason": reason or "planner_workflow",
            "hint": "",
        }

    if not sub_task:
        sub_task = orchestrator_agent_sub_task(original_task, completed_phases)
    return {
        "action": "agent",
        "sub_task": sub_task,
        "workflow_id": None,
        "reason": reason or "planner_agent",
        "hint": build_agent_routing_hint(
            original_task, completed_phases, catalog, sub_task, reason or "planner_agent",
        ),
    }


async def resolve_orchestrator_route(
    original_task: str,
    completed_phases: list[dict],
    user_profile: str,
    catalog: list[dict],
    current_url: str,
    context_text: str,
    planner_model: str,
    *,
    explicit_workflow_id: str | None = None,
    phase_index: int = 1,
    planner_callback: Any = None,
    use_orchestrator_planner: bool = True,
) -> dict:
    """AI orchestrator decides the next phase. Template matching is hints-only."""
    remaining_task = orchestrator_agent_sub_task(original_task, completed_phases)
    fast_task = original_task if not completed_phases else remaining_task

    if phase_index == 1 and explicit_workflow_id:
        explicit_route = try_fast_workflow_route(
            user_profile,
            fast_task,
            current_url=current_url,
            context_text=context_text,
            explicit_workflow_id=explicit_workflow_id,
        )
        if explicit_route:
            explicit_route["reason"] = "explicit_workflow_id"
            return explicit_route

    if use_orchestrator_planner:
        match_hints = build_workflow_match_hints(
            user_profile,
            original_task,
            completed_phases,
            current_url=current_url,
            context_text=context_text,
        )
        planned = await plan_next_orchestrator_phase(
            original_task,
            completed_phases,
            catalog,
            planner_model,
            current_url=current_url,
            callback_handler=planner_callback,
            match_hints=match_hints,
        )
        return finalize_planned_orchestrator_route(
            planned,
            original_task=original_task,
            completed_phases=completed_phases,
            catalog=catalog,
            remaining_task=remaining_task,
        )

    return {
        "action": "agent",
        "sub_task": remaining_task,
        "workflow_id": None,
        "reason": "orchestrator_planner_disabled",
        "hint": "",
    }


def build_workflow_catalog(user_profile: str) -> list[dict]:
    """Workflow list with capability cards so the router knows what each can do."""
    profiles = load_site_profiles()
    catalog: list[dict] = []
    for domain, workflows in profiles.items():
        for wf in workflows:
            owner = wf.get("user_profile")
            if owner not in (user_profile, "Default", None):
                continue
            catalog.append(build_workflow_capability_card(wf, domain))
    return catalog


async def plan_workflow_composition(task: str, catalog: list[dict], planner_model: str,
                                    callback_handler: Any = None) -> list[dict] | None:
    """Use ONE planner call to break a big task into an ordered list of known
    sub-workflows. Returns [{"workflow_id", "task"}] or None to fall back to a
    full Agent (request #6, hybrid routing)."""
    if not catalog:
        return None
    catalog_desc = json.dumps(
        [
            {"workflow_id": c["workflow_id"], "name": c["name"],
             "intent": c["intent"], "variables": c["variables"]}
            for c in catalog
        ],
        ensure_ascii=False,
    )
    prompt = (
        "Bạn là bộ điều phối (router). Người dùng giao một CÔNG VIỆC LỚN. "
        "Hãy chia nó thành chuỗi các bước, mỗi bước dùng MỘT workflow đã học sẵn để tiết kiệm chi phí "
        "(replay không cần gọi AI).\n\n"
        f"Công việc lớn:\n{task}\n\n"
        f"Danh sách workflow đã học (JSON):\n{catalog_desc}\n\n"
        "Quy tắc:\n"
        "1. CHỈ dùng workflow_id có trong danh sách. Mỗi bước kèm 'task' là câu lệnh cụ thể cho workflow đó, "
        "điền giá trị thật trích từ công việc lớn.\n"
        "2. Nếu một phần công việc KHÔNG có workflow phù hợp, bỏ qua phần đó (agent sẽ xử lý), KHÔNG bịa workflow_id.\n"
        "3. Giữ đúng thứ tự thực hiện. Nếu công việc thực ra chỉ là một việc đơn lẻ, trả về steps rỗng.\n\n"
        "Trả về DUY NHẤT JSON: {\"steps\": [{\"workflow_id\": \"...\", \"task\": \"...\"}]}\n"
    )
    try:
        llm = make_llm(planner_model, callback_handler)
        response = await llm.ainvoke(prompt)
        text = getattr(response, "content", None)
        if isinstance(text, list):
            text = " ".join(str(p) for p in text)
        text = text if isinstance(text, str) else str(response)
    except Exception as exc:
        print(f"[plan_workflow_composition] LLM failed: {exc}")
        return None

    obj = _extract_json_object(text)
    if not obj:
        return None
    steps = obj.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    valid_ids = {c["workflow_id"] for c in catalog}
    plan: list[dict] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        wid = step.get("workflow_id")
        sub_task = str(step.get("task") or "").strip()
        if wid in valid_ids and sub_task:
            plan.append({"workflow_id": wid, "task": sub_task})
    return plan or None


async def replay_workflow_for_composition(
    workflow_id: str,
    sub_task: str,
    browser_context: Any,
) -> tuple[bool, str, str]:
    """Streamlined 0-LLM replay of one known workflow as a composition sub-step.
    Returns (ok, final_text, error). Incident recovery is handled by the caller
    (which falls back to a scoped Agent for the remainder of the big task)."""
    domain, wf = find_workflow_by_id(workflow_id)
    if not wf:
        return False, "", "workflow_not_found"

    template = wf.get("prompt_template", "")
    try:
        _, var_values, _ = match_prompt_to_template_relaxed(template, sub_task)
    except Exception:
        var_values = {}

    match = build_workflow_match(
        domain=domain, wf=wf, var_values=var_values, score=100.0, match_type="composition",
    )
    try:
        page = await browser_context.get_current_page()
        current_url = page.url or ""
    except Exception:
        current_url = ""
    replay_plan = build_workflow_replay_plan(match, sub_task, current_url)
    replay_actions = [s["action"] for s in replay_plan]
    prev_fill = False
    for plan_step in replay_plan:
        action = plan_step.get("action") or {}
        action_name, params = action_name_and_params(action)
        if not action_name:
            continue
        if action_name == "done":
            break
        try:
            result = await execute_semantic_replay_step(
                plan_step, browser_context,
                after_text_input=prev_fill,
                hint=runtime_hint_for_step(match, plan_step),
            )
        except Exception as exc:
            return False, "", f"{action_name}: {exc}"
        if not result.ok:
            return False, "", result.error or f"{action_name} failed"
        if result.done:
            return True, (result.final_text or replay_final_text(match, replay_actions, sub_task)), ""
        prev_fill = replay_capability_for(action_name) == "fill"
        await wait_for_replay_settle(action_name, browser_context)
    return True, replay_final_text(match, replay_actions, sub_task), ""


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
# Strategic guidance injected into the planner LLM (browser_use
# extend_planner_system_message). The planner only fires every few steps, so its
# job is to keep the executor on the cheapest successful path: stable selectors,
# right scroll scope, no blind retries, stop as soon as the goal is met.
PLANNER_GUIDANCE = (
    "Bạn là planner chiến lược cho một web agent. Mục tiêu: giữ executor đi đúng hướng "
    "với ÍT bước và ÍT lần gọi model nhất.\n"
    "Nguyên tắc:\n"
    "1. Chia mục tiêu lớn thành các mục tiêu con rõ ràng, có thứ tự; nêu rõ mục tiêu con hiện tại và điều kiện hoàn thành.\n"
    "2. Ưu tiên hành động ổn định: chọn phần tử theo text/nhãn hiển thị hoặc URL thay vì chỉ số (index) dễ thay đổi giữa các lần tải.\n"
    "3. Cuộn đúng phạm vi: trước khi kiểm tra danh sách dài/dropdown, yêu cầu executor gọi list_scrollable_regions; "
    "cuộn bằng smart_scroll (index mốc TRONG danh sách hoặc target_text). "
    "TUYỆT ĐỐI không kết luận «đã xem hết» nếu action trả ⚠️/noscroll.\n"
    "4. Theo dõi checkpoint đã đạt để không lặp lại bước đã xong; nếu một mục tiêu con đã hoàn thành, chuyển ngay sang mục tiêu kế tiếp.\n"
    "5. Nếu executor lặp lại một hành động mà không tiến triển hoặc lỗi nhiều lần, hãy ĐỔI chiến lược (đường khác, selector khác) hoặc đề nghị hỏi người dùng — tuyệt đối không thử mù.\n"
    "6. Khi toàn bộ mục tiêu đã đạt, yêu cầu executor gọi done ngay, không thao tác thừa.\n"
    "Trả lời ngắn gọn, tập trung vào bước kế tiếp và lý do."
)


def resolve_planner_interval(task: str) -> int:
    """Plan more often for long/marathon tasks, less often for ordinary ones.

    Fewer planner invocations on healthy multi-step tasks directly reduces token
    spend; marathon/monitor tasks benefit from tighter strategic steering.
    """
    normalized = (task or "").lower()
    hard_markers = [
        "liên tục", "theo dõi", "lặp lại", "cho đến khi", "trong vòng",
        "suốt", "hàng giờ", "nhiều giờ", "cả ngày", "mỗi",
        "monitor", "watch", "loop", "repeat", "until", "keep",
        "automatically", "hourly", "for hours", "all day",
    ]
    if any(m in normalized for m in hard_markers):
        return PLANNER_INTERVAL_HARD
    return PLANNER_INTERVAL_NORMAL


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
        "smart_scroll":   "cuộn thông minh (trang/vùng)",
        "list_scrollable_regions": "liệt kê vùng có thể cuộn",
        "scroll_element": "cuộn vùng theo index",
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
        "smart_scroll":   "Đang cuộn đúng vùng (trang hoặc danh sách con).",
        "list_scrollable_regions": "Đang xác định vùng nào trên trang có thể cuộn.",
        "scroll_element": "Đang cuộn vùng chứa phần tử theo index.",
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
        if primary_action in {"scroll_down", "scroll_up", "smart_scroll", "scroll_element"}:
            # Only treat repeated scrolling as "stuck" when the page genuinely is NOT
            # moving. Scrolling a long list legitimately needs many identical actions.
            if int(run_state.get("scroll_misses", 0)) < SCROLL_STUCK_MISSES:
                return None
            return (
                "Agent đã cuộn nhiều lần nhưng trang không di chuyển thêm (có thể đã ở cuối danh sách, "
                "hoặc chưa trúng đúng vùng cuộn). Hãy nói rõ tên mục cần cuộn tới, hoặc khu vực/danh sách cần cuộn."
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
    type: str | None = "custom"
    user_data_dir: str | None = None
    profile_directory: str | None = None


class FeedbackRequest(BaseModel):
    satisfied: bool
    workflow_name: str | None = None
    user_profile: str | None = None
    workflow_mode: str | None = None
    action: str | None = None  # "rate" (lightweight learn) | "save" (explicit packaging)


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
async def get_history(user_profile: str | None = None):
    history = await load_history_async()
    chats = list(history.values())
    if user_profile:
        profile_name = user_profile.strip() or "Default"
        chats = [
            chat for chat in chats
            if (chat.get("user_profile") or "Default") == profile_name
        ]
    return list(reversed(chats))

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


@app.get("/api/system_chrome_profiles")
async def get_system_chrome_profiles():
    chrome_path = find_chrome_executable()
    profiles = discover_system_chrome_profiles()
    return {
        "ok": True,
        "chrome_path": chrome_path,
        "searched_paths": [str(path) for path in chrome_user_data_dirs()],
        "profiles": profiles,
    }


@app.post("/api/profiles")
async def create_profile(req: CreateProfileRequest):
    name = req.name.strip()
    if not name:
        return {"ok": False, "error": "Tên hồ sơ không được để trống"}

    profile_type = (req.type or "custom").strip().lower()
    if profile_type not in {"custom", "system"}:
        return {"ok": False, "error": "Loại hồ sơ không hợp lệ"}
    if profile_type == "system" and (not req.user_data_dir or not req.profile_directory):
        return {"ok": False, "error": "Thiếu thông tin hồ sơ Chrome cần liên kết"}

    profiles = load_user_profiles()
    if any(profile.get("name") == name for profile in profiles):
        return {"ok": False, "error": "Hồ sơ đã tồn tại"}

    if profile_type == "custom":
        profile = {
            "name": name,
            "type": "custom",
            "user_data_dir": str((AGENT_CHROME_PROFILES_DIR / safe_profile_dir_name(name)).resolve()),
            "profile_directory": "Default",
        }
    else:
        profile = {
            "name": name,
            "type": "system",
            "user_data_dir": str(Path(req.user_data_dir or "")),
            "profile_directory": req.profile_directory or "Default",
        }

    profiles.append(profile)
    save_user_profiles(profiles)
    return {"ok": True, "profiles": profiles}


@app.get("/api/site_workflows")
async def get_site_workflows(user_profile: str | None = None):
    site_profiles = load_site_profiles()
    if not user_profile:
        return {"site_profiles": site_profiles}

    filtered: dict[str, list[dict]] = {}
    profile_name = user_profile.strip() or "Default"
    workflow_count = 0
    for domain, workflows in site_profiles.items():
        scoped = [
            wf for wf in workflows
            if (wf.get("user_profile") or "Default") == profile_name
        ]
        if scoped:
            filtered[domain] = scoped
            workflow_count += len(scoped)

    return {
        "site_profiles": filtered,
        "profile": profile_name,
        "summary": {
            "workflow_count": workflow_count,
            "domain_count": len(filtered),
        },
    }


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

    # Decide path: explicit "save" packages a permanent named workflow; "rate" is
    # the lightweight learn-to-replay path. Default keeps backward compatibility:
    # a request carrying a workflow name/mode is a save, otherwise a rating.
    action = (req.action or "").strip().lower()
    if action not in {"rate", "save"}:
        action = "save" if (req.workflow_name is not None or req.workflow_mode) else "rate"

    if action == "rate":
        return await handle_rating_feedback(session, req)

    # ── Explicit save path (💾 Lưu quy trình): named, permanent workflow ──────
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
        # Self-improvement (request #3): for general workflows, let the planner
        # author a robust generalization; fall back to the rule-based one.
        generalized = None
        if workflow_mode != "specific":
            try:
                planner_model_for_save = MODEL_CONFIG.get("planner_model") or MODEL_DEFAULTS["planner_model"]
                generalized = await llm_generalize_workflow(
                    planner_model_for_save,
                    original_task,
                    last_history,
                )
            except Exception as exc:
                print(f"[chat_feedback] planner generalization skipped: {exc}")
                generalized = None
        if not generalized:
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
        global browser_instance, current_browser_profile

        def sse(data: dict) -> str:
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        def status(text: str, phase: str, runner: str | None = None) -> str:
            payload: dict[str, Any] = {"type": "status", "text": text, "phase": phase}
            if runner in ("workflow", "agent"):
                payload["runner"] = runner
            return sse(payload)

        chat_profile = req.user_profile or "Default"
        await set_chat_profile_async(req.chat_id, chat_profile)
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

        profile_config = get_profile_config(req.user_profile)
        try:
            browser_profile_config = await asyncio.to_thread(clone_system_profile_for_agent, profile_config)
        except Exception as exc:
            msg = f"Không clone được hồ sơ Chrome '{profile_config.get('name') or 'đã chọn'}': {exc}"
            await add_message_async(req.chat_id, "assistant", msg, {"state": "error"})
            yield sse({"type": "error", "text": msg})
            return

        requested_profile_key = profile_key(browser_profile_config)
        profile_name = profile_config.get("name") or "Default"
        user_data_dir = str(browser_profile_config.get("user_data_dir") or "")
        debug_port = profile_debug_port(browser_profile_config)
        needs_new_browser = browser_instance is None or current_browser_profile != requested_profile_key
        if browser_instance is not None and not needs_new_browser and not is_tcp_port_open(debug_port):
            yield status("Phát hiện cửa sổ trình duyệt đã bị đóng, đang khởi tạo lại...", "browser_reset")
            await reset_browser_runtime()
            needs_new_browser = True

        blocking_pids = chrome_processes_using_user_data_dir(user_data_dir, debug_port) if user_data_dir else []
        if blocking_pids and needs_new_browser:
            yield status(
                f"Đang tự tắt Chrome Agent cũ để mở hồ sơ '{profile_name}'...",
                "browser_cleanup",
            )
            remaining_pids = await asyncio.to_thread(close_chrome_processes, blocking_pids)
            if remaining_pids:
                preflight_error = profile_lock_preflight_message(browser_profile_config)
                msg = preflight_error or f"Không tắt được Chrome chạy nền (PID còn lại: {', '.join(map(str, remaining_pids[:8]))})."
                await add_message_async(req.chat_id, "assistant", msg, {"state": "error"})
                yield sse({"type": "error", "text": msg})
                return
            await asyncio.sleep(1.0)
        elif blocking_pids:
            preflight_error = profile_lock_preflight_message(profile_config)
            await add_message_async(req.chat_id, "assistant", preflight_error or "Chrome vẫn đang giữ hồ sơ.", {"state": "error"})
            yield sse({"type": "error", "text": preflight_error or "Chrome vẫn đang giữ hồ sơ."})
            return

        if browser_instance is not None and current_browser_profile != requested_profile_key:
            yield status(f"Đang đổi sang hồ sơ trình duyệt: {profile_name}...", "browser")
            await close_all_sessions()
            try:
                await browser_instance.close()
            except Exception:
                pass
            browser_instance = None
            current_browser_profile = None

        created_browser      = False
        fresh_browser_context = False

        if browser_instance is None:
            yield status(f"Đang khởi động trình duyệt với hồ sơ: {profile_name}...", "browser")
            try:
                browser_instance = await with_timeout(
                    asyncio.to_thread(
                        Browser,
                        config=build_browser_config(browser_profile_config),
                    ),
                    BROWSER_START_TIMEOUT,
                    "Khởi động trình duyệt",
                )
                current_browser_profile = requested_profile_key
                created_browser = True
            except RuntimeError as exc:
                msg = browser_profile_error_message(exc, browser_profile_config)
                await add_message_async(req.chat_id, "assistant", msg, {"state": "error"})
                yield sse({"type": "error", "text": msg})
                return
        else:
            yield status(f"Đang dùng lại trình duyệt hiện có với hồ sơ: {profile_name}...", "browser")

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
        use_orchestrator_planner = True
        vision_mode     = session["vision_mode"]
        mode_text       = (
            "AI điều phối + planner executor" if use_planner
            else "AI điều phối (executor nhanh)"
        )
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
        planner_callback  = UsageMetadataCallbackHandler()
        task_state = {
            "started_at":    time.monotonic(),
            "recent_actions": [],
            "awaiting_user": False,
            "stop_requested": False,
            "question":      None,
            "current_step":  0,
            "scroll_sig":     None,
            "scroll_misses":  0,
        }

        def current_usage_summary() -> dict:
            return build_usage_summary(
                executor_model=executor_model,
                planner_model=planner_model if (use_planner or use_orchestrator_planner) else None,
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
            event["runner"] = "agent"
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

            # Track scroll progress: compare real scroll positions before/after a scroll
            # step. If positions changed, the agent IS making progress (don't interrupt
            # even on many consecutive scrolls); if unchanged, count it as a "miss".
            if primary_action in SCROLL_ACTION_NAMES:
                try:
                    bc = get_agent_browser_context(agent_ref)
                    page = await bc.get_current_page()
                    sig = await page.evaluate(SCROLL_SIGNATURE_JS)
                    if task_state["scroll_sig"] is not None and sig == task_state["scroll_sig"]:
                        task_state["scroll_misses"] += 1
                    else:
                        task_state["scroll_misses"] = 0
                    task_state["scroll_sig"] = sig
                except Exception:
                    pass
            else:
                task_state["scroll_misses"] = 0
                task_state["scroll_sig"] = None

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
        orchestrator_phases: list[dict] = []
        orchestration_meta: dict[str, Any] = {"phases": [], "mode": "flexible"}
        catalog = build_workflow_catalog(user_profile)
        agent_sub_task = req.task
        orchestrator_phase_idx = 0

        while orchestrator_phase_idx < MAX_ORCHESTRATOR_PHASES:
            orchestrator_phase_idx += 1
            workflow_replay_hint = ""
            workflow_runtime_metadata = None
            workflow_match = None

            if is_stop_requested() or session.get("stop_requested"):
                session["current_run"] = None
                async for chunk in emit_stopped_early("Da dung truoc khi dieu phoi xong."):
                    yield chunk
                return

            routing_url = ""
            try:
                page = await session["browser_context"].get_current_page()
                routing_url = page.url or ""
            except Exception:
                routing_url = ""

            yield status(
                f"AI điều phối pha {orchestrator_phase_idx}: quyết định workflow hay tự xử lý...",
                "orchestrator",
            )
            route = await resolve_orchestrator_route(
                original_task=req.task,
                completed_phases=orchestrator_phases,
                user_profile=user_profile,
                catalog=catalog,
                current_url=routing_url,
                context_text=session.get("context") or "",
                planner_model=planner_model,
                explicit_workflow_id=req.workflow_id if orchestrator_phase_idx == 1 else None,
                phase_index=orchestrator_phase_idx,
                planner_callback=planner_callback,
                use_orchestrator_planner=use_orchestrator_planner,
            )
            last_agent_summary = ""
            if orchestrator_phases and orchestrator_phases[-1].get("kind") == "agent":
                last_agent_summary = str(orchestrator_phases[-1].get("summary") or "")
            route = guard_orchestrator_route(
                route,
                original_task=req.task,
                completed_phases=orchestrator_phases,
                latest_agent_text=last_agent_summary,
            )

            route_reason = str(route.get("reason") or "").strip()
            if route_reason:
                action_label = {
                    "workflow": "Workflow",
                    "agent": "AI",
                    "done": "Hoàn tất",
                }.get(str(route.get("action") or ""), "Bước")
                decision_runner = (
                    "workflow" if route.get("action") == "workflow"
                    else "agent" if route.get("action") == "agent"
                    else None
                )
                yield status(
                    f"Quyết định: {action_label} — {route_reason[:160]}",
                    "orchestrator_decision",
                    runner=decision_runner,
                )

            if route.get("action") == "done":
                pieces = [str(p.get("summary") or "") for p in orchestrator_phases if p.get("summary")]
                final_text = str(route.get("sub_task") or "").strip()
                if not final_text:
                    final_text = "\n".join(pieces) if pieces else "Hoan thanh cong viec."
                usage = current_usage_summary()
                session["last_usage"] = usage
                metadata = {
                    "state": "done",
                    "usage": usage,
                    "orchestration": orchestration_meta,
                }
                await add_message_async(req.chat_id, "assistant", final_text, metadata)
                session["current_run"] = None
                yield sse({"type": "usage", "usage": usage})
                yield sse({"type": "done", "text": final_text, "usage": usage})
                return

            workflow_replay_hint = str(route.get("hint") or "")
            workflow_match = route.get("match")
            phase_sub_task = str(route.get("sub_task") or req.task)
            run_agent_this_phase = route.get("action") == "agent"
            is_explicit = bool(req.workflow_id and orchestrator_phase_idx == 1)

            if route.get("action") == "workflow":
                if not workflow_match and route.get("workflow_id"):
                    _d, _wf = find_workflow_by_id(route.get("workflow_id"))
                    if _wf:
                        template = _wf.get("prompt_template", "")
                        try:
                            _, var_values, _ = match_prompt_to_template_relaxed(template, phase_sub_task)
                        except Exception:
                            var_values = {}
                        workflow_match = build_workflow_match(
                            domain=_d,
                            wf=_wf,
                            var_values=var_values,
                            score=100.0,
                            match_type="orchestrator",
                        )

            replay_allowed = False
            if workflow_match and route.get("action") == "workflow":
                replay_allowed, replay_reason = should_replay_workflow(
                    workflow_match, explicit=is_explicit,
                )
                if not replay_allowed:
                    wf = workflow_match.get("workflow") or {}
                    yield status(
                        f"Workflow '{wf.get('workflow_name', 'workflow da hoc')}' khop nhung bi bo qua: {replay_reason}.",
                        "workflow_skip",
                    )
                    run_agent_this_phase = True
                    workflow_replay_hint = (
                        workflow_replay_hint or
                        f"[Workflow '{wf.get('workflow_name', 'workflow')}' khong replay duoc: {replay_reason}]\n"
                        "Hay xu ly pha nay bang Agent."
                    )

            if workflow_match and replay_allowed:
                workflow = workflow_match.get("workflow") or {}
                wf_name = workflow.get("workflow_name") or "workflow da hoc"
                yield status(
                    f"Pha {orchestrator_phase_idx}: dùng workflow «{wf_name}» (không gọi AI)...",
                    "workflow_replay",
                    runner="workflow",
                )

                current_url = routing_url
                replay_plan = build_workflow_replay_plan(workflow_match, phase_sub_task, current_url)
                replay_actions = [step["action"] for step in replay_plan]
                replay_error = ""
                final_text = replay_final_text(workflow_match, replay_actions, phase_sub_task)
                executed_replay_steps = 0
                previous_step_filled_text = False
                local_repair_count = 0
                ai_interventions = 0
                max_ai_interventions = adaptive_recovery_budget(replay_plan)
                skip_until_idx = 0
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

                    # After an incident recovery resynced ahead, skip steps already satisfied.
                    if idx <= skip_until_idx:
                        continue

                    action = plan_step.get("action") or {}
                    action_name, params = action_name_and_params(action)
                    if not action_name:
                        continue
                    if action_name == "done":
                        final_text = replay_final_text(workflow_match, replay_actions, phase_sub_task)
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
                        "runner": "workflow",
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
                            final_text = replay_final_text(workflow_match, replay_actions, phase_sub_task)
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
                        if can_recover and ai_interventions < max_ai_interventions:
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
                                user_task=phase_sub_task,
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
                                await restore_workflow_page(workflow_match, session["browser_context"])
                                resync = await resync_replay_index(
                                    workflow_match, replay_plan, session["browser_context"], idx,
                                )
                                if resync.get("done"):
                                    terminal_verified = True
                                    final_text = replay_final_text(workflow_match, replay_actions, phase_sub_task)
                                    yield status(
                                        f"AI da khoi phuc va workflow da dat trang thai dich ({resync.get('note', '')}).",
                                        "workflow_resumed",
                                    )
                                    break
                                skip_until_idx = max(skip_until_idx, int(resync.get("resume_idx", idx + 1)) - 1)
                                yield status(
                                    f"AI da khoi phuc checkpoint. Quay lai workflow: {resync.get('note', '')}",
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
                            if can_recover and ai_interventions < max_ai_interventions:
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
                                    user_task=phase_sub_task,
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
                                    await restore_workflow_page(workflow_match, session["browser_context"])
                                    resync = await resync_replay_index(
                                        workflow_match, replay_plan, session["browser_context"], idx,
                                    )
                                    if resync.get("done"):
                                        terminal_verified = True
                                        final_text = replay_final_text(workflow_match, replay_actions, phase_sub_task)
                                        yield status(
                                            f"AI da khoi phuc va workflow da dat trang thai dich ({resync.get('note', '')}).",
                                            "workflow_resumed",
                                        )
                                        break
                                    skip_until_idx = max(skip_until_idx, int(resync.get("resume_idx", idx + 1)) - 1)
                                    yield status(
                                        f"AI da khoi phuc checkpoint. Quay lai workflow: {resync.get('note', '')}",
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
                    update_workflow_stats(
                        workflow_match.get("domain") or "",
                        workflow.get("workflow_id"),
                        success=True,
                        tokens=int((current_usage_summary().get("totals") or {}).get("total_tokens") or 0),
                        example=phase_sub_task,
                    )
                    persist_workflow_runtime_learning(
                        workflow_match.get("domain") or "",
                        workflow.get("workflow_id"),
                        trace=replay_trace,
                        success=True,
                    )
                    phase_record = {
                        "kind": "workflow",
                        "summary": final_text,
                        "workflow_id": workflow.get("workflow_id"),
                        "workflow_name": wf_name,
                        "sub_task": phase_sub_task,
                    }
                    orchestrator_phases.append(phase_record)
                    orchestration_meta["phases"].append({
                        "phase": orchestrator_phase_idx,
                        "action": "workflow",
                        "workflow_id": workflow.get("workflow_id"),
                        "workflow_name": wf_name,
                        "reason": route.get("reason"),
                    })
                    session["current_run"] = None
                    if orchestrator_should_finish_after_workflow(
                        req.task, phase_sub_task, orchestrator_phases, use_orchestrator_planner,
                    ):
                        usage = current_usage_summary()
                        session["last_usage"] = usage
                        metadata = {
                            "state": "done",
                            "usage": usage,
                            "orchestration": orchestration_meta,
                        }
                        await add_message_async(req.chat_id, "assistant", final_text, metadata)
                        yield sse({"type": "usage", "usage": usage})
                        yield sse({"type": "done", "text": final_text, "usage": usage})
                        return
                    yield status(
                        f"Pha {orchestrator_phase_idx}: xong workflow «{wf_name}». Chọn bước tiếp theo...",
                        "orchestrator_workflow_done",
                        runner="workflow",
                    )
                    continue

                session["current_run"] = None
                update_workflow_stats(
                    workflow_match.get("domain") or "",
                    workflow.get("workflow_id"),
                    success=False,
                    error=replay_error,
                    example=phase_sub_task,
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
                    f"Workflow replay gap loi: {replay_error[:160]}. Dang fallback sang Agent...",
                    "workflow_fallback",
                    runner="agent",
                )
                run_agent_this_phase = True

            if not run_agent_this_phase:
                continue

            agent_sub_task = phase_sub_task
            orchestration_meta["phases"].append({
                "phase": orchestrator_phase_idx,
                "action": "agent",
                "reason": route.get("reason"),
            })

            try:
                os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY
                yield status(
                    f"Pha {orchestrator_phase_idx}: AI xử lý «{agent_sub_task[:80]}»...",
                    "running",
                    runner="agent",
                )
    
                # FIX 8: Improved system prompt for long-running tasks.
                # The original prompt was defensive ("don't loop"). This version adds
                # explicit guidance for marathon tasks: checkpoint progress, use
                # extract_content proactively, and prefer explicit waits over spin-loops.
                extend_msg = (
                    "Luôn mở trang web cụ thể trước khi thao tác. "
                    "Nếu đang ở about:blank hoặc trang trống, hãy go_to_url hoặc search_google ngay. "
                    "Ưu tiên thao tác qua danh sách phần tử interactive (index) trong DOM; "
                    "khi có ảnh màn hình, dùng vision để xác nhận layout hoặc nút khó thấy trong DOM. "
                    "QUY TẮC CUỘN & KIỂM TRA: khi phải xem hết danh sách/dropdown/bảng dài, "
                    "BẮT BUỘC gọi `list_scrollable_regions` trước để biết vùng nào cuộn được. "
                    "Sau đó dùng `smart_scroll` với `index` (một mục TRONG danh sách) hoặc `target_text` (tên mục cần thấy). "
                    "Hệ thống thử scrollBy rồi wheel chuột tại vùng đó — chỉ báo «Đã cuộn» khi vị trí thật sự đổi. "
                    "Sau mỗi lần cuộn, đọc lại DOM/extract_content. "
                    "TUYỆT ĐỐI KHÔNG nói đã cuộn/đã kiểm tra hết nếu kết quả có ⚠️ hoặc «KHÔNG cuộn được». "
                    "Chỉ kết luận xong khi thấy mục cuối hoặc list_scrollable_regions báo hết biên (canDown=false). "
                    "Với tác vụ dài nhiều giờ: ưu tiên ghi nhớ trạng thái hiện tại qua extract_content "
                    "sau mỗi bước quan trọng, dùng wait thay vì lặp click khi trang đang tải, "
                    "và tóm tắt tiến độ trong next_goal để planner giữ hướng đúng. "
                    "Nếu bị lặp hành động hoặc chưa tìm ra mục tiêu sau vài lần thử, "
                    "hãy ưu tiên làm rõ tình trạng thay vì cố thử đi thử lại mù quáng."
                )
    
                phase_message_context = message_context
                if workflow_replay_hint:
                    phase_message_context = (workflow_replay_hint + "\n" + (phase_message_context or "")).strip()
                if orchestrator_phases:
                    done_note = build_orchestrator_completed_summary(orchestrator_phases)
                    phase_message_context = (
                        f"[Cac pha da xong:\n{done_note}]\n" + (phase_message_context or "")
                    ).strip()
                initial_actions_to_use = (
                    infer_initial_actions(agent_sub_task)
                    if fresh_browser_context and orchestrator_phase_idx == 1
                    else None
                )
    
                # Cost optimisation: route page content extraction to a cheap model when
                # the executor is an expensive Pro model. For already-cheap lite/flash
                # executors keep the default (executor model) to avoid any risk.
                extraction_llm = None
                if "pro" in executor_model.lower():
                    extraction_llm = make_llm(EXTRACTION_MODEL, executor_callback)
    
                agent = Agent(
                    task=agent_sub_task,
                    llm=make_llm(executor_model, executor_callback),
                    planner_llm=make_llm(planner_model, planner_callback) if use_planner else None,
                    planner_interval=resolve_planner_interval(req.task),
                    is_planner_reasoning=use_planner,
                    use_vision_for_planner=False,
                    extend_planner_system_message=PLANNER_GUIDANCE if use_planner else None,
                    page_extraction_llm=extraction_llm,
                    max_actions_per_step=MAX_ACTIONS_PER_STEP,
                    max_input_tokens=MAX_INPUT_TOKENS,
                    max_failures=MAX_FAILS_BEFORE_ASK + 1,
                    use_vision=initial_use_vision(vision_mode),
                    enable_memory=False,
                    browser_context=session.get("browser_context"),
                    message_context=phase_message_context or None,
                    initial_actions=initial_actions_to_use,
                    register_new_step_callback=on_new_step,
                    extend_system_message=extend_msg,
                    controller=controller,
                )
                session["current_agent"] = agent
                session["current_run"]   = task_state
    
                async def run_agent():
                    global browser_instance, current_browser_profile
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
                            browser_was_closed = is_browser_closed_error(e)
                            error_text = (
                                "Trình duyệt đã bị đóng hoặc mất kết nối. Server đã reset trạng thái, hãy gửi prompt lại để mở phiên mới."
                                if browser_was_closed
                                else browser_profile_error_message(e, browser_profile_config)
                            )
                            if browser_was_closed or error_text != str(e):
                                try:
                                    browser_context = session.get("browser_context")
                                    if browser_context:
                                        await browser_context.close()
                                except Exception:
                                    pass
                                session["browser_context"] = None
                                try:
                                    if browser_instance:
                                        await browser_instance.close()
                                except Exception:
                                    pass
                                browser_instance = None
                                current_browser_profile = None
                            await event_queue.put({
                                "type":  "error",
                                "text":  error_text,
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
                orchestrator_continue = False

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

                    if event["type"] == "done":
                        final_text = event["text"]
                        agent_task_joined = False
                        repair_result = None
                        cont_route: dict[str, Any] = {"action": "done"}

                        if workflow_replay_hint and workflow_match:
                            await agent_task
                            agent_task_joined = True
                            usage_totals = ((event.get("usage") or {}).get("totals") or {})
                            improved_override = None
                            try:
                                improved_override = await llm_generalize_workflow(
                                    planner_model,
                                    req.task,
                                    session.get("last_history") or [],
                                )
                            except Exception as exc:
                                print(f"[auto_repair] planner generalization skipped: {exc}")
                            repair_result = auto_repair_workflow_from_history(
                                workflow_match,
                                original_task=req.task,
                                history_list=session.get("last_history") or [],
                                final_text=final_text,
                                tokens=int(usage_totals.get("total_tokens") or 0),
                                generalized_override=improved_override,
                            )

                        orchestrator_phases.append({
                            "kind": "agent",
                            "summary": final_text,
                            "sub_task": agent_sub_task,
                        })
                        if not agent_task_joined:
                            await agent_task
                        cont_url = ""
                        try:
                            page = await session["browser_context"].get_current_page()
                            cont_url = page.url or ""
                        except Exception:
                            cont_url = ""
                        if agent_signals_blocked(final_text):
                            cont_route = guard_orchestrator_route(
                                {"action": "agent", "sub_task": ""},
                                original_task=req.task,
                                completed_phases=orchestrator_phases,
                                latest_agent_text=final_text,
                            )
                        else:
                            cont_route = await resolve_orchestrator_route(
                                original_task=req.task,
                                completed_phases=orchestrator_phases,
                                user_profile=user_profile,
                                catalog=catalog,
                                current_url=cont_url,
                                context_text=session.get("context") or "",
                                planner_model=planner_model,
                                phase_index=orchestrator_phase_idx + 1,
                                planner_callback=planner_callback,
                                use_orchestrator_planner=use_orchestrator_planner,
                            )
                            cont_route = guard_orchestrator_route(
                                cont_route,
                                original_task=req.task,
                                completed_phases=orchestrator_phases,
                                latest_agent_text=final_text,
                            )
                        if cont_route.get("action") != "done":
                            orchestrator_continue = True
                            phase_usage = event.get("usage")
                            if phase_usage:
                                yield sse({"type": "usage", "usage": phase_usage})
                            yield sse({
                                "type": "phase_done",
                                "text": final_text,
                                "phase": orchestrator_phase_idx,
                                "usage": phase_usage,
                            })
                            yield status(
                                f"Pha {orchestrator_phase_idx} xong. Chuyển sang bước tiếp theo...",
                                "orchestrator_agent_done",
                                runner="agent",
                            )
                            workflow_replay_hint = ""
                            workflow_match = None
                            workflow_runtime_metadata = None
                            task_state["current_step"] = 0
                            fresh_browser_context = False
                            session["last_usage"] = phase_usage
                            break

                        pieces = [str(p.get("summary") or "") for p in orchestrator_phases if p.get("summary")]
                        final_text = (
                            str(cont_route.get("sub_task") or "").strip()
                            or "\n".join(pieces)
                            or final_text
                        )
                        phase_usage = event.get("usage")
                        if phase_usage:
                            yield sse({"type": "usage", "usage": phase_usage})
                        yield sse({"type": "done", "text": final_text, "usage": phase_usage})

                        metadata = {
                            "state": "done",
                            "usage": phase_usage,
                            "orchestration": orchestration_meta,
                        }
                        if workflow_runtime_metadata:
                            metadata["workflow_runtime"] = workflow_runtime_metadata
                        if repair_result:
                            metadata["workflow_auto_repair"] = repair_result
                        await add_message_async(req.chat_id, "assistant", final_text, metadata)

                        refreshed_history = await load_history_async()
                        refreshed_messages = refreshed_history.get(req.chat_id, {}).get("messages", [])
                        session["context"] = build_message_context(refreshed_messages, "")
                        session["last_usage"] = phase_usage
                        stop_requests.discard(req.chat_id)
                        session["stop_requested"] = False
                        session["last_progress"] = None
                        return

                    yield sse(event)
                    if event["type"] not in {"error", "needs_input", "stopped"}:
                        continue

                    final_text = event["text"]
                    metadata = {
                        "state": event["type"],
                        "usage": event.get("usage"),
                        "orchestration": orchestration_meta,
                    }
                    if workflow_runtime_metadata:
                        metadata["workflow_runtime"] = workflow_runtime_metadata
                    await add_message_async(req.chat_id, "assistant", final_text, metadata)

                    refreshed_history = await load_history_async()
                    refreshed_messages = refreshed_history.get(req.chat_id, {}).get("messages", [])
                    session["context"] = build_message_context(refreshed_messages, "")
                    session["last_usage"] = event.get("usage")
                    stop_requests.discard(req.chat_id)
                    session["stop_requested"] = False
                    session["last_progress"] = None
                    await agent_task
                    return

                if orchestrator_continue:
                    continue

            except Exception as e:
                err = f"Loi: {str(e)}"
                await add_message_async(req.chat_id, "assistant", err, {"state": "error"})
                yield sse({"type": "error", "text": err})
                return

        # Orchestrator exhausted phases without explicit done
        pieces = [str(p.get("summary") or "") for p in orchestrator_phases if p.get("summary")]
        final_text = "\n".join(pieces) if pieces else "Da xu ly cong viec."
        usage = current_usage_summary()
        metadata = {"state": "done", "usage": usage, "orchestration": orchestration_meta}
        await add_message_async(req.chat_id, "assistant", final_text, metadata)
        session["current_run"] = None
        yield sse({"type": "usage", "usage": usage})
        yield sse({"type": "done", "text": final_text, "usage": usage})

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/reset_browser")
async def reset_browser():
    """Đóng và reset browser instance."""
    global browser_instance, current_browser_profile

    if browser_instance:
        try:
            await browser_instance.close()
        except Exception:
            pass
        browser_instance = None
    current_browser_profile = None

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
