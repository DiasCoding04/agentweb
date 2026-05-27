"use strict";

const SIDE_EFFECT_ACTIONS = new Set(["click", "type", "set", "clear", "press", "key", "submit", "send", "delete", "publish", "upload"]);
const HIGH_RISK_WORDS = /\b(send|submit|delete|remove|payment|pay|purchase|publish|budget|permission|password|transfer|gửi|xóa|xoá|thanh toán|đăng|quyền|mật khẩu)\b/i;
const WRITE_ACTIONS = new Set(["type", "set", "clear"]);
const NAV_ACTIONS = new Set(["goto", "back", "forward", "reload", "switch", "new-tab", "close-tab"]);
const ACQUIRE_ACTIONS = new Set(["click", "open", "select", "goto", "switch"]);
const ACQUISITION_STATES = Object.freeze({
  INIT: "INIT",
  NAVIGATED: "NAVIGATED",
  TARGET_SEARCHING: "TARGET_SEARCHING",
  TARGET_CANDIDATE_FOUND: "TARGET_CANDIDATE_FOUND",
  TARGET_OPENING: "TARGET_OPENING",
  TARGET_CONTEXT_VERIFYING: "TARGET_CONTEXT_VERIFYING",
  TARGET_LOCKED: "TARGET_LOCKED",
  ACTION_READY: "ACTION_READY",
  VERIFYING_SUCCESS: "VERIFYING_SUCCESS",
  DONE: "DONE",
  FAILED: "FAILED"
});

