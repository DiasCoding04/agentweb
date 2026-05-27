#!/usr/bin/env node
import http from 'node:http';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';

const PORT = Number(process.env.OPENCLAW_BROWSER_CDP_PORT || 9222);
const PROFILE = process.env.OPENCLAW_BROWSER_PROFILE || 'C:\\Users\\pc\\.openclaw\\browser-profile';
const CHROME_CANDIDATES = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
];

const args = process.argv.slice(2);
const cmd = args.shift() || 'help';

function parseArgs(items) {
  const out = { _: [] };
  for (let i = 0; i < items.length; i++) {
    const a = items[i];
    if (a.startsWith('--')) {
      const k = a.slice(2);
      const next = items[i + 1];
      if (next === undefined || next.startsWith('--')) out[k] = true;
      else { out[k] = next; i++; }
    } else out._.push(a);
  }
  return out;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function requestJson(url, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = http.request(url, { method }, res => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', c => data += c);
      res.on('end', () => {
        if (res.statusCode < 200 || res.statusCode >= 300) return reject(new Error(`HTTP ${res.statusCode}: ${data.slice(0, 300)}`));
        try { resolve(data ? JSON.parse(data) : null); } catch { resolve(data); }
      });
    });
    req.on('error', reject);
    req.setTimeout(7000, () => req.destroy(new Error('timeout')));
    req.end();
  });
}

async function isRunning() {
  try { await requestJson(`http://127.0.0.1:${PORT}/json/version`); return true; }
  catch { return false; }
}

