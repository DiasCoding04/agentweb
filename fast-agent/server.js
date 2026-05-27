const http = require("http");
const fs = require("fs");
const path = require("path");
const { randomUUID } = require("crypto");
const { execFile } = require("child_process");
const {
  ACQUISITION_STATES,
  WRITE_ACTIONS,
  actionFromTool,
  assertContextStillValid,
  audit,
  classifyTargetContext,
  createTargetCandidate,
  createOrUpdateContextLock,
  createTaskState,
  ensureTargetLocked,
  enrichCandidate,
  finalStatusFromVerification,
  injectDefaultExpectedResult,
  inferTaskIntent,
  requiresLockedContext,
  retryClassFromResult,
  setAcquisitionState,
  summarizeObservationJson,
  validateAction
} = require("./runtime-core");

const PORT = Number(process.env.FAST_AGENT_PORT || 18792);
const HOST = "127.0.0.1";
const STATE_DIR = process.env.OPENCLAW_STATE_DIR || path.join(process.env.USERPROFILE || "", ".openclaw");
const AUTH_FILE = path.join(STATE_DIR, "agents", "main", "agent", "auth-profiles.json");
const BROWSER_DOM = process.env.BROWSER_DOM_CMD || path.join(STATE_DIR, "workspace", "browser-dom.cmd");
const BROWSER_DOM_SCRIPT = process.env.BROWSER_DOM_SCRIPT || path.join(__dirname, "browser-dom.mjs");
const BROWSER_PLAYWRIGHT_SCRIPT = process.env.BROWSER_PLAYWRIGHT_SCRIPT || path.join(__dirname, "browser-playwright.mjs");
const BROWSER_DRIVER = String(process.env.BROWSER_DRIVER || "cdp").toLowerCase() === "playwright" ? "playwright" : "cdp";
const BROWSER_DRIVER_SCRIPT = BROWSER_DRIVER === "playwright" ? BROWSER_PLAYWRIGHT_SCRIPT : BROWSER_DOM_SCRIPT;
const DEFAULT_MODEL = process.env.FAST_AGENT_MODEL || "gemini-3.5-flash";
const configuredMaxSteps = Number(process.env.FAST_AGENT_MAX_STEPS || 0);
const MAX_STEPS = configuredMaxSteps > 0 ? configuredMaxSteps : Infinity;
const HISTORY_ITEMS = Number(process.env.FAST_AGENT_HISTORY_ITEMS || 8);
const HISTORY_CHARS = Number(process.env.FAST_AGENT_HISTORY_CHARS || 4000);
const TOOL_RESULT_CHARS = Number(process.env.FAST_AGENT_TOOL_RESULT_CHARS || 1600);
const TRACE_CHARS = Number(process.env.FAST_AGENT_TRACE_CHARS || 700);
const OBSERVE_HTML_CHARS = Number(process.env.FAST_AGENT_OBSERVE_HTML_CHARS || 12000);
const MODELS = [
  { id: "gemini-3.5-flash", label: "Gemini 3.5 Flash", note: "nhanh, mặc định hiện tại" },
  { id: "gemini-3.1-flash-lite", label: "Gemini 3.1 Flash-Lite", note: "nhanh/rẻ, thao tác trình duyệt nhẹ" },
  { id: "gemini-2.5-flash-lite", label: "Gemini 2.5 Flash-Lite", note: "rẻ hơn, hợp thao tác rõ" },
  { id: "gemini-2.5-flash", label: "Gemini 2.5 Flash", note: "cân bằng" },
  { id: "gemini-3.1-pro-preview", label: "Gemini 3.1 Pro Preview", note: "suy luận khó, đắt hơn" }
];

const allowedCommands = new Set([
  "start", "goto", "new-tab", "tabs", "switch", "close-tab",
  "read", "snapshot", "accessibility", "elements", "links", "buttons", "inputs", "forms", "tables", "state", "evaluate",
  "find", "click", "click-index", "type", "set", "clear", "act",
  "key", "enter", "tab-key", "escape", "scroll", "scroll-top", "scroll-bottom",
  "scroll-to-text", "wait-for-text", "wait-for-selector", "back", "forward",
  "reload", "url", "html", "upload", "verify"
]);

// Tool categories and helpers to avoid scattered if/else checks.
const INPUT_TOOLS = new Set(["type", "set", "clear"]);
const TYPING_TOOLS = new Set(["type", "set"]);
const ACTION_TOOLS = new Set(["type", "set", "clear", "click", "act"]);
const STRUCTURE_TOOLS = new Set(["tables", "elements", "find", "inputs", "forms"]);
const GENERIC_INPUT_SELECTORS = new Set([
  "input",
  "textarea",
  "[contenteditable='true']",
  '[contenteditable="true"]',
  "div[contenteditable='true']",
  'div[contenteditable="true"]',
  "[role='textbox']",
  '[role="textbox"]',
  "div[role='textbox']",
  'div[role="textbox"]'
]);
const GENERIC_CLICK_SELECTORS = new Set([
  "button",
  "a",
  "[role='button']",
  '[role="button"]',
  "[contenteditable='true']",
  '[contenteditable="true"]',
  "div[contenteditable='true']",
  'div[contenteditable="true"]',
  "[role='textbox']",
  '[role="textbox"]',
  "div[role='textbox']",
  'div[role="textbox"]'
]);
const COMMAND_ALIASES = new Map([
  ["go", "goto"],
  ["navigate", "goto"],
  ["open", "goto"],
  ["press", "key"],
  ["submit", "enter"]
]);

function isInputTool(cmd) { return INPUT_TOOLS.has(String(cmd)); }
function isTypingTool(cmd) { return TYPING_TOOLS.has(String(cmd)); }
function isActionTool(cmd) { return ACTION_TOOLS.has(String(cmd)); }
function isStructureTool(cmd) { return STRUCTURE_TOOLS.has(String(cmd)); }
function normalizeCommand(cmd) { return COMMAND_ALIASES.get(String(cmd)) || String(cmd); }

const conversations = new Map();

function resolveSessionId(rawSessionId) {
  const clean = String(rawSessionId || "").trim();
  return clean || `anon-${randomUUID()}`;
}

function resetSession(sessionId) {
  const id = resolveSessionId(sessionId);
  const existed = conversations.delete(id);
  return { ok: true, sessionId: id, existed };
}

function readApiKey() {
  if (process.env.GEMINI_API_KEY) return process.env.GEMINI_API_KEY;
  const raw = fs.readFileSync(AUTH_FILE, "utf8");
  const json = JSON.parse(raw);
  const key = json?.profiles?.["google:default"]?.key;
  if (!key) throw new Error("Không tìm thấy Gemini API key trong OpenClaw auth.");
  return key;
}

function sendJson(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(data)
  });
  res.end(data);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", chunk => {
      data += chunk;
      if (data.length > 200_000) {
        reject(new Error("Request quá lớn."));
        req.destroy();
      }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

function runBrowserDom(cmd, args = []) {
  return new Promise((resolve) => {
    cmd = normalizeCommand(cmd);
    if (!allowedCommands.has(cmd)) {
      resolve({ ok: false, output: `Lệnh browser không được phép: ${cmd}` });
      return;
    }
    const safeArgs = [cmd, ...args.map(String)];
    const started = Date.now();
    execFile(process.execPath, [BROWSER_DRIVER_SCRIPT, ...safeArgs], {
      windowsHide: true,
      timeout: 20_000,
      maxBuffer: 1_500_000
    }, (error, stdout, stderr) => {
      const output = `${stdout || ""}${stderr ? `\nERR:\n${stderr}` : ""}`.trim();
      resolve({
        ok: !error,
        ms: Date.now() - started,
        output: output.slice(0, 60_000),
        error: error ? String(error.message || error) : null
      });
    });
  });
}