function normalizeText(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function argValue(args, name) {
  const index = Array.isArray(args) ? args.indexOf(name) : -1;
  return index >= 0 ? args[index + 1] : undefined;
}

function hasArg(args, name) {
  return Array.isArray(args) && args.includes(name);
}

function parseTaskInvariants(request) {
  const raw = normalizeText(request);
  const parsedTask = parseUserTask(raw);
  const quoted = [...raw.matchAll(/["“”']([^"“”']{1,500})["“”']/g)].map(m => normalizeText(m[1]));
  const intendedAction =
    parsedTask.intent === "send_message" ? "send" :
    parsedTask.intent === "youtube_search" || parsedTask.intent === "open_website" ? "navigate" :
    /\b(gửi|send|nhắn|message)\b/i.test(raw) ? "send" :
    /\b(xóa|xoá|delete|remove)\b/i.test(raw) ? "delete" :
    /\b(đăng|publish|post)\b/i.test(raw) ? "publish" :
    /\b(nhập|gõ|type|set|fill)\b/i.test(raw) ? "fill" :
    /\b(mở|vào|open|go|navigate)\b/i.test(raw) ? "navigate" :
    "unknown";

  const payloadMarkers = [
    /(?:nội dung|message|tin nhắn|payload|value|giá trị)\s*(?:là|:)\s*(.+)$/i,
    /(?:gửi|send|nhắn|message)\s+["“']([^"“”']+)["”']/i
  ];
  let intendedPayload = quoted[0] || "";
  for (const pattern of payloadMarkers) {
    const match = raw.match(pattern);
    if (match) {
      intendedPayload = normalizeText(match[1]);
      break;
    }
  }

  const targetPatterns = [
    /(?:cho|tới|đến|to|for|với|trong|in|on)\s+([^,.!?]{2,120})/i,
    /(?:target|recipient|record|item|campaign|page|object)\s*(?:là|:)\s*([^,.!?]{2,120})/i,
    /(?:record|item|campaign|page|object)\s+([^,.!?]{2,120})/i,
    /(?:open|mở|vào|navigate)\s+([^,.!?]{2,120})/i
  ];
  const targetHints = [];
  for (const pattern of targetPatterns) {
    const match = raw.match(pattern);
    if (match) {
      let target = normalizeText(match[1]);
      target = target
        .replace(/^(?:cho|to|for|với)\s+/i, "")
        .replace(/\s*[:,-]\s*(?:tin nhắn|message|nội dung|payload|value)\b.*$/i, "")
        .replace(/\s+\b(?:tin nhắn|message|nội dung|payload|value)\b.*$/i, "")
        .replace(/\b(and|và|rồi|để|then)\b.*$/i, "")
        .trim();
      if (target) targetHints.push(target);
    }
  }
  if (parsedTask.target) targetHints.unshift(parsedTask.target);
  for (const value of quoted) {
    if (value && value !== intendedPayload) targetHints.push(value);
  }
  if (parsedTask.intent === "open_website" || parsedTask.intent === "youtube_search") targetHints.length = 0;
  if (parsedTask.payload) intendedPayload = parsedTask.payload;
  if (parsedTask.query) intendedPayload = parsedTask.query;

  const riskLevel = HIGH_RISK_WORDS.test(raw) ? "high" : intendedAction === "unknown" ? "medium" : "low";
  const successCriteria = [];
  if (intendedPayload) successCriteria.push({ kind: "payload", value: intendedPayload });
  if (targetHints[0]) successCriteria.push({ kind: "target", value: targetHints[0] });
  if (["send", "submit", "publish", "delete"].includes(intendedAction)) successCriteria.push({ kind: "committed", value: intendedAction });

  return {
    originalRequest: raw,
    parsedTask,
    intendedAction,
    targetEntity: targetHints[0] || "",
    targetHints: [...new Set(targetHints.filter(Boolean))].slice(0, 5),
    intendedPayload,
    riskLevel,
    successCriteria,
    createdAt: new Date().toISOString(),
    status: "RUNNING"
  };
}

function parseUserTask(request) {
  const raw = normalizeText(request);
  const lower = raw.toLowerCase();
  const sendMatch = raw.match(/(?:vào\s+mess(?:enger)?(?:\s+trên\s+facebook)?\s+và\s+)?nhắn(?:\s+tin)?\s+cho\s+([^:]+):\s*(.+)$/i);
  if (sendMatch) {
    const target = normalizeText(sendMatch[1]).replace(/\s+[:].*$/, "").trim();
    const payload = normalizeText(sendMatch[2]);
    return {
      intent: "send_message",
      app: /mess|facebook/i.test(raw) ? "messenger" : "",
      target,
      searchTarget: target,
      payload,
      query: "",
      rawRequest: raw
    };
  }
  const youtubeSearchPatterns = [
    /^(?:vào|mở)\s+youtube\s+(?:mở|tìm|bật)\s+(.+)$/i,
    /^youtube\s+(?:mở|tìm|bật)\s+(.+)$/i
  ];
  for (const pattern of youtubeSearchPatterns) {
    const match = raw.match(pattern);
    if (match) {
      return {
        intent: "youtube_search",
        app: "youtube",
        target: "",
        searchTarget: "",
        payload: "",
        query: normalizeText(match[1]),
        rawRequest: raw
      };
    }
  }
  if (/^(?:vào|mở)\s+youtube$/i.test(lower) || /^youtube$/i.test(lower)) {
    return {
      intent: "open_website",
      app: "youtube",
      target: "",
      searchTarget: "",
      payload: "",
      query: "",
      rawRequest: raw
    };
  }
  return {
    intent: "",
    app: "",
    target: "",
    searchTarget: "",
    payload: "",
    query: "",
    rawRequest: raw
  };
}

function classifyCommand(cmd, args = []) {
  const action = cmd === "act" ? String(argValue(args, "--action") || "click").toLowerCase() : String(cmd || "").toLowerCase();
  const riskText = [action, argValue(args, "--name"), argValue(args, "--text"), argValue(args, "--value")].filter(Boolean).join(" ");
  const sideEffect = SIDE_EFFECT_ACTIONS.has(action) || cmd === "upload";
  const riskLevel = HIGH_RISK_WORDS.test(riskText) ? "high" : sideEffect ? "medium" : "low";
  return { action, sideEffect, riskLevel, readOnly: !sideEffect && !NAV_ACTIONS.has(action) };
}

function actionFromTool(cmd, args = []) {
  const kind = classifyCommand(cmd, args);
  const expected = {
    text: argValue(args, "--expect-text") || argValue(args, "--verify-text") || "",
    url: argValue(args, "--expect-url") || argValue(args, "--verify-url") || "",
    selector: argValue(args, "--expect-selector") || argValue(args, "--verify-selector") || "",
    value: argValue(args, "--expect-value") || argValue(args, "--verify-value") || ""
  };
  return {
    type: kind.action,
    cmd,
    args,
    target: {
      role: argValue(args, "--role") || "",
      name: argValue(args, "--name") || "",
      text: argValue(args, "--text") || "",
      selector: argValue(args, "--selector") || "",
      index: argValue(args, "--index") || "",
      placeholder: argValue(args, "--placeholder") || "",
      near: argValue(args, "--near") || ""
    },
    scope: argValue(args, "--scope") || argValue(args, "--scope-selector") || "",
    value: argValue(args, "--value") || "",
    expected,
    sideEffect: kind.sideEffect,
    riskLevel: argValue(args, "--risk") || kind.riskLevel,
    requiresHumanConfirmation: hasArg(args, "--confirm")
  };
}

function expectedPresent(expected) {
  return !!(expected?.text || expected?.url || expected?.selector || expected?.value);
}

function inferTaskIntent(taskState) {
  const parsedIntent = String(taskState?.invariants?.parsedTask?.intent || "").toLowerCase();
  if (parsedIntent) return parsedIntent;
  const action = String(taskState?.invariants?.intendedAction || "").toLowerCase();
  if (action === "send") return "send_message";
  if (action === "fill") return "type_text";
  if (action === "navigate") return "navigate";
  return action || "unknown";
}

function requiresLockedContext(taskState) {
  const intent = inferTaskIntent(taskState);
  return ["send_message", "delete", "publish"].includes(intent);
}

function injectDefaultExpectedResult(action, taskState) {
  const intent = inferTaskIntent(taskState);
  const immutablePayload = taskState.invariants.intendedPayload || "";
  const expected = { ...(action.expected || {}) };
  if (intent === "send_message" && /send|submit|click|press|key/.test(action.type || "")) {
    if (!expected.text && immutablePayload) expected.text = immutablePayload;
    expected.policy = expected.policy || {
      type: "message_sent",
      payloadEqualsImmutable: true,
      composerCleared: true,
      latestOutgoingMessageContains: immutablePayload || "",
      noDraftState: true,
      contextStillMatchesImmutableTarget: true
    };
  }
  if (intent === "type_text" && WRITE_ACTIONS.has(action.type)) {
    if (!expected.value && immutablePayload) expected.value = immutablePayload;
    expected.policy = expected.policy || {
      type: "field_value",
      fieldValueEquals: immutablePayload || action.value || "",
      contextStillMatchesImmutableTarget: true
    };
  }
  action.expected = expected;
  return action;
}

function validateActionTarget(action, taskState) {
  if (!action.sideEffect) return { ok: true, violations: [] };
  const violations = [];
  const lock = taskState.contextLock;
  if (!lock?.locked) {
    violations.push("side effect requires TARGET_LOCKED context");
    return { ok: false, violations };
  }
  const actionScope = normalizeText([action.scope, action.target.selector, action.target.name, action.target.text, action.target.near].join(" ")).toLowerCase();
  if (/sidebar|navigation|search|list/.test(actionScope)) {
    violations.push("side effect attempted inside navigation/sidebar/search/list scope");
  }
  const lockRegion = String(lock.lockedRegionFingerprint || "").toLowerCase();
  const targetFitsRegion = !lockRegion || !actionScope || actionScope.includes(lockRegion) || lock.hasActiveComposer;
  if (!targetFitsRegion) violations.push("action target is outside locked context region");
  return { ok: violations.length === 0, violations };
}

function validateAction(action, taskState) {
  injectDefaultExpectedResult(action, taskState);
  const violations = [];
  const lockRequiredForTask = requiresLockedContext(taskState);
  const sideEffectNeedsLock = requiresLockedContext(taskState) && action.sideEffect && (WRITE_ACTIONS.has(action.type) || /send|submit|publish|delete|upload/i.test(action.type));
  const requiresExpectedResult = action.sideEffect && (lockRequiredForTask || action.riskLevel === "high");
  if (requiresExpectedResult && !expectedPresent(action.expected)) {
    violations.push("side effect action requires expected result");
  }
  if (action.sideEffect && action.riskLevel === "high" && !action.requiresHumanConfirmation) {
    violations.push("high risk side effect requires explicit confirmation policy");
  }
  if (taskState.invariants.intendedPayload && action.value && action.value !== taskState.invariants.intendedPayload) {
    violations.push("payload differs from immutable task payload");
  }
  if (taskState.invariants.targetEntity) {
    const targetText = normalizeText([action.target.name, action.target.text, action.target.near, action.scope].join(" "));
    const isNavigation = NAV_ACTIONS.has(action.cmd) || NAV_ACTIONS.has(action.type);
    if (requiresLockedContext(taskState) && action.sideEffect && targetText && !targetText.toLowerCase().includes(taskState.invariants.targetEntity.toLowerCase()) && !taskState.contextLock?.locked && !WRITE_ACTIONS.has(action.type)) {
      violations.push("target differs from immutable task target");
    }
    if (requiresLockedContext(taskState) && action.sideEffect && !targetText && !taskState.contextLock?.locked) {
      violations.push("side effect target is not tied to immutable task target or locked context");
    }
    if (sideEffectNeedsLock && !taskState.contextLock?.locked) {
      violations.push("side effect requires TARGET_LOCKED context");
    }
    if (isNavigation && taskState.contextLock?.locked && action.sideEffect) {
      violations.push("navigation cannot be mixed with side effect inside locked context");
    }
  }
  if (action.sideEffect && /sidebar|navigation|search|list/i.test(action.scope || "") && !/select|navigate|open/i.test(action.type)) {
    violations.push("side effect attempted inside navigation/sidebar/search/list scope");
  }
  const targetValidation = requiresLockedContext(taskState) ? validateActionTarget(action, taskState) : { ok: true, violations: [] };
  violations.push(...targetValidation.violations);
  return { ok: violations.length === 0, violations };
}

function createTaskState(request) {
  return {
    invariants: parseTaskInvariants(request),
    contextLock: null,
    acquisition: {
      state: ACQUISITION_STATES.INIT,
      stateVersion: 1,
      candidates: [],
      selectedCandidate: null,
      attempts: 0,
      repeatedFinds: 0
    },
    audit: [],
    retries: {},
    finalStatus: "RUNNING"
  };
}

function audit(taskState, event) {
  const entry = { ts: new Date().toISOString(), ...event };
  taskState.audit.push(entry);
  return entry;
}

function textContainsAny(text, values) {
  const hay = String(text || "").toLowerCase();
  return values.filter(Boolean).some(v => hay.includes(String(v).toLowerCase()));
}

function summarizeObservationJson(parts) {
  const out = {};
  for (const part of parts || []) {
    if (part.cmd) out[part.cmd] = part.json;
  }
  return out;
}

function classifyRegionType(selector = "", role = "") {
  const s = String(selector || "").toLowerCase();
  const r = String(role || "").toLowerCase();
  if (r === "img" || /(^|[ >])img(\b|[.#\[])/.test(s)) return "status_badge";
  if (/tr|table/.test(s)) return "table_row";
  if (/li|list|sidebar/.test(s)) return "list_row";
  if (/card/.test(s)) return "card";
  if (r === "link" || /(^|[ >])a(\b|[.#\[])/.test(s)) return "link";
  if (r === "button" || /button/.test(s)) return "button";
  if (/conversation|thread|chat/.test(s)) return "conversation_item";
  return "interactive_region";
}

function scoreCandidate(candidate) {
  let score = 0.5;
  if (candidate.selector) score += 0.15;
  if (candidate.boundingBox?.w > 0 && candidate.boundingBox?.h > 0) score += 0.1;
  if (candidate.parentContainer?.selector) score += 0.1;
  if (candidate.parentContainer?.role || candidate.parentContainer?.name) score += 0.1;
  if (candidate.matchedText && candidate.targetText && candidate.matchedText.toLowerCase().includes(candidate.targetText.toLowerCase())) score += 0.05;
  if (candidate.regionType === "status_badge") score -= 0.3;
  return Math.max(0, Math.min(1, Number(score.toFixed(2))));
}

function createTargetCandidate(targetText, findJson = {}) {
  if (!findJson?.ok) return null;
  const matchedText = normalizeText(findJson.text || targetText);
  const selector = findJson.selector || "";
  const role = findJson.role || findJson.parentRole || "";
  const name = findJson.name || matchedText;
  const box = findJson.rect || null;
  const candidate = {
    targetText: normalizeText(targetText),
    matchedText,
    selector,
    boundingBox: box ? { x: box.x, y: box.y, w: box.w, h: box.h } : null,
    parentContainer: {
      selector: findJson.parentSelector || selector,
      role: role || "",
      name: findJson.parentName || name || ""
    },
    role,
    name,
    regionType: classifyRegionType(selector, role),
    confidence: 0,
    source: "exact_find"
  };
  candidate.confidence = scoreCandidate(candidate);
  return candidate;
}

function enrichCandidate(candidate, observation = {}) {
  if (!candidate) return null;
  const elements = Array.isArray(observation.snapshot?.elements) ? observation.snapshot.elements : [];
  if ((!candidate.parentContainer?.selector || !candidate.boundingBox) && elements.length) {
    const hint = String(candidate.selector || candidate.matchedText || "").toLowerCase();
    const match = elements.find(e => {
      const text = String(e.text || "").toLowerCase();
      const selector = String(e.selector || "").toLowerCase();
      return hint && (text.includes(hint) || selector.includes(hint));
    });
    if (match) {
      candidate.parentContainer = {
        selector: candidate.parentContainer?.selector || match.selector || "",
        role: candidate.parentContainer?.role || match.role || "",
        name: candidate.parentContainer?.name || match.text || ""
      };
      if (!candidate.boundingBox) candidate.boundingBox = { x: match.x || 0, y: match.y || 0, w: match.w || 0, h: match.h || 0 };
      if (!candidate.selector) candidate.selector = match.selector || "";
      if (!candidate.role) candidate.role = match.role || "";
      if (!candidate.name) candidate.name = match.text || "";
      candidate.regionType = classifyRegionType(candidate.selector, candidate.role);
      candidate.confidence = scoreCandidate(candidate);
    }
  }
  return candidate;
}

function setAcquisitionState(taskState, nextState, extra = {}) {
  taskState.acquisition = taskState.acquisition || {};
  taskState.acquisition.state = nextState;
  taskState.acquisition.stateVersion = (taskState.acquisition.stateVersion || 0) + 1;
  Object.assign(taskState.acquisition, extra);
  return taskState.acquisition;
}

function createOrUpdateContextLock(taskState, observation) {
  if (taskState.contextLock?.locked) return taskState.contextLock;
  const snapshotText = observation.snapshot?.text || "";
  const axText = JSON.stringify(observation.accessibility || {});
  const targetHints = taskState.invariants.targetHints || [];
  const candidate = taskState.acquisition?.selectedCandidate || taskState.acquisition?.candidates?.[0] || null;
  const preferredTarget = candidate?.targetText || candidate?.matchedText || taskState.invariants.targetEntity || "";
  const lockHints = [preferredTarget, candidate?.matchedText, taskState.invariants.targetEntity].filter(Boolean);
  const contextClass = classifyTargetContext(observation, preferredTarget);
  const hasTarget = textContainsAny(`${snapshotText} ${axText}`, targetHints);
  const hasLockHint = textContainsAny(`${snapshotText} ${axText}`, lockHints);
  const candidatePresent = !!candidate;
  const canTrustActiveThreadEvidence = contextClass.targetActive && contextClass.hasThreadishUrl && (contextClass.hasComposer || contextClass.activeAnchors) && candidatePresent;
  if ((!hasTarget && !hasLockHint && taskState.invariants.targetEntity && !canTrustActiveThreadEvidence) || !contextClass.targetActive) return null;
  const url = observation.url?.url || observation.snapshot?.url || "";
  const title = observation.url?.title || observation.snapshot?.title || "";
  const fingerprint = normalizeText([preferredTarget, title, targetHints.join(" "), candidate?.selector || ""].join(" ")).slice(0, 240);
  taskState.contextLock = {
    locked: true,
    lockedTargetName: preferredTarget,
    lockedMatchedName: candidate?.matchedText || "",
    lockedUrl: url,
    urlPattern: url ? url.split("#")[0].replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : "",
    lockedTitle: title,
    entityName: preferredTarget || "",
    targetFingerprint: fingerprint,
    lockedRegionFingerprint: candidate?.parentContainer?.selector || candidate?.selector || "",
    lockType: "conversation_or_record_context",
    regionFingerprint: contextClass.regionFingerprint || fingerprint,
    hasActiveComposer: contextClass.hasComposer,
    confidence: candidate?.confidence || 0.6,
    lockedAt: new Date().toISOString()
  };
  setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_LOCKED);
  return taskState.contextLock;
}

function classifyTargetContext(observation = {}, targetText = "") {
  const hay = normalizeText(`${observation.snapshot?.text || ""} ${JSON.stringify(observation.accessibility || {})}`).toLowerCase();
  const t = String(targetText || "").toLowerCase();
  const hasTarget = !!t && hay.includes(t);
  const hasComposer = /compose|composer|message|tin nhắn|textbox|nhập|type a message|gửi|phần soạn|soạn tin/i.test(hay);
  const listSignals = /(unread|chưa đọc|\d+\s*phút|\d+\s*giờ|ago|đến mai|online|snippet|preview)/i.test(hay);
  const manyItemsSignals = /(people you may know|contacts|conversation list|danh sách|đoạn chat khác)/i.test(hay);
  const headerSignals = /(current chat|conversation|đoạn chat|chat with|đang trò chuyện|đang mở đoạn chat)/i.test(hay);
  const activeAnchors = /(chuyển đến phần soạn|đang mở đoạn chat|mở đoạn chat|compose area)/i.test(hay);
  const genericTitle = /^(messenger|facebook|inbox|messages)$/i.test(String(observation.url?.title || observation.snapshot?.title || "").trim());
  const url = String(observation.url?.url || observation.snapshot?.url || "");
  const hasThreadishUrl = /\/t\/|\/thread|\/conversation|\/record|\/detail/i.test(url);
  const targetLikelyInList = hasTarget && (listSignals || manyItemsSignals) && !headerSignals;
  const targetActive = (hasTarget && ((hasComposer && hasThreadishUrl) || headerSignals || (hasComposer && !targetLikelyInList)))
    || (hasThreadishUrl && hasComposer && !targetLikelyInList)
    || (hasThreadishUrl && activeAnchors && hasTarget);
  return {
    targetFound: hasTarget,
    targetActive,
    targetLikelyInList,
    genericTitle,
    hasThreadishUrl,
    hasComposer,
    activeAnchors,
    regionFingerprint: normalizeText((observation.snapshot?.text || "").slice(0, 260))
  };
}

function verifyActiveContext(taskState, observation = {}, candidate = null) {
  const target = candidate?.targetText || candidate?.matchedText || taskState.invariants.targetEntity || "";
  const context = classifyTargetContext(observation, target);
  const url = observation.url?.url || observation.snapshot?.url || "";
  const hasThreadishUrl = /\/t\/|\/thread|\/conversation|\/record|\/detail/i.test(url);
  const evidence = {
    hasTarget: context.targetFound,
    hasComposer: context.hasComposer,
    hasHeaderSignal: !context.targetLikelyInList && context.targetActive,
    genericTitle: context.genericTitle,
    hasThreadishUrl
  };
  const candidatePresent = !!(candidate?.targetText || taskState.acquisition?.selectedCandidate?.targetText);
  const ok = ((evidence.hasTarget || candidatePresent) && ((evidence.hasComposer && evidence.hasThreadishUrl) || evidence.hasHeaderSignal || (evidence.hasComposer && !context.targetLikelyInList)));
  return { ok, context, evidence };
}

function ensureTargetLocked(taskState, observation = {}) {
  if (taskState.contextLock?.locked) {
    const check = assertContextStillValid(taskState, observation);
    return check.ok ? { ok: true, status: "TARGET_LOCKED", lock: taskState.contextLock } : { ok: false, status: "FAILED_CONTEXT_LOCK", reason: check.reason };
  }
  const existingCandidate = taskState.acquisition?.selectedCandidate || taskState.acquisition?.candidates?.[0] || null;
  const target = existingCandidate?.targetText || existingCandidate?.matchedText || taskState.invariants.targetEntity || "";
  let candidate = taskState.acquisition?.selectedCandidate || taskState.acquisition?.candidates?.[0] || null;
  if (!candidate && target) {
    candidate = createTargetCandidate(target, { ok: true, text: target, selector: "", role: "", rect: null });
    candidate = enrichCandidate(candidate, observation);
    if (candidate) {
      taskState.acquisition.candidates = [candidate];
      taskState.acquisition.selectedCandidate = candidate;
      setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_CANDIDATE_FOUND);
    }
  }
  if (!candidate) {
    return { ok: false, status: "FAILED_CONTEXT_LOCK", reason: "no target candidate" };
  }
  const verify = verifyActiveContext(taskState, observation, candidate);
  if (verify.ok) {
    const lock = createOrUpdateContextLock(taskState, observation);
    if (lock?.locked) return { ok: true, status: "TARGET_LOCKED", lock };
    // Fallback: when active-context verification is already strong, create a deterministic lock
    // even if strict text hint checks in createOrUpdateContextLock are inconclusive.
    const url = observation.url?.url || observation.snapshot?.url || "";
    const title = observation.url?.title || observation.snapshot?.title || "";
    const preferredTarget = candidate?.targetText || candidate?.matchedText || taskState.invariants.targetEntity || "";
    if (verify.evidence?.hasThreadishUrl && (verify.evidence?.hasComposer || verify.context?.activeAnchors)) {
      taskState.contextLock = {
        locked: true,
        lockedTargetName: preferredTarget,
        lockedMatchedName: candidate?.matchedText || "",
        lockedUrl: url,
        urlPattern: url ? url.split("#")[0].replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : "",
        lockedTitle: title,
        entityName: preferredTarget,
        targetFingerprint: normalizeText([preferredTarget, title, candidate?.selector || ""].join(" ")).slice(0, 240),
        lockedRegionFingerprint: candidate?.parentContainer?.selector || candidate?.selector || "",
        lockType: "conversation_or_record_context",
        regionFingerprint: verify.context?.regionFingerprint || normalizeText((observation.snapshot?.text || "").slice(0, 260)),
        hasActiveComposer: !!verify.evidence?.hasComposer,
        confidence: Math.max(0.72, Number(candidate?.confidence || 0.72)),
        lockedAt: new Date().toISOString()
      };
      setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_LOCKED);
      return { ok: true, status: "TARGET_LOCKED", lock: taskState.contextLock };
    }
  }
  if (verify.context.targetLikelyInList) {
    return { ok: false, status: "REQUIRES_ACQUIRE_TARGET", reason: "target appears in sidebar/list", candidate, context: verify.context };
  }
  if (verify.context.targetFound && !verify.context.targetActive) {
    return { ok: false, status: "UNKNOWN_TARGET_CONTEXT", reason: "target found but active context uncertain", candidate, context: verify.context };
  }
  const url = String(observation.url?.url || observation.snapshot?.url || "");
  const title = String(observation.url?.title || observation.snapshot?.title || "");
  const candidateTarget = String(candidate?.targetText || "").toLowerCase();
  if (/\/t\/|\/thread|\/conversation|\/record|\/detail/i.test(url) && candidateTarget && title.toLowerCase().includes(candidateTarget)) {
    const forced = createOrUpdateContextLock(taskState, observation) || {
      locked: true,
      lockedTargetName: candidate?.targetText || candidate?.matchedText || "",
      lockedMatchedName: candidate?.matchedText || "",
      lockedUrl: url,
      urlPattern: url ? url.split("#")[0].replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : "",
      lockedTitle: title,
      entityName: candidate?.targetText || candidate?.matchedText || "",
      targetFingerprint: normalizeText([candidate?.targetText || "", title, candidate?.selector || ""].join(" ")).slice(0, 240),
      lockedRegionFingerprint: candidate?.parentContainer?.selector || candidate?.selector || "",
      lockType: "conversation_or_record_context",
      regionFingerprint: normalizeText((observation.snapshot?.text || "").slice(0, 260)),
      hasActiveComposer: verify.context?.hasComposer || verify.context?.activeAnchors || false,
      confidence: Math.max(0.74, Number(candidate?.confidence || 0.74)),
      lockedAt: new Date().toISOString()
    };
    taskState.contextLock = forced;
    setAcquisitionState(taskState, ACQUISITION_STATES.TARGET_LOCKED);
    return { ok: true, status: "TARGET_LOCKED", lock: taskState.contextLock };
  }
  return { ok: false, status: "FAILED_CONTEXT_LOCK", reason: "unable to verify active context", candidate, context: verify.context };
}

function assertContextStillValid(taskState, observation) {
  const lock = taskState.contextLock;
  if (!lock?.locked) return { ok: true, reason: "no lock" };
  const url = observation.url?.url || observation.snapshot?.url || "";
  const title = observation.url?.title || observation.snapshot?.title || "";
  const body = `${observation.snapshot?.text || ""} ${JSON.stringify(observation.accessibility || {})}`;
  if (lock.urlPattern && url && !new RegExp(lock.urlPattern).test(url)) {
    return { ok: false, status: "CONTEXT_CHANGED", reason: "url changed unexpectedly", expected: lock.lockedUrl, actual: url };
  }
  if (lock.entityName && !textContainsAny(body, [lock.entityName])) {
    return { ok: false, status: "CONTEXT_CHANGED", reason: "locked entity no longer visible", expected: lock.entityName, actual: title };
  }
  return { ok: true, reason: "context valid" };
}

function retryClassFromResult(resultText) {
  const text = String(resultText || "").toLowerCase();
  if (text.includes("ambiguous")) return "ambiguous target";
  if (text.includes("element not found")) return "element not found";
  if (text.includes("occluded") || text.includes("covered")) return "element hidden/covered";
  if (text.includes("loading")) return "page loading";
  if (text.includes("context changed")) return "context changed";
  if (text.includes("network")) return "network error";
  if (text.includes("validation")) return "validation error";
  if (text.includes("draft") || text.includes("pending")) return "draft/pending state";
  return "unknown";
}

function finalStatusFromVerification(action, resultJson, contextCheck) {
  if (!contextCheck.ok) return contextCheck.status || "CONTEXT_CHANGED";
  if (resultJson?.error && /captcha/i.test(resultJson.error)) return "BLOCKED_BY_CAPTCHA";
  if (resultJson?.error && /login/i.test(resultJson.error)) return "BLOCKED_BY_LOGIN";
  if (resultJson?.error && /ambiguous/i.test(resultJson.error)) return "AMBIGUOUS_TARGET";
  if (resultJson?.ok === false) return "FAILED_VERIFICATION";
  if (action.sideEffect && resultJson?.verification?.ok === false) return "FAILED_VERIFICATION";
  if (action.sideEffect && resultJson?.verification?.ok === true) return "SUCCESS";
  return "PARTIAL";
}

module.exports = {
  ACQUIRE_ACTIONS,
  ACQUISITION_STATES,
  WRITE_ACTIONS,
  actionFromTool,
  assertContextStillValid,
  audit,
  createTargetCandidate,
  createOrUpdateContextLock,
  createTaskState,
  ensureTargetLocked,
  enrichCandidate,
  finalStatusFromVerification,
  injectDefaultExpectedResult,
  inferTaskIntent,
  requiresLockedContext,
  normalizeText,
  parseTaskInvariants,
  parseUserTask,
  retryClassFromResult,
  setAcquisitionState,
  classifyTargetContext,
  verifyActiveContext,
  summarizeObservationJson,
  validateActionTarget,
  validateAction
};