async function ensureBrowser(url = 'about:blank') {
  if (await isRunning()) return;
  const chrome = CHROME_CANDIDATES.find(existsSync);
  if (!chrome) throw new Error('Chrome/Edge not found');
  mkdirSync(PROFILE, { recursive: true });
  const child = spawn(chrome, [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE}`,
    '--no-first-run',
    '--disable-features=Translate,MediaRouter',
    url
  ], { detached: true, stdio: 'ignore' });
  child.unref();
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    if (await isRunning()) return;
  }
  throw new Error('Browser CDP did not start');
}

async function tabs() {
  await ensureBrowser();
  const list = await requestJson(`http://127.0.0.1:${PORT}/json/list`);
  return list.filter(t => t.type === 'page');
}

async function getTab(opts = {}) {
  const list = await tabs();
  if (opts.id) {
    const found = list.find(t => t.id === opts.id);
    if (found) return found;
  }
  if (opts.index !== undefined) {
    const idx = Number(opts.index);
    if (Number.isInteger(idx) && list[idx]) return list[idx];
  }
  const match = opts.match || opts.url || opts.title;
  if (match) {
    const found = list.find(t => (t.url || '').includes(match) || (t.title || '').includes(match));
    if (found) return found;
  }
  return list[0] || await requestJson(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent('about:blank')}`, 'PUT');
}

class Cdp {
  constructor(wsUrl) { this.wsUrl = wsUrl; this.next = 1; this.pending = new Map(); }
  async open() {
    this.ws = new WebSocket(this.wsUrl);
    await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('CDP websocket timeout')), 6000);
      this.ws.addEventListener('open', () => { clearTimeout(t); resolve(); }, { once: true });
      this.ws.addEventListener('error', err => { clearTimeout(t); reject(err.error || err); }, { once: true });
    });
    this.ws.addEventListener('message', ev => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      }
    });
  }
  call(method, params = {}) {
    const id = this.next++;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => { this.pending.delete(id); reject(new Error(`${method} timeout`)); }, 20000);
      this.pending.set(id, { resolve: v => { clearTimeout(t); resolve(v); }, reject: e => { clearTimeout(t); reject(e); } });
    });
  }
  close() { try { this.ws.close(); } catch {} }
}

async function withPage(fn, opts = {}) {
  const tab = await getTab(opts);
  const cdp = new Cdp(tab.webSocketDebuggerUrl);
  await cdp.open();
  try {
    await cdp.call('Runtime.enable').catch(() => {});
    await cdp.call('Page.enable').catch(() => {});
    return await fn(cdp, tab);
  } finally { cdp.close(); }
}

async function evalPage(cdp, expression, awaitPromise = true) {
  const res = await cdp.call('Runtime.evaluate', { expression, awaitPromise, returnByValue: true, userGesture: true });
  if (res.exceptionDetails) throw new Error(res.exceptionDetails.exception?.description || res.exceptionDetails.text || 'Runtime exception');
  return res.result?.value;
}

const helperSource = String.raw`
(() => {
  window.__ocDom = window.__ocDom || {};
  window.__ocConsoleErrors = window.__ocConsoleErrors || [];
  if (!window.__ocConsoleHooked) {
    window.__ocConsoleHooked = true;
    const origError = console.error.bind(console);
    console.error = (...args) => {
      window.__ocConsoleErrors.push(args.map(x => {
        try { return typeof x === 'string' ? x : JSON.stringify(x); } catch { return String(x); }
      }).join(' ').slice(0,500));
      if (window.__ocConsoleErrors.length > 50) window.__ocConsoleErrors.splice(0, window.__ocConsoleErrors.length - 50);
      return origError(...args);
    };
    window.addEventListener('error', ev => {
      window.__ocConsoleErrors.push(String(ev.message || ev.error || 'error').slice(0,500));
    });
  }
  window.__ocDom.cssPath = (el) => {
    if (!el || el.nodeType !== 1) return '';
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 6) {
      let s = el.tagName.toLowerCase();
      if (el.getAttribute('name')) s += '[name="' + CSS.escape(el.getAttribute('name')) + '"]';
      else if (el.getAttribute('data-testid')) s += '[data-testid="' + CSS.escape(el.getAttribute('data-testid')) + '"]';
      else {
        const parent = el.parentElement;
        if (parent) {
          const same = [...parent.children].filter(x => x.tagName === el.tagName);
          if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
        }
      }
      parts.unshift(s); el = el.parentElement;
    }
    return parts.join(' > ');
  };
  window.__ocDom.label = (el) => [
    el.innerText, el.value, el.placeholder, el.ariaLabel, el.title, el.alt,
    el.getAttribute('aria-label'), el.getAttribute('name'), el.getAttribute('data-testid'), el.getAttribute('href')
  ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
  window.__ocDom.visible = (el) => {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none' && st.pointerEvents !== 'none';
  };
  window.__ocDom.rect = (el) => {
    const r = el.getBoundingClientRect();
    let x = r.x;
    let y = r.y;
    let win = el.ownerDocument?.defaultView;
    while (win && win.frameElement) {
      const fr = win.frameElement.getBoundingClientRect();
      x += fr.x;
      y += fr.y;
      win = win.frameElement.ownerDocument?.defaultView;
    }
    return { x, y, width:r.width, height:r.height, left:x, top:y, right:x+r.width, bottom:y+r.height };
  };
  window.__ocDom.actionable = (el) => {
    if (!el || !window.__ocDom.visible(el)) return false;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
    const r = window.__ocDom.rect(el);
    const cx = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1);
    const cy = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1);
    const top = document.elementFromPoint(cx, cy);
    return !top || top === el || el.contains(top) || top.contains(el);
  };
  window.__ocDom.deepQueryAll = (selector, root = document) => {
    const out = [];
    const seenRoots = new Set();
    const seenElements = new Set();
    const push = (el) => {
      if (el && el.nodeType === 1 && !seenElements.has(el)) {
        seenElements.add(el);
        out.push(el);
      }
    };
    const visit = (scope) => {
      if (!scope || seenRoots.has(scope)) return;
      seenRoots.add(scope);
      let matches = [];
      try { matches = [...scope.querySelectorAll(selector)]; } catch { matches = []; }
      matches.forEach(push);

      let all = [];
      try { all = [...scope.querySelectorAll('*')]; } catch { all = []; }
      for (const el of all) {
        if (el.shadowRoot) visit(el.shadowRoot);
        if (el.tagName === 'IFRAME') {
          try {
            if (el.contentDocument) visit(el.contentDocument);
          } catch {}
        }
      }
    };
    visit(root);
    return out;
  };
  window.__ocDom.candidates = (selector = '') => {
    const base = window.__ocDom.selector;
    if (!selector) return window.__ocDom.deepQueryAll(base).filter(window.__ocDom.visible);
    let roots = [];
    try { roots = window.__ocDom.deepQueryAll(selector); } catch { return []; }
    const out = [];
    const seen = new Set();
    const add = (el) => {
      if (el && el.nodeType === 1 && window.__ocDom.visible(el) && !seen.has(el)) {
        seen.add(el);
        out.push(el);
      }
    };
    roots.forEach(root => {
      add(root);
      try { root.querySelectorAll?.(base).forEach(add); } catch {}
      if (root.shadowRoot) {
        window.__ocDom.deepQueryAll(base, root.shadowRoot).forEach(add);
      }
      if (root.tagName === 'IFRAME') {
        try {
          if (root.contentDocument) window.__ocDom.deepQueryAll(base, root.contentDocument).forEach(add);
        } catch {}
      }
    });
    return out;
  };
  window.__ocDom.selector = 'a,button,input,textarea,select,option,[role="button"],[role="link"],[role="menuitem"],[contenteditable="true"],[aria-label],[tabindex],[data-testid]';
  window.__ocDom.nearestInteractiveAncestor = (el) => {
    if (!el || el.nodeType !== 1) return null;
    const direct = el.closest('a[href],button,[role="button"],[role="link"],[role="menuitem"],[onclick],li,article,tr,[data-testid],[tabindex]');
    if (direct && window.__ocDom.visible(direct)) return direct;
    const fallback = el.closest('div,section,article,li,tr');
    return fallback && window.__ocDom.visible(fallback) ? fallback : null;
  };
  window.__ocDom.elements = (rootSelector = '') => {
    return window.__ocDom.candidates(rootSelector).map((el, i) => {
    const r = window.__ocDom.rect(el);
    const area = Math.round(r.width * r.height);
    return { index:i, tag:el.tagName.toLowerCase(), type:el.type||'', role:el.getAttribute('role')||'', text:window.__ocDom.label(el).slice(0,220), selector:window.__ocDom.cssPath(el), x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height), area, href:el.href||'', checked:!!el.checked, disabled:!!el.disabled, occluded:!window.__ocDom.actionable(el) };
    });
  };
  window.__ocDom.find = (query, selector, index) => {
    let el = null;
    if (selector) {
      try { el = window.__ocDom.deepQueryAll(selector)[0] || null; } catch { el = null; }
    }
    const all = window.__ocDom.candidates();
    if (!el && index !== undefined && index !== null && all[Number(index)]) el = all[Number(index)];
    if (!el && query) {
      const q = String(query).toLowerCase();
      const candidates = all
        .map((node) => {
          const r = window.__ocDom.rect(node);
          return {
            node,
            tag: node.tagName.toLowerCase(),
            role: node.getAttribute('role') || '',
            text: window.__ocDom.label(node),
            selector: window.__ocDom.cssPath(node),
            area: r.width * r.height,
            y: r.y,
            occluded: !window.__ocDom.actionable(node),
            disabled: !!node.disabled || node.getAttribute('aria-disabled') === 'true'
          };
        })
        .filter(x => x.text.toLowerCase().includes(q) || x.selector.toLowerCase().includes(q))
        .filter(x => !x.disabled)
        .sort((a, b) => {
          const aStarts = a.text.toLowerCase().startsWith(q) ? 0 : 1;
          const bStarts = b.text.toLowerCase().startsWith(q) ? 0 : 1;
          const ar = (a.role === 'link' || a.tag === 'a') ? 0 : (a.role === 'button' || a.tag === 'button') ? 1 : 2;
          const br = (b.role === 'link' || b.tag === 'a') ? 0 : (b.role === 'button' || b.tag === 'button') ? 1 : 2;
          return aStarts - bStarts || Number(a.occluded) - Number(b.occluded) || ar - br || a.area - b.area || b.y - a.y;
        });
      const match = candidates[0];
      if (match) el = match.node;
      if (!el) {
        const textNodes = [...document.querySelectorAll('body *')]
          .filter(node => window.__ocDom.visible(node))
          .map(node => {
            const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
            const r = window.__ocDom.rect(node);
            return { node, text, area: r.width * r.height, y: r.y };
          })
          .filter(x => x.text.toLowerCase().includes(q))
          .sort((a, b) => b.area - a.area || a.y - b.y);
        const textMatch = textNodes[0]?.node || null;
        if (textMatch) el = window.__ocDom.nearestInteractiveAncestor(textMatch) || textMatch;
      }
    }
    if (!el) return null;
    el.scrollIntoView({ block:'center', inline:'center' }); el.focus();
    return el;
  };
  window.__ocDom.resolve = (target = {}) => {
    const selector = target.selector || '';
    const index = target.index;
    const scope = target.scope || target.scopeSelector || '';
    const query = target.text || target.name || target.placeholder || '';
    const role = String(target.role || '').toLowerCase();
    const name = String(target.name || '').toLowerCase();
    const placeholder = String(target.placeholder || '').toLowerCase();
    const near = String(target.near || target.nearText || '').toLowerCase();

    if (selector || index !== undefined) {
      const direct = scope
        ? (() => {
            const scoped = window.__ocDom.candidates(scope);
            if (index !== undefined && index !== null && scoped[Number(index)]) return scoped[Number(index)];
            if (selector) return window.__ocDom.candidates(scope).find(el => {
              try { return el.matches(selector); } catch { return false; }
            }) || null;
            return null;
          })()
        : window.__ocDom.find(query, selector, index);
      if (direct) return direct;
    }

    const all = window.__ocDom.candidates(scope);
    const scored = all.map((node) => {
      const label = window.__ocDom.label(node);
      const hay = [
        label,
        node.getAttribute('aria-label') || '',
        node.getAttribute('placeholder') || '',
        node.getAttribute('name') || '',
        node.getAttribute('data-testid') || ''
      ].join(' ').toLowerCase();
      const nodeRole = String(node.getAttribute('role') || node.tagName.toLowerCase()).toLowerCase();
      const r = window.__ocDom.rect(node);
      let score = 0;
      if (role && (nodeRole === role || (role === 'button' && node.tagName === 'BUTTON') || (role === 'link' && node.tagName === 'A') || (role === 'textbox' && ['INPUT','TEXTAREA'].includes(node.tagName)))) score += 35;
      if (name && hay.includes(name)) score += hay.startsWith(name) ? 35 : 22;
      if (placeholder && String(node.getAttribute('placeholder') || '').toLowerCase().includes(placeholder)) score += 28;
      if (query && hay.includes(String(query).toLowerCase())) score += hay.startsWith(String(query).toLowerCase()) ? 34 : 26;
      if (near) {
        const container = node.closest('form,[role="dialog"],[role="main"],main,section,article,div');
        const context = (container?.innerText || container?.textContent || '').replace(/\s+/g, ' ').toLowerCase();
        if (context.includes(near)) score += 10;
      }
      if (!window.__ocDom.actionable(node)) score -= 25;
      if (node.disabled || node.getAttribute('aria-disabled') === 'true') score -= 40;
      return { node, score, area:r.width * r.height, y:r.y };
    }).filter(x => x.score > 0)
      .sort((a, b) => b.score - a.score || a.area - b.area || b.y - a.y);

    if (scored.length > 1 && scored[0].score === scored[1].score && Math.abs(scored[0].area - scored[1].area) < 10) {
      return { __ocAmbiguous: true, candidates: scored.slice(0, 5).map(x => ({ score:x.score, text:window.__ocDom.label(x.node).slice(0,160), selector:window.__ocDom.cssPath(x.node) })) };
    }
    const best = scored[0]?.node || null;
    if (best) {
      best.scrollIntoView({ block:'center', inline:'center' });
      best.focus();
    }
    return best;
  };
  return true;
})()`;

async function installHelpers(cdp) { await evalPage(cdp, helperSource); }

function snapshotExpr(limit = 300) { return String.raw`
(() => {
  const all = window.__ocDom.elements().slice(0, ${Number(limit) || 300});
  return { title:document.title, url:location.href, scroll:{x:scrollX,y:scrollY,w:innerWidth,h:innerHeight,totalW:document.documentElement.scrollWidth,totalH:document.documentElement.scrollHeight}, text:document.body.innerText.replace(/\s+/g,' ').slice(0,6000), elements:all };
})()`; }

function targetFromOpts(opts) {
  return {
    text: opts.text || opts._?.join?.(' ') || '',
    selector: opts.selector || '',
    index: opts.index,
    role: opts.role || '',
    name: opts.name || '',
    placeholder: opts.placeholder || '',
    near: opts.near || opts.nearText || '',
    scope: opts.scope || opts['scope-selector'] || opts.scopeSelector || ''
  };
}

function findExpr(opts) { return String.raw`
(() => {
  const el = window.__ocDom.resolve(${JSON.stringify(targetFromOpts(opts))});
  if (el && el.__ocAmbiguous) return { ok:false, error:'ambiguous target', candidates:el.candidates };
  if (!el) return { ok:false, error:'element not found' };
  const parent = window.__ocDom.nearestInteractiveAncestor(el) || el;
  const pr = window.__ocDom.rect(parent);
  const r = window.__ocDom.rect(el);
  return { ok:true, tag:el.tagName.toLowerCase(), type:el.type||'', role:el.getAttribute('role')||'', text:window.__ocDom.label(el).slice(0,220), selector:window.__ocDom.cssPath(el), parentSelector:window.__ocDom.cssPath(parent), parentRole:parent.getAttribute('role') || parent.tagName.toLowerCase(), parentName:window.__ocDom.label(parent).slice(0,220), x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2), rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}, parentRect:{x:Math.round(pr.x),y:Math.round(pr.y),w:Math.round(pr.width),h:Math.round(pr.height)}, disabled:!!el.disabled || el.getAttribute('aria-disabled') === 'true', occluded:!window.__ocDom.actionable(el) };
})()`; }

function setValueExpr(opts) { return String.raw`
(() => {
  const el = window.__ocDom.resolve(${JSON.stringify(targetFromOpts(opts))});
  if (el && el.__ocAmbiguous) return { ok:false, error:'ambiguous target', candidates:el.candidates };
  if (!el) return { ok:false, error:'element not found' };
  const value = ${JSON.stringify(opts.value || '')};
  el.focus();
  if (el.isContentEditable) el.innerText = value;
  else el.value = value;
  el.dispatchEvent(new InputEvent('input', { bubbles:true, inputType:'insertText', data:value }));
  el.dispatchEvent(new Event('change', { bubbles:true }));
  return { ok:true, value, target:{selector:window.__ocDom.cssPath(el), text:window.__ocDom.label(el).slice(0,120)} };
})()`; }

function verifyExpr(opts) { return String.raw`
(() => {
  const checks = [];
  const text = ${JSON.stringify(opts.text || '')};
  const url = ${JSON.stringify(opts.url || '')};
  const selector = ${JSON.stringify(opts.selector || '')};
  const value = ${JSON.stringify(opts.value || '')};
  const noConsoleErrors = ${JSON.stringify(!!opts.noConsoleErrors || opts.console === 'ok')};
  const noNetworkErrors = ${JSON.stringify(!!opts.noNetworkErrors || opts.network === 'ok')};
  const stable = ${JSON.stringify(!!opts.stable || opts.loading === 'false')};
  const noPopup = ${JSON.stringify(!!opts.noPopup || opts.popup === 'false')};
  const target = ${JSON.stringify(targetFromOpts(opts))};

  if (text) checks.push({ kind:'text', ok:document.body.innerText.toLowerCase().includes(text.toLowerCase()) });
  if (url) checks.push({ kind:'url', ok:new RegExp(url).test(location.href) });
  if (selector) {
    let found = false;
    try { found = !!window.__ocDom.deepQueryAll(selector)[0]; } catch {}
    checks.push({ kind:'selector', ok:found });
  }
  if (value) {
    const el = window.__ocDom.resolve(target);
    if (el && el.__ocAmbiguous) {
      checks.push({ kind:'value', ok:false, error:'ambiguous target', candidates:el.candidates });
      return { ok:false, checks, url:location.href, title:document.title };
    }
    const actual = el ? (el.isContentEditable ? el.innerText : el.value || '') : '';
    checks.push({ kind:'value', ok:actual === value, actual });
  }
  if (stable) {
    const loading = document.readyState !== 'complete' || !!document.querySelector('[aria-busy="true"],[role="progressbar"],[data-loading="true"]');
    checks.push({ kind:'stable', ok:!loading });
  }
  if (noPopup) {
    const popup = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"]')].some(el => {
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    });
    checks.push({ kind:'noPopup', ok:!popup });
  }
  if (noConsoleErrors) {
    checks.push({ kind:'console', ok:(window.__ocConsoleErrors || []).length === 0, errors:(window.__ocConsoleErrors || []).slice(-5) });
  }
  if (noNetworkErrors) {
    const bad = performance.getEntriesByType('resource').filter(e => e.responseStatus && e.responseStatus >= 400).slice(-10);
    checks.push({ kind:'network', ok:bad.length === 0, errors:bad.map(e => ({ name:e.name, status:e.responseStatus })) });
  }
  return { ok:checks.length > 0 && checks.every(c => c.ok), checks, url:location.href, title:document.title };
})()`; }

function stateExpr() { return String.raw`
(() => {
  const dialogs = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"]')]
    .filter(el => {
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    })
    .map(el => ({ text:(el.innerText || el.textContent || '').replace(/\s+/g,' ').slice(0,300), selector:window.__ocDom.cssPath(el) }));
  const loading = document.readyState !== 'complete' || !!document.querySelector('[aria-busy="true"],[role="progressbar"],[data-loading="true"]');
  const networkErrors = performance.getEntriesByType('resource')
    .filter(e => e.responseStatus && e.responseStatus >= 400)
    .slice(-10)
    .map(e => ({ name:e.name, status:e.responseStatus }));
  return {
    ok:true,
    url:location.href,
    title:document.title,
    readyState:document.readyState,
    loading,
    dialogs,
    consoleErrors:(window.__ocConsoleErrors || []).slice(-10),
    networkErrors
  };
})()`; }

async function act(cdp, opts) {
  const action = String(opts.action || opts.cmd || opts._[0] || 'click').toLowerCase();
  const value = opts.value || opts._.slice(1).join(' ');
  const targetInfo = await evalPage(cdp, findExpr({ ...opts, text: opts.text || '' }));
  if (!targetInfo.ok) return { ok:false, phase:'resolve', error:targetInfo.error || 'target not found', target:targetInfo };
  if (targetInfo.disabled) return { ok:false, phase:'precheck', error:'element disabled', target:targetInfo };
  if (targetInfo.occluded && action === 'click') return { ok:false, phase:'precheck', error:'element occluded or not clickable at center', target:targetInfo };

  if (action === 'click') {
    await cdp.call('Input.dispatchMouseEvent', { type:'mousePressed', x:targetInfo.x, y:targetInfo.y, button:'left', clickCount:1 });
    await cdp.call('Input.dispatchMouseEvent', { type:'mouseReleased', x:targetInfo.x, y:targetInfo.y, button:'left', clickCount:1 });
  } else if (action === 'type') {
    const current = await evalPage(cdp, String.raw`