async function runBrowserCommand(cmd, args = []) {
  const result = await runBrowserDom(cmd, args);
  const json = toolJson(result);
  return { result, json, ok: toolSucceeded(result) };
}

function compactHistory(items) {
  return items.slice(-HISTORY_ITEMS).map(item => ({
    role: item.role,
    parts: [{ text: item.text.slice(0, HISTORY_CHARS) }]
  }));
}

function parseJsonSafe(text) {
  try {
    return JSON.parse(String(text || ""));
  } catch {
    return null;
  }
}

function toolJson(result) {
  return parseJsonSafe(String(result?.output || "").trim());
}

function toolSucceeded(result) {
  if (!result?.ok) return false;
  const json = toolJson(result);
  return !(json && json.ok === false);
}

function summarizeToolOutput(output) {
  const text = String(output || "")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "(không có nội dung trả về)";
  return text.slice(0, TRACE_CHARS);
}

function compactToolResult(cmd, result) {
  const raw = String(result?.output || result?.error || "").trim();
  const json = parseJsonSafe(raw);
  if (!json) return summarizeToolOutput(raw).slice(0, TOOL_RESULT_CHARS);

  if (cmd === "read") {
    return JSON.stringify({
      title: json.title || "",
      url: json.url || "",
      text: String(json.text || "").replace(/\s+/g, " ").slice(0, TOOL_RESULT_CHARS)
    });
  }
  if (cmd === "html") {
    const text = String(json || raw || "")
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
      .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ")
      .replace(/\s+/g, " ")
      .trim();
    return text.slice(0, TOOL_RESULT_CHARS);
  }
  if (cmd === "snapshot") {
    return JSON.stringify({
      title: json.title || "",
      url: json.url || "",
      scroll: json.scroll,
      text: String(json.text || "").replace(/\s+/g, " ").slice(0, 1200),
      elements: Array.isArray(json.elements) ? json.elements.slice(0, 160).map(e => ({
        pid: e.pid,
        tag: e.tag,
        type: e.type,
        role: e.role,
        text: String(e.text || "").slice(0, 180),
        selector: String(e.selector || "").slice(0, 180),
        x: e.x,
        y: e.y,
        w: e.w,
        h: e.h,
        disabled: e.disabled
      })) : []
    }).slice(0, TOOL_RESULT_CHARS * 2);
  }
  if (cmd === "accessibility") {
    return JSON.stringify({
      nodes: Array.isArray(json.nodes) ? json.nodes.slice(0, 120).map(n => ({
        index: n.index,
        role: n.role,
        name: String(n.name || "").slice(0, 160),
        value: String(n.value || "").slice(0, 120),
        description: String(n.description || "").slice(0, 120),
        props: n.props || {}
      })) : []
    }).slice(0, TOOL_RESULT_CHARS * 2);
  }
  if (cmd === "tabs" && Array.isArray(json)) {
    return JSON.stringify(json.slice(0, 12).map(t => ({
      index: t.index,
      title: String(t.title || "").slice(0, 80),
      url: String(t.url || "").slice(0, 160)
    })));
  }
  if (["click", "click-index", "type", "set", "clear", "find", "act"].includes(cmd)) {
    return JSON.stringify({
      ok: json.ok,
      error: json.error || "",
      typed: json.typed ? String(json.typed).slice(0, 160) : undefined,
      value: json.value ? String(json.value).slice(0, 160) : undefined,
      target: json.target ? {
        tag: json.target.tag,
        role: json.target.role,
        text: String(json.target.text || "").slice(0, 220),
        selector: String(json.target.selector || "").slice(0, 220)
      } : undefined,
      clicked: json.clicked ? {
        tag: json.clicked.tag,
        role: json.clicked.role,
        text: String(json.clicked.text || "").slice(0, 220),
        selector: String(json.clicked.selector || "").slice(0, 220)
      } : undefined
    });
  }
  if (cmd === "verify") {
    return JSON.stringify(json).slice(0, TOOL_RESULT_CHARS);
  }
  return JSON.stringify(json).slice(0, TOOL_RESULT_CHARS);
}

function toolSignature(cmd, args) {
  return `${cmd} ${JSON.stringify(args || [])}`;
}

function getArg(args, name) {
  const index = Array.isArray(args) ? args.indexOf(name) : -1;
  return index >= 0 ? args[index + 1] : undefined;
}

function selectorDepth(selector) {
  return String(selector || "").split(">").length;
}