(() => {
  const el = window.__ocDom.resolve(${JSON.stringify(targetFromOpts(opts))});
  if (!el || el.__ocAmbiguous) return { ok:false, value:'' };
  return { ok:true, value:el.isContentEditable ? el.innerText : (el.value || '') };
})()`);
    if (current.ok && current.value.includes(value)) {
      return { ok:true, action, skipped:true, reason:'target already contains intended payload', target:targetInfo, verification:{ ok:true, checks:[{ kind:'value', ok:true, actual:current.value }] } };
    }
    const setResult = await evalPage(cdp, setValueExpr({ ...opts, value }));
    if (!setResult.ok) return { ok:false, phase:'execute', error:setResult.error || 'type/set failed', target:targetInfo, result:setResult };
  } else if (action === 'set' || action === 'clear') {
    const setValue = action === 'clear' ? '' : value;
    const setResult = await evalPage(cdp, setValueExpr({ ...opts, value:setValue }));
    if (!setResult.ok) return { ok:false, phase:'execute', error:setResult.error || 'set failed', target:targetInfo, result:setResult };
  } else if (action === 'press' || action === 'key') {
    const name = opts.key || opts.name || value || 'Enter';
    const codeMap = { Enter:13, Tab:9, Escape:27, Backspace:8, Delete:46, ArrowDown:40, ArrowUp:38, ArrowLeft:37, ArrowRight:39 };
    await cdp.call('Input.dispatchKeyEvent', { type:'keyDown', key:name, code:name, windowsVirtualKeyCode:codeMap[name] || 0 });
    await cdp.call('Input.dispatchKeyEvent', { type:'keyUp', key:name, code:name, windowsVirtualKeyCode:codeMap[name] || 0 });
  } else {
    return { ok:false, phase:'execute', error:`unknown act action: ${action}`, target:targetInfo };
  }

  await sleep(Number(opts.wait || 300));
  const verifyOpts = {
    ...opts,
    text: opts.expectText || opts['expect-text'] || opts.verifyText || opts['verify-text'] || '',
    url: opts.expectUrl || opts['expect-url'] || opts.verifyUrl || opts['verify-url'] || '',
    selector: opts.expectSelector || opts['expect-selector'] || opts.verifySelector || opts['verify-selector'] || '',
    value: opts.expectValue || opts['expect-value'] || opts.verifyValue || opts['verify-value'] || ''
  };
  const shouldVerify = verifyOpts.text || verifyOpts.url || verifyOpts.selector || verifyOpts.value || opts.stable || opts.noPopup || opts.noConsoleErrors || opts.noNetworkErrors;
  const verification = shouldVerify ? await evalPage(cdp, verifyExpr(verifyOpts)) : { ok:true, checks:[] };
  return { ok:verification.ok, action, target:targetInfo, verification };
}

function tableExpr() { return String.raw`
(() => [...document.querySelectorAll('table')].map((table, tableIndex) => {
  const rows = [...table.querySelectorAll('tr')].map(tr => [...tr.children].map(td => td.innerText.replace(/\s+/g,' ').trim()));
  return { tableIndex, rows };
}))()`; }

function formsExpr() { return String.raw`
(() => {
  const forms = [...document.querySelectorAll('form')].map((form, formIndex) => ({
    formIndex,
    selector: window.__ocDom.cssPath(form),
    text: (form.innerText || form.textContent || '').replace(/\s+/g,' ').slice(0,500),
    fields: [...form.querySelectorAll('input,textarea,select,button,[contenteditable="true"],[role="textbox"]')].map((el, index) => ({ index, tag:el.tagName.toLowerCase(), type:el.type||'', role:el.getAttribute('role')||'', name:el.name||'', placeholder:el.placeholder||'', value:el.type === 'password' ? '[password]' : (el.value||''), text:window.__ocDom.label(el).slice(0,160), selector:window.__ocDom.cssPath(el) }))
  }));
  const looseFields = window.__ocDom.deepQueryAll('input,textarea,select,[contenteditable="true"],[role="textbox"]')
    .filter(window.__ocDom.visible)
    .map((el, index) => ({ index, tag:el.tagName.toLowerCase(), type:el.type||'', role:el.getAttribute('role')||'', name:el.name||'', placeholder:el.placeholder||'', value:el.type === 'password' ? '[password]' : (el.value||''), text:window.__ocDom.label(el).slice(0,160), selector:window.__ocDom.cssPath(el), rect:(() => { const r = window.__ocDom.rect(el); return { x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height) }; })(), occluded:!window.__ocDom.actionable(el) }));
  return { forms, looseFields };
})()`; }

function listsExpr(kind, selector = '') { return String.raw`
(() => {
  const selector = ${JSON.stringify(selector || '')};
  const kind = ${JSON.stringify(kind)};
  const list = window.__ocDom.elements(selector);
  return list.filter(e => kind === 'all' || e.tag === kind || e.role === kind || (e.tag === 'input' && kind === 'input')).slice(0,500);
})()`; }

function scrollExpr(opts) { return String.raw`
(() => {
  const amount = Number(${JSON.stringify(opts.y ?? opts.delta ?? 700)}) || 700;
  const x = Number(${JSON.stringify(opts.x ?? 0)}) || 0;
  window.scrollBy({ left:x, top:amount, behavior:'instant' });
  return { ok:true, scroll:{x:scrollX,y:scrollY,w:innerWidth,h:innerHeight,totalW:document.documentElement.scrollWidth,totalH:document.documentElement.scrollHeight} };
})()`; }

function scrollToTextExpr(text) { return String.raw`
(() => {
  const q = ${JSON.stringify(text || '')}.toLowerCase();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let node;
  while ((node = walker.nextNode())) {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g,' ').trim();
    if (t && t.toLowerCase().includes(q) && window.__ocDom.visible(node)) {
      node.scrollIntoView({ block:'center', inline:'center' });
      const r = node.getBoundingClientRect();
      return { ok:true, text:t.slice(0,240), selector:window.__ocDom.cssPath(node), rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)} };
    }
  }
  return { ok:false, error:'text not found' };
})()`; }

async function waitFor(cdp, opts) {
  const timeoutMs = Number(opts.timeout || 15000);
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const ok = await evalPage(cdp, String.raw`
(() => {
  const text = ${JSON.stringify(opts.text || '')};
  const selector = ${JSON.stringify(opts.selector || '')};
  if (selector && document.querySelector(selector)) return { ok:true, reason:'selector' };
  if (text && document.body.innerText.toLowerCase().includes(text.toLowerCase())) return { ok:true, reason:'text' };
  return { ok:false };
})()`);
    if (ok.ok) return ok;
    await sleep(300);
  }
  return { ok:false, error:'timeout' };
}

async function navigate(cdp, url) {
  await cdp.call('Page.navigate', { url });
  await sleep(1200);
  return await evalPage(cdp, `({ok:true,title:document.title,url:location.href})`);
}

async function accessibility(cdp, opts = {}) {
  const limit = Number(opts.limit || 400);
  const tree = await cdp.call('Accessibility.getFullAXTree').catch(async () => {
    await cdp.call('Accessibility.enable').catch(() => {});
    return await cdp.call('Accessibility.getFullAXTree');
  });
  const nodes = (tree.nodes || [])
    .map((node, index) => {
      const role = node.role?.value || '';
      const name = node.name?.value || '';
      const value = node.value?.value || '';
      const description = node.description?.value || '';
      const ignored = !!node.ignored;
      const props = {};
      for (const prop of node.properties || []) {
        if (['disabled','focused','focusable','editable','expanded','selected','checked','pressed','modal','multiline','readonly','required'].includes(prop.name)) {
          props[prop.name] = prop.value?.value;
        }
      }
      return { index, role, name, value, description, ignored, props };
    })
    .filter(node => !node.ignored && (node.name || node.value || node.role))
    .filter(node => !['generic','none','StaticText','InlineTextBox'].includes(node.role) || node.name)
    .slice(0, limit);
  return { title:'Accessibility tree', nodes };
}

async function main() {
  const opts = parseArgs(args);
  if (cmd === 'help') {
    console.log(`Commands:
  start --url URL | goto --url URL | new-tab --url URL
  tabs | switch --index N|--match TEXT | close-tab --index N|--match TEXT
  read | snapshot [--limit N] | accessibility [--limit N] | elements | links | buttons | inputs | forms | tables
  find --text TEXT|--selector CSS|--index N|--role ROLE|--name NAME|--placeholder TEXT|--near TEXT
  act --action click|type|set|clear|press --role ROLE --name NAME --value VALUE [--expect-text TEXT|--expect-url REGEX|--expect-value VALUE]
  click --text TEXT|--selector CSS|--index N|--role ROLE|--name NAME|--near TEXT | click-index N
  type --text TEXT|--selector CSS|--index N|--role ROLE|--name NAME|--placeholder TEXT|--near TEXT --value VALUE | set --... --value VALUE | clear --...
  key --name Enter|Tab|Escape|Backspace | enter | tab-key | escape
  scroll --y 700 | scroll-top | scroll-bottom | scroll-to-text --text TEXT
  wait-for-text --text TEXT [--timeout MS] | wait-for-selector --selector CSS [--timeout MS] | verify --text TEXT|--url REGEX|--selector CSS|--value VALUE | state
  back | forward | reload | url | html | upload --selector CSS --file PATH