function isGenericSelector(selector, cmd) {
  const clean = String(selector || "").trim();
  if (!clean) return false;
  const normalized = clean.replace(/\s+/g, " ");
  if (isInputTool(cmd) && GENERIC_INPUT_SELECTORS.has(normalized)) return true;
  if (cmd === "click" && GENERIC_CLICK_SELECTORS.has(normalized)) return true;
  if (isTypingTool(cmd) && /^div\[contenteditable=(['"])true\1\]$/i.test(normalized)) return true;
  return false;
}

function isBrittleSelector(selector) {
  const clean = String(selector || "").trim();
  if (!clean) return false;
  return selectorDepth(clean) > 5 && /nth-of-type/i.test(clean) && !/(#|name=|aria-label=|placeholder=|data-testid=|role=)/i.test(clean);
}

function needsSaferTarget(cmd, args) {
  if (!isActionTool(cmd)) return "";
  if (cmd === "act") return "";
  const pid = getArg(args, "--pid");
  const selector = getArg(args, "--selector");
  const text = getArg(args, "--text");
  const index = getArg(args, "--index");
  if (pid !== undefined) return "";
  if (index !== undefined) return "";
  if (selector && isGenericSelector(selector, cmd)) return `selector quá rộng: ${selector}`;
  if (selector && isTypingTool(cmd) && isBrittleSelector(selector)) return `selector quá mong manh, dựa nhiều vào nth-of-type: ${selector}`;
  if (!selector && !text && isActionTool(cmd)) return "thiếu --selector, --text hoặc --index để xác định phần tử";
  return "";
}

function describeTargetingRule(cmd) {
  if (isTypingTool(cmd)) {
    return [
      "Chọn lại mục tiêu nhập bằng một trong các cách an toàn:",
      "1) dùng --index lấy trực tiếp từ snapshot/elements nếu element đúng đã xuất hiện;",
      "2) dùng selector có aria-label, placeholder, name, data-testid, role hoặc id;",
      "3) chọn phần tử có vai trò và vị trí phù hợp với nhiệm vụ hiện tại, không chọn trường nhập chỉ vì nó có thể gõ được.",
      "Không dùng selector chung như div[contenteditable='true'] hoặc [role='textbox'] vì dễ gõ nhầm."
    ].join(" ");
  }
  return "Chọn lại mục tiêu bằng --index từ snapshot/elements hoặc selector cụ thể hơn; tránh selector chung.";
}

function commandChangesState(cmd) {
  return new Set(["goto", "back", "forward", "reload", "switch", "new-tab", "close-tab", "click", "act", "find"]).has(String(cmd));
}

function shouldObserveBeforeTool(cmd) {
  return isActionTool(cmd);
}

function shouldObserveAfterTool(cmd) {
  return isActionTool(cmd) || ["goto", "back", "forward", "reload", "switch", "new-tab", "close-tab"].includes(String(cmd));
}

function observationPlanForAction(cmd, args = [], phase = "pre") {
  const normalizedCmd = normalizeCommand(cmd);
  const actType = normalizedCmd === "act"
    ? String(getArg(args, "--action") || "click").toLowerCase()
    : normalizedCmd;

  if (phase === "block") return [];
  if (phase === "not_found") return [["snapshot", ["--limit", "500"]], ["state", []], ["html", ["--max", String(OBSERVE_HTML_CHARS)]]];

  if (phase === "pre") {
    if (["goto", "back", "forward", "reload", "switch", "new-tab", "close-tab"].includes(normalizedCmd)) {
      return [["snapshot", ["--limit", "500"]], ["state", []]];
    }
    if (["type", "set", "clear"].includes(actType)) {
      return [["snapshot", ["--limit", "500"]], ["forms", []]];
    }
    if (["click", "press", "key"].includes(actType)) {
      return [["snapshot", ["--limit", "500"]], ["state", []]];
    }
    return [["snapshot", ["--limit", "500"]], ["state", []]];
  }

  if (phase === "verify") {
    if (["goto", "back", "forward", "reload", "switch", "new-tab", "close-tab"].includes(normalizedCmd)) {
      return [["snapshot", ["--limit", "500"]], ["state", []]];
    }
    if (["type", "set", "clear"].includes(actType)) {
      return [["forms", []]];
    }
    if (["click", "press", "key"].includes(actType)) {
      return [["url", []], ["state", []]];
    }
    return [["url", []], ["state", []]];
  }

  return [["url", []], ["snapshot", ["--limit", "500"]], ["state", []]];
}

function summarizeAcquisition(taskState) {
  const candidate = taskState.acquisition?.selectedCandidate || taskState.acquisition?.candidates?.[0] || null;
  return {
    originalTarget: taskState.invariants.targetEntity || "",
    exactFindResult: !!candidate,
    targetFound: !!candidate,
    targetActive: !!taskState.contextLock?.locked,
    candidates: candidate ? [candidate] : [],
    activeContextStatus: taskState.contextLock?.locked ? "TARGET_LOCKED" : taskState.acquisition?.state || ACQUISITION_STATES.INIT,
    lockStatus: taskState.contextLock?.locked ? "LOCKED" : "UNLOCKED",
    nextAllowedActionTypes: taskState.contextLock?.locked ? ["type", "set", "click_send", "verify"] : ["acquire_target", "open_target", "verify_context"],
    sideEffectsAllowed: !!taskState.contextLock?.locked
  };
}

function isAcquireAction(toolCmd, toolArgs, taskState) {
  if (String(toolCmd) !== "click" && String(toolCmd) !== "act") return false;
  const candidate = taskState.acquisition?.selectedCandidate || taskState.acquisition?.candidates?.[0];
  if (!candidate) return false;
  const argsText = JSON.stringify(toolArgs || []).toLowerCase();
  return (candidate.selector && argsText.includes(String(candidate.selector).toLowerCase()))
    || (candidate.matchedText && argsText.includes(String(candidate.matchedText).toLowerCase()))
    || (candidate.targetText && argsText.includes(String(candidate.targetText).toLowerCase()));
}

function autoCreateCandidateFromObservation(taskState, observation) {
  const target = taskState.invariants.targetEntity || "";
  if (!target) return null;
  const context = classifyTargetContext(observation, target);
  if (!context.targetFound) return null;
  const candidate = createTargetCandidate(target, {
    ok: true,
    text: target,
    selector: "",
    role: context.targetLikelyInList ? "link" : "region",
    rect: null
  });
  return candidate ? enrichCandidate(candidate, observation) : null;
}

function buildAcquireTargetArgs(candidate) {
  const selector = String(candidate?.selector || "");
  const role = String(candidate?.role || "").toLowerCase();
  const nonOpenable = role === "img" || /(^|[ >])img(\b|[.#\[])/i.test(selector) || /đã xem lúc|seen at/i.test(String(candidate?.matchedText || ""));
  if (!nonOpenable && selector) return ["--selector", selector];
  if (candidate?.targetText) return ["--role", "link", "--name", String(candidate.targetText)];
  if (candidate?.matchedText) return ["--text", String(candidate.matchedText)];
  if (candidate?.targetText) return ["--text", String(candidate.targetText)];
  return [];
}

function hasArg(args, name) {
  return Array.isArray(args) && args.includes(name);
}

function bindActionToLockedTarget(toolCmd, toolArgs, taskState) {
  const lockName = taskState.contextLock?.lockedTargetName || taskState.contextLock?.lockedMatchedName || "";
  if (!lockName || !Array.isArray(toolArgs)) return toolArgs;
  const cmd = String(toolCmd);
  const bound = [...toolArgs];
  const actType = cmd === "act" ? String(getArg(bound, "--action") || "click").toLowerCase() : cmd;
  const needsScopedTarget = ["type", "set", "clear", "click", "press", "key", "send", "submit"].includes(actType);
  if (needsScopedTarget && !hasArg(bound, "--near")) {
    bound.push("--near", lockName);
  }
  return bound;
}

function executionTargetMismatched(taskState, resultJson) {
  const lockName = String(taskState.contextLock?.lockedTargetName || taskState.contextLock?.lockedMatchedName || "").toLowerCase();
  if (!lockName) return false;
  const targetText = String(resultJson?.target?.text || resultJson?.clicked?.text || "").toLowerCase();
  if (!targetText) return false;
  if (targetText.includes(lockName)) return false;
  if (/viết cho|write to|to\s+[a-zà-ỹ]/i.test(targetText)) return true;
  return false;
}

async function attemptEnsureLocked(taskState, trace, history, baseObservation) {
  const targetText = taskState.invariants.targetEntity || "";
  if (targetText && !taskState.acquisition?.selectedCandidate?.selector) {
    const findResult = await runBrowserDom("find", ["--text", targetText]);
    const findJson = toolJson(findResult);
    trace.push({ type: "tool", cmd: "find", args: ["--text", targetText], ms: findResult.ms, ok: toolSucceeded(findResult), summary: `ensure_lock_find: ${summarizeToolOutput(compactToolResult("find", findResult))}` });
    if (findJson?.ok) {
      let candidate = createTargetCandidate(targetText, findJson);
      candidate = enrichCandidate(candidate, baseObservation || {});
      if (candidate) {
        taskState.acquisition.candidates = [candidate];
        taskState.acquisition.selectedCandidate = candidate;
        setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_CANDIDATE_FOUND);
      }
    }
    if (!taskState.acquisition?.selectedCandidate || /(^|[ >])img(\b|[.#\[])/i.test(String(taskState.acquisition?.selectedCandidate?.selector || ""))) {
      const semanticFind = await runBrowserDom("find", ["--role", "link", "--name", targetText]);
      const semanticJson = toolJson(semanticFind);
      trace.push({ type: "tool", cmd: "find", args: ["--role", "link", "--name", targetText], ms: semanticFind.ms, ok: toolSucceeded(semanticFind), summary: `ensure_lock_semantic_find: ${summarizeToolOutput(compactToolResult("find", semanticFind))}` });
      if (semanticJson?.ok) {
        let candidate = createTargetCandidate(targetText, semanticJson);
        candidate = enrichCandidate(candidate, baseObservation || {});
        if (candidate) {
          taskState.acquisition.candidates = [candidate];
          taskState.acquisition.selectedCandidate = candidate;
          setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_CANDIDATE_FOUND);
        }
      }
    }
  }
  let status = ensureTargetLocked(taskState, baseObservation);
  if (status.ok) return status;
  if (!["REQUIRES_ACQUIRE_TARGET", "UNKNOWN_TARGET_CONTEXT", "FAILED_CONTEXT_LOCK"].includes(status.status)) return status;
  const candidate = status.candidate || taskState.acquisition?.selectedCandidate;
  if (!candidate) return { ok: false, status: "FAILED_CONTEXT_LOCK", reason: "missing candidate for acquire action" };
  const acquireArgs = buildAcquireTargetArgs(candidate);
  if (!acquireArgs.length) return { ok: false, status: "FAILED_CONTEXT_LOCK", reason: "candidate has no actionable locator" };
  setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_OPENING, { attempts: (taskState.acquisition?.attempts || 0) + 1 });
  const acquireResult = await runBrowserDom("click", acquireArgs);
  const acquireOk = toolSucceeded(acquireResult);
  trace.push({ type: "tool", cmd: "click", args: acquireArgs, ms: acquireResult.ms, ok: acquireOk, summary: `acquire_target: ${summarizeToolOutput(compactToolResult("click", acquireResult))}` });
  if (!acquireOk) return { ok: false, status: "FAILED_CONTEXT_LOCK", reason: "acquire target click failed", raw: acquireResult.output || acquireResult.error };
  setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_CONTEXT_VERIFYING);
  const verifyObservation = await runObservation(trace, history, "ACQUIRE_VERIFY_OBSERVATION", { includeHtml: false });
  status = ensureTargetLocked(taskState, verifyObservation);
  if (status.ok) setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_LOCKED);
  return status;
}

async function runObservation(trace, history, label, opts = {}) {
  const includeHtml = !!opts.includeHtml;
  const observationCmds = Array.isArray(opts.commands) && opts.commands.length
    ? opts.commands.map(([cmd, args = []]) => [normalizeCommand(cmd), args])
    : [["url", []], ["snapshot", ["--limit", "500"]], ["state", []]];
  if (includeHtml && !observationCmds.some(([cmd]) => cmd === "html")) {
    observationCmds.push(["html", ["--max", String(OBSERVE_HTML_CHARS)]]);
  }

  const parts = [];
  for (const [cmd, args] of observationCmds) {
    try {
      const result = await runBrowserDom(cmd, args);
      const compact = compactToolResult(cmd, result);
      const json = toolJson(result);
      trace.push({
        type: "tool",
        cmd,
        args,
        ms: result.ms,
        ok: toolSucceeded(result),
        summary: `${label}: ${summarizeToolOutput(compact)}`
      });
      parts.push({ cmd, compact, json });
    } catch (error) {
      trace.push({
        type: "tool",
        cmd,
        args,
        ms: 0,
        ok: false,
        summary: `${label} error: ${String(error.message || error)}`
      });
    }
  }

  if (parts.length) {
    const text = `${label} OBSERVATION\n${parts.map(p => `${p.cmd}: ${p.compact}`).join("\n")}`;
    history.push({ role: "user", text: text.slice(0, TOOL_RESULT_CHARS * 4) });
  }
  return summarizeObservationJson(parts);
}

async function runAndTrace(trace, cmd, args, label) {
  const { result, json, ok } = await runBrowserCommand(cmd, args);
  trace.push({
    type: "tool",
    cmd,
    args,
    ms: result.ms,
    ok,
    summary: `${label}: ${summarizeToolOutput(compactToolResult(cmd, result))}`
  });
  return { result, json, ok };
}

function youtubeUrl() {
  return "https://www.youtube.com";
}

function spotifyUrl() {
  return "https://open.spotify.com/search";
}

function googleSearchUrl(query) {
  return `https://www.google.com/search?q=${encodeURIComponent(String(query || ""))}`;
}

function normalizeNeedle(text) {
  return String(text || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

function findVisibleRank(items, target) {
  const needle = normalizeNeedle(target);
  if (!needle) return 0;
  const list = Array.isArray(items) ? items : [];
  for (let i = 0; i < list.length; i += 1) {
    const item = list[i] || {};
    const haystack = normalizeNeedle([item.text, item.href, item.selector].filter(Boolean).join(" "));
    if (haystack.includes(needle)) return i + 1;
  }
  return 0;
}

async function runDeterministicTask(taskState, history) {
  const parsed = taskState.invariants.parsedTask || {};
  const intent = parsed.intent || "";
  if (!["open_website", "youtube_search", "spotify_search", "google_search_rank", "send_message"].includes(intent)) return null;
  const trace = [];
  const contextLockRequired = requiresLockedContext(taskState);
  audit(taskState, {
    type: "deterministic_started",
    driver: BROWSER_DRIVER,
    rawRequest: taskState.invariants.originalRequest,
    parsed,
    firstAction:
      intent === "send_message" ? "goto_messenger" :
      parsed.app === "spotify" ? "goto_spotify" :
      parsed.app === "google" ? "goto_google" :
      "goto_youtube",
    contextLockRequired
  });
  if (intent === "open_website" && parsed.app === "youtube") {
    const first = await runAndTrace(trace, "goto", [youtubeUrl()], "DETERMINISTIC_OPEN_WEBSITE");
    const verification = await runAndTrace(trace, "verify", ["--url", "youtube.com"], "DETERMINISTIC_VERIFY");
    const success = first.ok && verification.json?.ok === true;
    taskState.finalStatus = success ? "SUCCESS" : "FAILED_VERIFICATION";
    audit(taskState, { type: "verification_result", driver: BROWSER_DRIVER, verification: verification.json, status: taskState.finalStatus });
    return {
      final: success ? "Đã mở YouTube và xác minh domain youtube.com." : "FAILED_VERIFICATION: không xác minh được YouTube sau khi mở.",
      trace,
      audit: taskState.audit,
      status: taskState.finalStatus,
      invariants: taskState.invariants
    };
  }
  if (intent === "open_website" && parsed.app === "spotify") {
    const first = await runAndTrace(trace, "goto", [spotifyUrl()], "DETERMINISTIC_OPEN_SPOTIFY");
    const verification = await runAndTrace(trace, "verify", ["--url", "spotify.com"], "DETERMINISTIC_VERIFY");
    const success = first.ok && verification.json?.ok === true;
    taskState.finalStatus = success ? "SUCCESS" : "FAILED_VERIFICATION";
    audit(taskState, { type: "verification_result", driver: BROWSER_DRIVER, verification: verification.json, status: taskState.finalStatus });
    return {
      final: success ? "Đã mở Spotify và xác minh domain spotify.com." : "FAILED_VERIFICATION: không xác minh được Spotify sau khi mở.",
      trace,
      audit: taskState.audit,
      status: taskState.finalStatus,
      invariants: taskState.invariants
    };
  }
  if (intent === "youtube_search" && parsed.app === "youtube") {
    const query = parsed.query || "";
    const first = await runAndTrace(trace, "goto", [youtubeUrl()], "DETERMINISTIC_YOUTUBE_GOTO");
    const filled = await runAndTrace(trace, "type", ["--selector", "input[name=\"search_query\"]", "--value", query, "--expect-value", query], "DETERMINISTIC_YOUTUBE_FILL");
    const pressed = filled.ok ? await runAndTrace(trace, "key", ["--name", "Enter"], "DETERMINISTIC_YOUTUBE_ENTER") : { ok: false, json: null };
    if (pressed.ok) await runAndTrace(trace, "wait-for-text", ["--text", query, "--timeout", "12000"], "DETERMINISTIC_YOUTUBE_WAIT");
    const verification = await runAndTrace(trace, "verify", ["--url", "search_query"], "DETERMINISTIC_VERIFY");
    const success = first.ok && filled.ok && pressed.ok && verification.json?.ok === true;
    taskState.finalStatus = success ? "SUCCESS" : "FAILED_VERIFICATION";
    audit(taskState, { type: "verification_result", driver: BROWSER_DRIVER, verification: verification.json, status: taskState.finalStatus });
    return {
      final: success ? `Đã tìm YouTube với truy vấn: ${query}` : "FAILED_VERIFICATION: không xác minh được trang kết quả YouTube.",
      trace,
      audit: taskState.audit,
      status: taskState.finalStatus,
      invariants: taskState.invariants
    };
  }
  if (intent === "spotify_search" && parsed.app === "spotify") {
    const query = parsed.query || "";
    const first = await runAndTrace(trace, "goto", [spotifyUrl()], "DETERMINISTIC_SPOTIFY_GOTO");
    const filled = await runAndTrace(trace, "type", ["--selector", "input[type=\"search\"]", "--value", query, "--expect-value", query], "DETERMINISTIC_SPOTIFY_FILL");
    const pressed = filled.ok ? await runAndTrace(trace, "key", ["--name", "Enter"], "DETERMINISTIC_SPOTIFY_ENTER") : { ok: false, json: null };
    const verification = await runAndTrace(trace, "verify", ["--url", "spotify.com/search"], "DETERMINISTIC_VERIFY");
    const success = first.ok && filled.ok && pressed.ok && verification.json?.ok === true;
    taskState.finalStatus = success ? "SUCCESS" : "FAILED_VERIFICATION";
    audit(taskState, { type: "verification_result", driver: BROWSER_DRIVER, query, verification: verification.json, status: taskState.finalStatus });
    return {
      final: success ? `Đã mở Spotify Search với truy vấn: ${query}` : "FAILED_VERIFICATION: không xác minh được trang tìm kiếm Spotify.",
      trace,
      audit: taskState.audit,
      status: taskState.finalStatus,
      invariants: taskState.invariants
    };
  }
  if (intent === "google_search_rank" && parsed.app === "google") {
    const query = parsed.query || "";
    const target = parsed.searchTarget || parsed.target || "";
    const first = await runAndTrace(trace, "goto", [googleSearchUrl(query)], "DETERMINISTIC_GOOGLE_GOTO");
    const results = first.ok ? await runAndTrace(trace, "elements", ["--query", "a h3"], "DETERMINISTIC_GOOGLE_RESULTS") : { ok: false, json: [] };
    const fallbackResults = (!Array.isArray(results.json) || !results.json.length) && first.ok
      ? await runAndTrace(trace, "elements", ["--query", "h3"], "DETERMINISTIC_GOOGLE_RESULTS_FALLBACK")
      : null;
    const visibleItems = Array.isArray(results.json) && results.json.length ? results.json : Array.isArray(fallbackResults?.json) ? fallbackResults.json : [];
    const rank = findVisibleRank(visibleItems, target);
    const verification = await runAndTrace(trace, "verify", ["--url", "google.com/search"], "DETERMINISTIC_VERIFY");
    const success = first.ok && verification.json?.ok === true;
    taskState.finalStatus = success ? "SUCCESS" : "FAILED_VERIFICATION";
    audit(taskState, { type: "verification_result", driver: BROWSER_DRIVER, query, target, rank, visibleResults: visibleItems.slice(0, 10), verification: verification.json, status: taskState.finalStatus });
    return {
      final: success
        ? (rank
          ? `Đã tìm Google với truy vấn "${query}". "${target}" đang ở vị trí thứ ${rank} trong các kết quả nhìn thấy đầu tiên.`
          : `Đã tìm Google với truy vấn "${query}" nhưng chưa thấy "${target}" trong các kết quả nhìn thấy đầu tiên.`)
        : "FAILED_VERIFICATION: không xác minh được trang kết quả Google.",
      trace,
      audit: taskState.audit,
      status: taskState.finalStatus,
      invariants: taskState.invariants
    };
  }
  if (intent === "send_message" && parsed.app === "messenger") {
    const target = parsed.searchTarget || parsed.target || "";
    const payload = parsed.payload || "";
    const first = await runAndTrace(trace, "goto", ["https://www.messenger.com"], "DETERMINISTIC_MESSENGER_GOTO");
    const found = first.ok ? await runAndTrace(trace, "find", ["--text", target], "DETERMINISTIC_MESSENGER_FIND_TARGET") : { ok: false, json: null };
    const opened = found.ok ? await runAndTrace(trace, "click", ["--text", target], "DETERMINISTIC_MESSENGER_OPEN_CHAT") : { ok: false, json: null };
    const verifyContext = opened.ok ? await runObservation(trace, history, "DETERMINISTIC_MESSENGER_CONTEXT", { includeHtml: false }) : {};
    if (opened.ok) createOrUpdateContextLock(taskState, verifyContext);
    const typed = opened.ok ? await runAndTrace(trace, "type", ["--role", "textbox", "--value", payload, "--expect-value", payload], "DETERMINISTIC_MESSENGER_TYPE") : { ok: false, json: null };
    const sent = typed.ok ? await runAndTrace(trace, "key", ["--name", "Enter"], "DETERMINISTIC_MESSENGER_SEND") : { ok: false, json: null };
    const verification = sent.ok ? await runAndTrace(trace, "verify", ["--text", payload], "DETERMINISTIC_VERIFY") : { ok: false, json: null };
    const postObservation = sent.ok ? await runObservation(trace, history, "DETERMINISTIC_MESSENGER_POST", { includeHtml: false }) : {};
    const contextCheck = assertContextStillValid(taskState, postObservation);
    const success = first.ok && found.ok && opened.ok && typed.ok && sent.ok && verification.json?.ok === true && contextCheck.ok;
    taskState.finalStatus = success ? "SUCCESS" : "FAILED_VERIFICATION";
    audit(taskState, { type: "verification_result", driver: BROWSER_DRIVER, target, payload, verification: verification.json, contextCheck, status: taskState.finalStatus });
    return {
      final: success ? `Đã gửi tin nhắn cho ${target} và xác minh payload.` : "FAILED_VERIFICATION: thao tác Messenger đã chạy nhưng verifier không xác nhận đủ target/payload.",
      trace,
      audit: taskState.audit,
      status: taskState.finalStatus,
      invariants: taskState.invariants
    };
  }
  return null;
}

function tryParseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function extractBalancedJson(text) {
  let depth = 0;
  let start = -1;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "{") {
      if (depth === 0) start = i;
      depth += 1;
    } else if (ch === "}") {
      depth -= 1;
      if (depth === 0 && start >= 0) {
        return text.slice(start, i + 1);
      }
    }
  }
  return null;
}

function extractJson(text) {
  const clean = text.trim().replace(/^```(?:json)?/i, "").replace(/```$/i, "").trim();
  const parsed = tryParseJson(clean);
  if (parsed !== null) return parsed;

  const balanced = extractBalancedJson(clean);
  if (balanced) {
    const repaired = tryParseJson(balanced);
    if (repaired !== null) return repaired;
  }

  const start = clean.indexOf("{");
  const end = clean.lastIndexOf("}");
  if (start >= 0 && end > start) {
    const sliced = clean.slice(start, end + 1);
    const repaired = tryParseJson(sliced);
    if (repaired !== null) return repaired;
  }

  throw new Error(`Model không trả JSON hợp lệ: ${clean.slice(0, 300)}`);
}

function normalizeModel(modelId) {
  const requested = String(modelId || DEFAULT_MODEL).trim();
  return MODELS.some(m => m.id === requested) ? requested : DEFAULT_MODEL;
}

function normalizeMessageText(text) {
  return String(text || "").toLowerCase().replace(/\s+/g, " ").trim();
}

function tokenizeMessage(text) {
  return new Set(
    normalizeMessageText(text)
      .replace(/[^\p{L}\p{N}\s]/gu, " ")
      .split(" ")
      .map(token => token.trim())
      .filter(token => token.length >= 2)
  );
}

function looksLikeFollowUp(message) {
  const text = normalizeMessageText(message);
  return /^(tiếp|tiếp tục|làm tiếp|thử lại|again|retry|sửa|đổi|thêm|giảm|tăng|ngắn hơn|dài hơn|khác đi|nhắn|gửi|enter|icon)/i.test(text);
}

function shouldResetHistoryForNewTask(history, message) {
  if (!Array.isArray(history) || history.length === 0) return false;
  if (looksLikeFollowUp(message)) return false;

  const previousUser = [...history].reverse().find(item => item?.role === "user" && item?.text);
  if (!previousUser) return false;

  const currentTokens = tokenizeMessage(message);
  const previousTokens = tokenizeMessage(previousUser.text);
  if (!currentTokens.size || !previousTokens.size) return false;

  let overlap = 0;
  for (const token of currentTokens) {
    if (previousTokens.has(token)) overlap += 1;
  }
  const overlapRatio = overlap / Math.max(1, Math.min(currentTokens.size, previousTokens.size));
  return overlapRatio < 0.2;
}

async function callGemini(apiKey, history, userText) {
  const system = `Bạn là Fast Browser Agent chạy local trên Windows.
Mục tiêu: quan sát cấu trúc DOM và hành động chính xác trên trình duyệt (không dùng vision). Luôn giao tiếp bằng tiếng Việt.

Luôn TRẢ LẠI CHỈ JSON theo một trong hai schema sau:
- Tool call: {"tool": {"cmd": "<cmd>", "args": [ ... ] }, "note":"ngắn gọn tiếng Việt"}
- Final report: {"final": "báo cáo ngắn tiếng Việt"}

QUY TẮC KHẢO SÁT VÀ CHỌN MỤC TIÊU (BẮT BUỘC):
1) Hệ thống sẽ tự động cung cấp OBSERVATION (gồm URL, snapshot, accessibility, v.v.). Bạn PHẢI dựa vào OBSERVATION mới nhất để chọn mục tiêu.
2) LUÔN LUÔN ưu tiên nhắm mục tiêu bằng pid nếu phần tử đó có pid (ví dụ: {"cmd":"click","args":["--pid","15"]}). Đừng mò mẫm bằng text hoặc css selector nếu đã có pid.
3) Bạn CÓ THỂ (và được khuyến khích) xuất ra nhiều lệnh liên tiếp trong mảng "tools" để thực hiện một chuỗi hành động nhanh chóng. Hệ thống sẽ tự động chạy tuần tự và báo cáo lại nếu có lệnh nào hụt. Đừng ngại gộp các lệnh như [nhập chữ] -> [enter] -> [nhập chữ] thành một mảng lệnh.

XỬ LÝ LỖI VÀ FALLBACK:
- Nếu một thao tác báo lỗi "element not found", hãy yêu cầu 'snapshot' hoặc 'elements' để lấy pid của giao diện hiện tại thay vì thử đoán bừa selector.
- Không có giới hạn bước cố định; hãy tiếp tục hành động khi còn chiến lược. Nếu bế tắc, trả 'final' để hỏi người dùng.

SỔ TAY QUY TRÌNH ĐẶC BIỆT (PLAYBOOKS):
- Nếu tác vụ yêu cầu Mở nhạc/Video YouTube (VD: "mở bài Vinh Khuất", "bật nhạc Sơn Tùng"): 
  (1) Không bao giờ dùng Tab+Enter mù quáng. 
  (2) Dùng lệnh 'goto' để tìm kiếm: '{"cmd":"goto","args":["https://www.youtube.com/results?search_query=vinh+khuat"]}'
  (3) Dùng 'evaluate' để lấy link video đầu tiên: '{"cmd":"evaluate","args":["--expr","Array.from(document.querySelectorAll(\'ytd-video-renderer a#video-title\')).slice(0,3).map(a => ({title: a.textContent.trim(), href: a.href}))"]}'
  (4) Dùng 'goto' để mở link video vừa lấy được.
- Nếu URL chứa "ads.salontukawa.com" và bạn cần đếm chiến dịch hoặc lấy thông tin: (1) Nhìn cột bên trái, tìm 'pid' của Tài Khoản mục tiêu và gọi lệnh click vào pid đó. (2) Chờ trang tải bảng điều khiển bên phải. (3) Dùng lệnh 'tables' hoặc đếm thủ công các chiến dịch đang hoạt động. (4) Trả kết quả 'final'.

VÍ DỤ HỢP LỆ (chỉ JSON):
1) Batch Lệnh (Khuyên dùng): {"tools": [{"cmd":"click","args":["--pid","10"]},{"cmd":"type","args":["--pid","12","--value","abc"]},{"cmd":"key","args":["--name","Enter"]}]}
2) Discovery: {"tools": [{"cmd":"elements","args":[]}]}
3) Goto: {"tools": [{"cmd":"goto","args":["https://example.com"]}]}
4) Final Báo cáo: {"final":"Đã xử lý xong tác vụ."}`;

  const systemWithRules = `${system}

TOOL SAVINGS RULES:
- After SAFETY_BLOCK, do not request any extra observation tools. Change strategy immediately.
- If elements returns fewer than 5 items, immediately try elements with --query instead of calling plain elements again.
- Do not call the same tool twice in a row with the same params.
- For YouTube link discovery, use elements --query "ytd-video-renderer a#video-title" instead of evaluate.
- For login or form filling, prefer pid-based batch actions such as type --pid 1 --value ..., type --pid 2 --value ..., click --pid 3.`;

  const contents = compactHistory(history);
  contents.push({ role: "user", parts: [{ text: userText }] });

  const body = {
    systemInstruction: { parts: [{ text: systemWithRules }] },
    contents,
    generationConfig: {
      temperature: 0.2,
      maxOutputTokens: 4000,
      thinkingConfig: { thinkingBudget: 0 }
    }
  };

  const modelId = history.modelId || DEFAULT_MODEL;
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${modelId}:generateContent?key=${encodeURIComponent(apiKey)}`;
  const started = Date.now();
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body)
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`Gemini HTTP ${response.status}: ${text.slice(0, 500)}`);
  const json = JSON.parse(text);
  const out = json?.candidates?.[0]?.content?.parts?.map(p => p.text || "").join("").trim();
  if (!out) throw new Error("Gemini trả rỗng.");
  return { ms: Date.now() - started, raw: out, parsed: extractJson(out) };
}

async function agentTurn(sessionId, message, modelId) {
  let history = conversations.get(sessionId) || [];
  if (shouldResetHistoryForNewTask(history, message)) {
    history = [];
  }
  history.modelId = normalizeModel(modelId);
  history.push({ role: "user", text: message });
  const taskState = createTaskState(message);
  audit(taskState, {
    type: "task_started",
    driver: BROWSER_DRIVER,
    rawRequest: taskState.invariants.originalRequest,
    parsed: taskState.invariants.parsedTask,
    contextLockRequired: requiresLockedContext(taskState),
    invariants: taskState.invariants
  });
  const deterministic = await runDeterministicTask(taskState, history);
  if (deterministic) return deterministic;
  const apiKey = readApiKey();

  const trace = [];
  const failedToolCounts = new Map();
  const toolCounts = new Map();
  const signatureStateVersion = new Map();
  let lastToolSignature = "";
  let nextInput = message;
  let final = "";

  for (let step = 0; step < MAX_STEPS; step += 1) {
    const model = await callGemini(apiKey, history, nextInput);
    trace.push({
      type: "model",
      ms: model.ms,
      note: model.parsed.note || (model.parsed.final ? "Hoàn tất báo cáo" : "Model quyết định bước tiếp theo"),
      raw: model.raw.slice(0, 1000)
    });

    if (model.parsed.final) {
      if (taskState.invariants.successCriteria.length && taskState.finalStatus === "RUNNING") { audit(taskState, { type: "final_unverified", reason: "missing verified success criteria", proposedFinal: String(model.parsed.final) }); }
        final = String(model.parsed.final);
        taskState.finalStatus = taskState.finalStatus === "RUNNING" ? "SUCCESS" : taskState.finalStatus;
        audit(taskState, { type: "final", status: taskState.finalStatus, final });
      history.push({ role: "model", text: final });
      break;
    }

    const tools = model.parsed.tools || (model.parsed.tool ? [model.parsed.tool] : []);
    if (!tools.length) throw new Error("JSON không có final hoặc tools.");

    history.push({ role: "model", text: JSON.stringify(model.parsed) });

    let nextInputs = [];
    let shouldBreakOuter = false;

    for (const tool of tools) {
      if (!tool || !tool.cmd) continue;



    const toolArgs = Array.isArray(tool.args) ? tool.args : [];
    const toolCmd = normalizeCommand(tool.cmd);
    const signature = toolSignature(toolCmd, toolArgs);
    const currentStateVersion = taskState.acquisition?.stateVersion || 0;
    const lastVersion = signatureStateVersion.get(signature);
    toolCounts.set(signature, (toolCounts.get(signature) || 0) + 1);
    if (signature === lastToolSignature) { nextInputs.push(`Do not call tool '${toolCmd}' twice in a row with the same params. Change strategy or use elements --query to narrow the target.`); continue; }
    if (lastVersion === currentStateVersion && (toolCounts.get(signature) || 0) > 1) { nextInputs.push('Cảnh báo: Bạn vừa lặp lại lệnh ' + toolCmd + ' mà không có tác dụng. Hãy thử cách khác.'); continue; }
    signatureStateVersion.set(signature, currentStateVersion);
    if ((failedToolCounts.get(signature) || 0) >= 2) {
      final = `Tôi dừng vì cùng một thao tác đã lỗi lặp lại nhiều lần: ${toolCmd}. Cần đổi cách làm thay vì tiếp tục thử mù.`;
      shouldBreakOuter = true; break;
    }

    // Browser state changes after nearly every action. Observe before each
    // action so the model targets the current page, not stale DOM.
    let preObservation = null;
    if (shouldObserveBeforeTool(toolCmd)) {
      preObservation = await runObservation(trace, history, "PRE_ACTION", {
        includeHtml: false,
        commands: observationPlanForAction(toolCmd, toolArgs, "pre")
      });
      if (requiresLockedContext(taskState) && !taskState.acquisition?.selectedCandidate) {
        const autoCandidate = autoCreateCandidateFromObservation(taskState, preObservation);
        if (autoCandidate) {
          taskState.acquisition.candidates = [autoCandidate];
          taskState.acquisition.selectedCandidate = autoCandidate;
          const context = classifyTargetContext(preObservation, taskState.invariants.targetEntity || "");
          setAcquisitionState(taskState, context.targetLikelyInList ? ACQUISITION_STATES.TARGET_CANDIDATE_FOUND : ACQUISITION_STATES.TARGET_CONTEXT_VERIFYING);
        }
      }
      if (requiresLockedContext(taskState)) createOrUpdateContextLock(taskState, preObservation);
      const contextCheck = assertContextStillValid(taskState, preObservation);
      audit(taskState, { type: "pre_action_observation", command: toolCmd, observation: preObservation, contextCheck });
      if (!contextCheck.ok) {
        taskState.finalStatus = contextCheck.status || "CONTEXT_CHANGED";
        final = `Dừng an toàn: context changed unexpectedly (${contextCheck.reason}).`;
        audit(taskState, { type: "blocked", status: taskState.finalStatus, reason: contextCheck.reason });
        shouldBreakOuter = true; break;
      }
    }

    const action = actionFromTool(toolCmd, toolArgs);
    injectDefaultExpectedResult(action, taskState);
    if (["goto", "back", "forward", "reload", "switch", "new-tab"].includes(toolCmd)) {
      setAcquisitionState(taskState, ACQUISITION_STATES.NAVIGATED);
    }
    if (toolCmd === "find") {
      setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_SEARCHING);
    }
    const acquireAction = isAcquireAction(toolCmd, toolArgs, taskState) && !taskState.contextLock?.locked;
    if (acquireAction) {
      action.sideEffect = false;
      action.riskLevel = "low";
      setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_OPENING, { attempts: (taskState.acquisition?.attempts || 0) + 1 });
    }
    const lockRequiredForTask = requiresLockedContext(taskState);
    const highRiskSideEffect = lockRequiredForTask && action.sideEffect && (WRITE_ACTIONS.has(action.type) || /send|submit|save|delete|publish|upload|press|key|click/i.test(action.type));
    if (highRiskSideEffect && !taskState.contextLock?.locked) {
      const lockStatus = await attemptEnsureLocked(taskState, trace, history, preObservation || {});
      audit(taskState, { type: "ensure_target_locked", requestedBy: action.type, lockStatus });
      if (!lockStatus.ok) {
        taskState.finalStatus = lockStatus.status === "UNKNOWN_TARGET_CONTEXT" ? "AMBIGUOUS_TARGET_CONTEXT" : "FAILED_CONTEXT_LOCK";
        final = `${taskState.finalStatus}: ${lockStatus.reason || "cannot lock target context"}. candidates=${JSON.stringify(taskState.acquisition?.candidates || [])}`;
        shouldBreakOuter = true; break;
      }
    }
    const validation = validateAction(action, taskState);
    audit(taskState, { type: "proposed_action", action, validation });
    if (!validation.ok) {
      if (!taskState.contextLock?.locked && (action.type === "type" || action.type === "set" || /send|submit|press|key/.test(action.type))) {
        const acq = summarizeAcquisition(taskState);
        history.push({ role: "user", text: `LOCK_REQUIRED_BEFORE_SIDE_EFFECT: ${JSON.stringify(acq)}` });
      }
      taskState.finalStatus = "BLOCKED_BY_SECURITY";
      final = `Dừng an toàn: action bị block vì ${validation.violations.join("; ")}.`;
      audit(taskState, { type: "blocked", status: taskState.finalStatus, violations: validation.violations });
      shouldBreakOuter = true; break;
    }

    const unsafeReason = needsSaferTarget(toolCmd, toolArgs);
    if (unsafeReason) {
      history.push({ role: "model", text: JSON.stringify(model.parsed) });
      history.push({ role: "user", text: `SAFETY_BLOCK ${toolCmd}: ${unsafeReason}.` });
      nextInput = `Tôi đã chặn thao tác '${toolCmd}' vì ${unsafeReason}. ${describeTargetingRule(toolCmd)} Hãy chọn lại bằng OBSERVATION mới nhất, hoặc trả final nếu không đủ chắc chắn.`;
      continue;
    }

    const effectiveArgs = taskState.contextLock?.locked ? bindActionToLockedTarget(toolCmd, toolArgs, taskState) : toolArgs;
    lastToolSignature = signature;
    let result = await runBrowserDom(toolCmd, effectiveArgs);
    let logicalOk = toolSucceeded(result);
    if (!logicalOk) failedToolCounts.set(signature, (failedToolCounts.get(signature) || 0) + 1);
    let compactResult = compactToolResult(toolCmd, result);
    let resultJson = toolJson(result);
    if (logicalOk && action.sideEffect && taskState.contextLock?.locked && executionTargetMismatched(taskState, resultJson)) {
      const relock = await attemptEnsureLocked(taskState, trace, history, preObservation || {});
      audit(taskState, { type: "execution_target_mismatch", detected: true, relock });
      if (relock.ok) {
        result = await runBrowserDom(toolCmd, bindActionToLockedTarget(toolCmd, toolArgs, taskState));
        logicalOk = toolSucceeded(result);
        compactResult = compactToolResult(toolCmd, result);
        resultJson = toolJson(result);
      } else {
        taskState.finalStatus = "FAILED_CONTEXT_LOCK";
        final = `FAILED_CONTEXT_LOCK: execution target drifted outside locked context (${relock.reason || "mismatch target"}).`;
        shouldBreakOuter = true; break;
      }
    }
    if (toolCmd === "find" && resultJson?.ok === true) {
      let candidate = createTargetCandidate(taskState.invariants.targetEntity || getArg(toolArgs, "--text") || "", resultJson);
      if (preObservation) candidate = enrichCandidate(candidate, preObservation);
      if (candidate) {
        taskState.acquisition.candidates = [candidate];
        taskState.acquisition.selectedCandidate = candidate;
        const context = classifyTargetContext(preObservation || {}, candidate.targetText || taskState.invariants.targetEntity || "");
        setAcquisitionState(taskState, context.targetActive ? ACQUISITION_STATES.TARGET_CONTEXT_VERIFYING : ACQUISITION_STATES.TARGET_CANDIDATE_FOUND);
        if (context.targetActive) createOrUpdateContextLock(taskState, preObservation || {});
      }
    } else if (toolCmd === "find" && resultJson?.ok === false) {
      
    }
    
    audit(taskState, {
      type: "execution_result",
      action,
      ok: logicalOk,
      result: resultJson || compactResult,
      retryClass: logicalOk ? "" : retryClassFromResult(result.output || result.error || compactResult)
    });
    trace.push({
      type: "tool",
      cmd: toolCmd,
      args: effectiveArgs,
      ms: result.ms,
      ok: logicalOk,
      summary: summarizeToolOutput(compactResult)
    });
    if (toolCmd === "find" && resultJson?.ok === true) {
      const acq = summarizeAcquisition(taskState);
      history.push({ role: "user", text: `TARGET_FOUND = true; candidate_count=${acq.candidates.length}; OBS_SUMMARY=${JSON.stringify(acq)}` });
      nextInput = `Tool find đã xác nhận TARGET_FOUND=true. Không được nói là chưa tìm thấy. Nếu target chưa active thì chỉ được open_target/acquire_target, chưa được type/send.`;
      continue;
    }

    if (logicalOk && shouldObserveAfterTool(toolCmd)) {
      if (acquireAction) setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_CONTEXT_VERIFYING);
      const verifyObservation = await runObservation(trace, history, "VERIFY_OBSERVATION", {
        includeHtml: false,
        commands: observationPlanForAction(toolCmd, toolArgs, "verify")
      });
      if (acquireAction) {
        const locked = createOrUpdateContextLock(taskState, verifyObservation);
        if (locked?.locked) setAcquisitionState(taskState, ACQUISITION_STATES.ACTION_READY);
      }
      const postContextCheck = assertContextStillValid(taskState, verifyObservation);
      const status = finalStatusFromVerification(action, resultJson, postContextCheck);
      audit(taskState, { type: "verification_result", action, contextCheck: postContextCheck, status, observation: verifyObservation, result: resultJson });
      if (!postContextCheck.ok) {
        taskState.finalStatus = postContextCheck.status || "CONTEXT_CHANGED";
        final = `Dừng an toàn: context changed unexpectedly (${postContextCheck.reason}).`;
        shouldBreakOuter = true; break;
      }
      if (action.sideEffect && status === "FAILED_VERIFICATION") {
        taskState.finalStatus = "FAILED_VERIFICATION";
        final = "Thao tác đã chạy nhưng verifier không xác nhận được tiêu chí thành công, nên tôi không coi task là hoàn tất.";
        shouldBreakOuter = true; break;
      }
      if (status === "SUCCESS") taskState.finalStatus = "SUCCESS";
    }

    // If the action failed because an element wasn't found, run a
    // focused discovery pass and feed results back to the model so it can
    // pick a correct selector or a different strategy instead of blind retries.
    const errText = String(result.error || result.output || "");
    if (!logicalOk && /element not found/i.test(errText)) {
      try {
        await runObservation(trace, history, "NOT_FOUND_OBSERVATION", {
          includeHtml: false,
          commands: observationPlanForAction(toolCmd, toolArgs, "not_found")
        });
        nextInput = `Hành động '${toolCmd}' thất bại: không tìm thấy phần tử. Tôi đã thêm OBSERVATION mới vào lịch sử. Hãy chọn chiến lược tiếp theo (tìm selector mới, click element khác, quan sát thêm, hoặc báo lỗi/hỏi người dùng).`;
        continue;
      } catch (e) {
        // If discovery also fails, proceed as usual and let model decide.
      }
    }

    const shortResult = compactResult;
    const resultText = `Ket qua tool ${toolCmd} (${result.ms}ms, ok=${logicalOk}): ${shortResult}`;
    history.push({ role: "model", text: JSON.stringify(model.parsed) });
    history.push({ role: "user", text: resultText.slice(0, TOOL_RESULT_CHARS + 200) });
    nextInput = `Tiep tuc tu ket qua tool va OBSERVATION moi nhat trong lich su. Neu vua thao tac, hay kiem tra VERIFY_OBSERVATION de xac nhan dung trang thai truoc khi lam tiep. Neu xong thi final, neu can thao tac tiep thi goi tool tiep. Neu khong du chac chan thi hoi nguoi dung. Khong lap lai tool vua loi qua 2 lan.\n${resultText.slice(0, TOOL_RESULT_CHARS + 200)}`;

    }
    
    if (nextInputs.length > 0) {
      nextInput = nextInputs.join("\n");
    }
    
    if (shouldBreakOuter) {
      break;
    }
  }

  if (!final) {
    const lastTool = [...trace].reverse().find(x => x.type === "tool");
    final = Number.isFinite(MAX_STEPS)
      ? `Đã chạy ${MAX_STEPS} bước nên tôi dừng để tránh thao tác mù. Trạng thái gần nhất: ${lastTool ? lastTool.summary : "chưa có kết quả tool"}`
      : `Tôi đã dừng vì chưa có kết luận cuối từ model. Trạng thái gần nhất: ${lastTool ? lastTool.summary : "chưa có kết quả tool"}`;
  }
  if (taskState.finalStatus === "RUNNING") taskState.finalStatus = final.startsWith("Dừng") ? "FAILED_VERIFICATION" : "PARTIAL";
  audit(taskState, { type: "task_finished", status: taskState.finalStatus, final });
  const keptHistory = history.slice(-24);
  keptHistory.modelId = history.modelId;
  conversations.set(sessionId, keptHistory);
  return { final, trace, audit: taskState.audit, status: taskState.finalStatus, invariants: taskState.invariants };
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${HOST}:${PORT}`);
    if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html")) {
      const file = path.join(__dirname, "index.html");
      const html = fs.readFileSync(file);
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(html);
      return;
    }
    if (req.method === "GET" && url.pathname === "/health") {
      sendJson(res, 200, { ok: true, defaultModel: DEFAULT_MODEL, models: MODELS, browserDriver: BROWSER_DRIVER, browserDom: BROWSER_DOM, browserDomScript: BROWSER_DOM_SCRIPT, browserPlaywrightScript: BROWSER_PLAYWRIGHT_SCRIPT });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/chat") {
      const body = JSON.parse(await readBody(req));
      const sessionId = resolveSessionId(body.sessionId);
      if (body.forceReset) resetSession(sessionId);
      const message = String(body.message || "").trim();
      const model = normalizeModel(body.model);
      if (!message) return sendJson(res, 400, { ok: false, error: "Tin nhắn trống." });
      const started = Date.now();
      const result = await agentTurn(sessionId, message, model);
      sendJson(res, 200, { ok: true, sessionId, model, ms: Date.now() - started, ...result });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/session/reset") {
      const body = JSON.parse(await readBody(req));
      const result = resetSession(body.sessionId);
      sendJson(res, 200, result);
      return;
    }
    sendJson(res, 404, { ok: false, error: "Not found" });
  } catch (error) {
    sendJson(res, 500, { ok: false, error: String(error.message || error) });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`Fast Browser Agent: http://${HOST}:${PORT}/`);
});