`);
    return;
  }

  if (cmd === 'start') { await ensureBrowser(opts.url || opts._[0] || 'about:blank'); if (opts.url || opts._[0]) await withPage(cdp => navigate(cdp, opts.url || opts._[0])); console.log(JSON.stringify({ ok:true, port:PORT, profile:PROFILE }, null, 2)); return; }
  if (cmd === 'tabs') { console.log(JSON.stringify((await tabs()).map((t,i)=>({index:i,id:t.id,title:t.title,url:t.url})), null, 2)); return; }
  if (cmd === 'new-tab') { await ensureBrowser(); const url = opts.url || opts._[0] || 'about:blank'; const tab = await requestJson(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(url)}`, 'PUT'); console.log(JSON.stringify({ ok:true, id:tab.id, url }, null, 2)); return; }
  if (cmd === 'close-tab') { const tab = await getTab(opts); await requestJson(`http://127.0.0.1:${PORT}/json/close/${tab.id}`); console.log(JSON.stringify({ ok:true, closed:{id:tab.id,title:tab.title,url:tab.url} }, null, 2)); return; }
  if (cmd === 'switch') { const tab = await getTab(opts); await requestJson(`http://127.0.0.1:${PORT}/json/activate/${tab.id}`); console.log(JSON.stringify({ ok:true, active:{id:tab.id,title:tab.title,url:tab.url} }, null, 2)); return; }

  const result = await withPage(async (cdp) => {
    await installHelpers(cdp);
    if (cmd === 'goto') return await navigate(cdp, opts.url || opts._[0] || 'about:blank');
    if (cmd === 'url') return await evalPage(cdp, `({title:document.title,url:location.href})`);
    if (cmd === 'read') { const s = await evalPage(cdp, snapshotExpr(50)); return { title:s.title, url:s.url, scroll:s.scroll, text:s.text }; }
    if (cmd === 'snapshot') return await evalPage(cdp, snapshotExpr(opts.limit || 300));
    if (cmd === 'accessibility') return await accessibility(cdp, opts);
    if (cmd === 'elements') return await evalPage(cdp, listsExpr('all', opts.selector || opts._[0] || ''));
    if (cmd === 'links') return await evalPage(cdp, listsExpr('a', opts.selector || opts._[0] || ''));
    if (cmd === 'buttons') return await evalPage(cdp, listsExpr('button', opts.selector || opts._[0] || ''));
    if (cmd === 'inputs') return await evalPage(cdp, listsExpr('input', opts.selector || opts._[0] || ''));
    if (cmd === 'forms') return await evalPage(cdp, formsExpr());
    if (cmd === 'tables') return await evalPage(cdp, tableExpr());
    if (cmd === 'state') return await evalPage(cdp, stateExpr());
    if (cmd === 'html') return await evalPage(cdp, `document.documentElement.outerHTML.slice(0, Number(${JSON.stringify(opts.max || 20000)}))`);
    if (cmd === 'find') return await evalPage(cdp, findExpr({ ...opts, text: opts.text || opts._.join(' ') }));
    if (cmd === 'act') return await act(cdp, opts);
    if (cmd === 'click-index') opts.index = opts._[0];
    if (cmd === 'click' || cmd === 'click-index') {
      const f = await evalPage(cdp, findExpr({ ...opts, text: opts.text || opts._.join(' ') }));
      if (!f.ok) return f;
      if (f.disabled) return { ok:false, error:'element disabled', target:f };
      if (f.occluded) return { ok:false, error:'element occluded or not clickable at center', target:f };
      await cdp.call('Input.dispatchMouseEvent', { type:'mousePressed', x:f.x, y:f.y, button:'left', clickCount:1 });
      await cdp.call('Input.dispatchMouseEvent', { type:'mouseReleased', x:f.x, y:f.y, button:'left', clickCount:1 });
      await sleep(300);
      return { ok:true, clicked:f };
    }
    if (cmd === 'type') {
      const f = await evalPage(cdp, findExpr({ ...opts, text: opts.text || '' }));
      if (!f.ok) return f;
      if (f.disabled) return { ok:false, error:'element disabled', target:f };
      await cdp.call('Input.insertText', { text: opts.value || opts._.join(' ') });
      return { ok:true, typed:opts.value || opts._.join(' '), target:f };
    }
    if (cmd === 'set') return await evalPage(cdp, setValueExpr({ ...opts, text: opts.text || '', value: opts.value || opts._.join(' ') }));
    if (cmd === 'clear') return await evalPage(cdp, setValueExpr({ ...opts, text: opts.text || '', value: '' }));
    if (cmd === 'key' || cmd === 'enter' || cmd === 'tab-key' || cmd === 'escape') {
      const name = cmd === 'enter' ? 'Enter' : cmd === 'tab-key' ? 'Tab' : cmd === 'escape' ? 'Escape' : (opts.name || opts._[0] || 'Enter');
      const codeMap = { Enter:13, Tab:9, Escape:27, Backspace:8, Delete:46, ArrowDown:40, ArrowUp:38, ArrowLeft:37, ArrowRight:39 };
      await cdp.call('Input.dispatchKeyEvent', { type:'keyDown', key:name, code:name, windowsVirtualKeyCode:codeMap[name] || 0 });
      await cdp.call('Input.dispatchKeyEvent', { type:'keyUp', key:name, code:name, windowsVirtualKeyCode:codeMap[name] || 0 });
      return { ok:true, key:name };
    }
    if (cmd === 'scroll') return await evalPage(cdp, scrollExpr(opts));
    if (cmd === 'scroll-top') return await evalPage(cdp, `(() => { scrollTo(0,0); return {ok:true, scroll:{x:scrollX,y:scrollY,totalH:document.documentElement.scrollHeight}} })()`);
    if (cmd === 'scroll-bottom') return await evalPage(cdp, `(() => { scrollTo(0,document.documentElement.scrollHeight); return {ok:true, scroll:{x:scrollX,y:scrollY,totalH:document.documentElement.scrollHeight}} })()`);
    if (cmd === 'scroll-to-text') return await evalPage(cdp, scrollToTextExpr(opts.text || opts._.join(' ')));
    if (cmd === 'wait-for-text') return await waitFor(cdp, { text: opts.text || opts._.join(' '), timeout: opts.timeout });
    if (cmd === 'wait-for-selector') return await waitFor(cdp, { selector: opts.selector || opts._[0], timeout: opts.timeout });
    if (cmd === 'verify') return await evalPage(cdp, verifyExpr(opts));
    if (cmd === 'back') { await cdp.call('Page.getNavigationHistory').then(h => cdp.call('Page.navigateToHistoryEntry', { entryId:h.entries[Math.max(0,h.currentIndex-1)]?.id })).catch(() => cdp.call('Runtime.evaluate', { expression:'history.back()' })); await sleep(600); return await evalPage(cdp, `({ok:true,title:document.title,url:location.href})`); }
    if (cmd === 'forward') { await cdp.call('Runtime.evaluate', { expression:'history.forward()' }); await sleep(600); return await evalPage(cdp, `({ok:true,title:document.title,url:location.href})`); }
    if (cmd === 'reload') { await cdp.call('Page.reload', { ignoreCache: !!opts.ignoreCache }); await sleep(1000); return await evalPage(cdp, `({ok:true,title:document.title,url:location.href})`); }
    if (cmd === 'upload') {
      const f = await evalPage(cdp, findExpr({ selector: opts.selector, index: opts.index, text: opts.text || '' }));
      if (!f.ok) return f;
      const doc = await cdp.call('DOM.getDocument', { depth:-1, pierce:true });
      const q = await cdp.call('DOM.querySelector', { nodeId:doc.root.nodeId, selector: f.selector || opts.selector });
      if (!q.nodeId) return { ok:false, error:'upload node not found' };
      await cdp.call('DOM.setFileInputFiles', { nodeId:q.nodeId, files:[opts.file || opts._[0]] });
      return { ok:true, file:opts.file || opts._[0] };
    }
    throw new Error('Unknown command: ' + cmd);
  }, { match: opts.match, index: opts.tabIndex, id: opts.tabId });
  console.log(JSON.stringify(result, null, 2));
}

main().catch(e => { console.error(JSON.stringify({ ok:false, error:String(e.message || e) })); process.exit(1); });
