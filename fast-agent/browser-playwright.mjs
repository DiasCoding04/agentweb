#!/usr/bin/env node
import http from 'node:http';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { chromium } from 'playwright';

const PORT = Number(process.env.OPENCLAW_BROWSER_CDP_PORT || 9222);
const PROFILE = process.env.OPENCLAW_BROWSER_PROFILE || 'C:\\Users\\pc\\.openclaw\\browser-profile';
const CDP_ENDPOINT = process.env.OPENCLAW_BROWSER_CDP_ENDPOINT || `http://127.0.0.1:${PORT}`;
const CHROME_CANDIDATES = [
  process.env.PLAYWRIGHT_CHROME_PATH || '',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
].filter(Boolean);

const args = process.argv.slice(2);
const cmd = args.shift() || 'help';

function parseArgs(items) {
  const out = { _: [] };
  for (let i = 0; i < items.length; i += 1) {
    const a = items[i];
    if (a.startsWith('--')) {
      const k = a.slice(2);
      const next = items[i + 1];
      if (next === undefined || next.startsWith('--')) out[k] = true;
      else { out[k] = next; i += 1; }
    } else out._.push(a);
  }
  return out;
}

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

function requestJson(url, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = http.request(url, { method }, res => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        if (res.statusCode < 200 || res.statusCode >= 300) return reject(new Error(`HTTP ${res.statusCode}: ${data.slice(0, 300)}`));
        try { resolve(data ? JSON.parse(data) : null); } catch { resolve(data); }
      });
    });
    req.on('error', reject);
    req.setTimeout(5000, () => req.destroy(new Error('timeout')));
    req.end();
  });
}

async function cdpRunning() {
  try { await requestJson(`${CDP_ENDPOINT}/json/version`); return true; }
  catch { return false; }
}

async function ensureChrome(url = 'about:blank') {
  if (await cdpRunning()) return;
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
  for (let i = 0; i < 40; i += 1) {
    await sleep(500);
    if (await cdpRunning()) return;
  }
  throw new Error('Browser CDP did not start');
}

async function getSession(opts = {}) {
  await ensureChrome(opts.url || 'about:blank');
  const browser = await chromium.connectOverCDP(CDP_ENDPOINT, { timeout: 7000 });
  const context = browser.contexts()[0] || await browser.newContext();
  let pages = context.pages().filter(p => !p.isClosed());
  let page = null;
  if (opts.match) page = pages.find(p => p.url().includes(opts.match));
  if (!page && opts.index !== undefined) page = pages[Number(opts.index)];
  if (!page) page = pages.find(p => p.url() !== 'about:blank') || pages[0];
  if (!page) page = await context.newPage();
  page.setDefaultTimeout(Number(process.env.PLAYWRIGHT_ACTION_TIMEOUT || 8000));
  return { browser, context, page };
}

function targetFromOpts(opts = {}) {
  return {
    role: opts.role || '',
    name: opts.name || '',
    text: opts.text || opts._?.join(' ') || '',
    selector: opts.selector || '',
    placeholder: opts.placeholder || '',
    label: opts.label || '',
    value: opts.value || '',
    scope: opts.scope || opts['scope-selector'] || '',
    index: opts.index
  };
}

async function visibleIndexes(locator) {
  const count = await locator.count();
  const out = [];
  for (let i = 0; i < count; i += 1) {
    const item = locator.nth(i);
    if (await item.isVisible().catch(() => false)) out.push(i);
  }
  return out;
}

async function elementInfo(locator) {
  return await locator.evaluate(el => {
    const r = el.getBoundingClientRect();
    const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').replace(/\s+/g, ' ').trim();
    const selector = (() => {
      if (el.id) return `#${CSS.escape(el.id)}`;
      const name = el.getAttribute('name');
      if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
      const aria = el.getAttribute('aria-label');
      if (aria) return `${el.tagName.toLowerCase()}[aria-label="${CSS.escape(aria)}"]`;
      const placeholder = el.getAttribute('placeholder');
      if (placeholder) return `${el.tagName.toLowerCase()}[placeholder="${CSS.escape(placeholder)}"]`;
      return el.tagName.toLowerCase();
    })();
    return { tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', text, selector, rect: { x:r.x, y:r.y, w:r.width, h:r.height }, disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true' };
  });
}

async function resolveLocator(page, target = {}, opts = {}) {
  const base = target.scope ? page.locator(target.scope).first() : page;
  const candidates = [];
  if (target.selector) candidates.push({ kind: 'selector', locator: base.locator ? base.locator(target.selector) : page.locator(target.selector) });
  if (target.role && target.name) candidates.push({ kind: 'role', locator: base.getByRole(target.role, { name: new RegExp(target.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i') }) });
  else if (target.role) candidates.push({ kind: 'role', locator: base.getByRole(target.role) });
  if (target.label) candidates.push({ kind: 'label', locator: base.getByLabel(target.label) });
  if (target.placeholder) candidates.push({ kind: 'placeholder', locator: base.getByPlaceholder(target.placeholder) });
  if (target.text) candidates.push({ kind: 'text', locator: base.getByText(target.text, { exact: false }) });
  if (!candidates.length && target.value) candidates.push({ kind: 'css', locator: page.locator(`[value="${target.value.replace(/"/g, '\\"')}"]`) });
  for (const candidate of candidates) {
    const indexes = await visibleIndexes(candidate.locator);
    if (indexes.length === 0) continue;
    if (indexes.length > 1 && !target.scope && target.index === undefined) return { ok:false, error:'ambiguous target', count:indexes.length, kind:candidate.kind };
    const nth = target.index !== undefined ? Number(target.index) : indexes[0];
    const locator = candidate.locator.nth(Number.isInteger(nth) ? nth : indexes[0]);
    const visible = await locator.isVisible().catch(() => false);
    const enabled = await locator.isEnabled().catch(() => true);
    if (!visible) return { ok:false, error:'element not visible', kind:candidate.kind };
    if (!enabled) return { ok:false, error:'element disabled', kind:candidate.kind, target: await elementInfo(locator).catch(() => null) };
    return { ok:true, locator, kind:candidate.kind, target: await elementInfo(locator).catch(() => null) };
  }
  return { ok:false, error:'element not found', target };
}

async function snapshot(page, limit = 300) {
  return await page.evaluate(max => {
    function cssPath(el) {
      if (el.id) return `#${CSS.escape(el.id)}`;
      const name = el.getAttribute('name');
      if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
      const aria = el.getAttribute('aria-label');
      if (aria) return `${el.tagName.toLowerCase()}[aria-label="${CSS.escape(aria)}"]`;
      const placeholder = el.getAttribute('placeholder');
      if (placeholder) return `${el.tagName.toLowerCase()}[placeholder="${CSS.escape(placeholder)}"]`;
      return el.tagName.toLowerCase();
    }
    const elements = [...document.querySelectorAll('a,button,input,textarea,select,[role],[contenteditable="true"]')]
      .filter(el => {
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
      })
      .slice(0, max)
      .map((el, index) => {
        const r = el.getBoundingClientRect();
        return { index, tag:el.tagName.toLowerCase(), type:el.getAttribute('type') || '', role:el.getAttribute('role') || '', text:(el.innerText || el.value || el.textContent || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').replace(/\s+/g,' ').trim().slice(0,240), selector:cssPath(el), x:r.x, y:r.y, w:r.width, h:r.height, disabled:!!el.disabled || el.getAttribute('aria-disabled') === 'true' };
      });
    return { title:document.title, url:location.href, scroll:{ x:scrollX, y:scrollY, totalH:document.documentElement.scrollHeight }, text:(document.body?.innerText || '').replace(/\s+/g,' ').trim().slice(0,12000), elements };
  }, Number(limit || 300));
}

async function inputs(page) {
  return await page.evaluate(() => [...document.querySelectorAll('input,textarea,select,[contenteditable="true"],[role="textbox"]')].map((el, index) => ({ index, tag:el.tagName.toLowerCase(), type:el.getAttribute('type') || '', role:el.getAttribute('role') || '', name:el.getAttribute('name') || '', label:el.getAttribute('aria-label') || el.getAttribute('placeholder') || '', placeholder:el.getAttribute('placeholder') || '', value:el.value || el.innerText || '', disabled:!!el.disabled || el.getAttribute('aria-disabled') === 'true' })));
}

async function forms(page) {
  return await page.evaluate(() => [...document.querySelectorAll('form')].map((form, formIndex) => ({ formIndex, text:(form.innerText || '').replace(/\s+/g,' ').slice(0,500), inputs:[...form.querySelectorAll('input,textarea,select,[contenteditable="true"],[role="textbox"]')].map((el, index) => ({ index, tag:el.tagName.toLowerCase(), type:el.getAttribute('type') || '', name:el.getAttribute('name') || '', label:el.getAttribute('aria-label') || el.getAttribute('placeholder') || '', value:el.value || el.innerText || '' })) })));
}

async function tables(page) {
  return await page.evaluate(() => [...document.querySelectorAll('table')].map((table, tableIndex) => ({ tableIndex, rows:[...table.querySelectorAll('tr')].map(tr => [...tr.children].map(td => td.innerText.replace(/\s+/g,' ').trim())) })));
}

async function accessibility(page, limit = 400) {
  return await page.evaluate(max => ({ title:'Accessibility approximation', nodes:[...document.querySelectorAll('a,button,input,textarea,select,[role],[aria-label],[placeholder],[contenteditable="true"]')].slice(0, max).map((el, index) => ({ index, role:el.getAttribute('role') || el.tagName.toLowerCase(), name:(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.innerText || el.value || '').replace(/\s+/g,' ').trim(), value:el.value || '', description:el.getAttribute('title') || '', ignored:false, props:{ disabled:!!el.disabled || el.getAttribute('aria-disabled') === 'true' } })) }), Number(limit || 400));
}

async function verify(page, opts) {
  const checks = [];
  if (opts.text) checks.push({ kind:'text', expected:opts.text, ok:(await page.locator('body').innerText()).toLowerCase().includes(String(opts.text).toLowerCase()) });
  if (opts.url) checks.push({ kind:'url', expected:opts.url, actual:page.url(), ok:new RegExp(opts.url, 'i').test(page.url()) || page.url().toLowerCase().includes(String(opts.url).toLowerCase()) });
  if (opts.selector) checks.push({ kind:'selector', expected:opts.selector, ok:await page.locator(opts.selector).first().isVisible().catch(() => false) });
  if (opts.value) {
    const activeValue = await page.evaluate(() => document.activeElement?.value || document.activeElement?.innerText || '').catch(() => '');
    checks.push({ kind:'value', expected:opts.value, actual:activeValue, ok:String(activeValue).includes(String(opts.value)) });
  }
  return { ok:checks.length ? checks.every(c => c.ok) : true, checks };
}

async function run() {
  const opts = parseArgs(args);
  if (cmd === 'help') {
    console.log('Playwright browser driver');
    return;
  }
  const startedUrl = opts.url || opts._?.[0] || 'about:blank';
  const { browser, context, page } = await getSession({ url: startedUrl, match: opts.match, index: opts.tabIndex });
  const target = targetFromOpts(opts);
  try {
    let result;
    if (cmd === 'start') result = { ok:true, driver:'playwright', port:PORT, profile:PROFILE };
    else if (cmd === 'goto') { await page.goto(startedUrl, { waitUntil:'domcontentloaded' }); await page.waitForLoadState('load').catch(() => {}); result = { ok:true, title:await page.title(), url:page.url() }; }
    else if (cmd === 'new-tab') { const p = await context.newPage(); await p.goto(startedUrl, { waitUntil:'domcontentloaded' }); result = { ok:true, url:p.url(), title:await p.title() }; }
    else if (cmd === 'tabs') result = context.pages().map((p, index) => ({ index, title:'', url:p.url() }));
    else if (cmd === 'url') result = { title:await page.title(), url:page.url() };
    else if (cmd === 'read') { const s = await snapshot(page, 50); result = { title:s.title, url:s.url, scroll:s.scroll, text:s.text }; }
    else if (cmd === 'snapshot') result = await snapshot(page, opts.limit || 300);
    else if (cmd === 'accessibility') result = await accessibility(page, opts.limit || 400);
    else if (cmd === 'inputs') result = await inputs(page);
    else if (cmd === 'forms') result = await forms(page);
    else if (cmd === 'tables') result = await tables(page);
    else if (cmd === 'html') result = await page.locator('html').evaluate((el, max) => el.outerHTML.slice(0, Number(max || 20000)), opts.max || 20000);
    else if (cmd === 'state') result = { ok:true, url:page.url(), title:await page.title(), readyState:await page.evaluate(() => document.readyState), loading:false, dialogs:[], consoleErrors:[], networkErrors:[] };
    else if (cmd === 'find') { const found = await resolveLocator(page, target, opts); result = found.ok ? { ok:true, ...found.target, selector:found.target?.selector || target.selector || '', text:found.target?.text || target.text || '', role:found.target?.role || target.role || '', resolver:found.kind } : found; }
    else if (cmd === 'click' || cmd === 'click-index') { if (cmd === 'click-index') target.index = opts._?.[0]; const found = await resolveLocator(page, target, opts); if (!found.ok) result = found; else { await found.locator.click(); await page.waitForLoadState('domcontentloaded').catch(() => {}); result = { ok:true, clicked:found.target, target:found.target }; } }
    else if (cmd === 'type' || cmd === 'set' || cmd === 'clear') { const found = await resolveLocator(page, target, opts); if (!found.ok) result = found; else { const value = cmd === 'clear' ? '' : (opts.value || opts._.join(' ')); await found.locator.fill(value).catch(async () => { await found.locator.click(); await page.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A'); await page.keyboard.insertText(value); }); const verification = await verify(page, { value }); result = { ok:verification.ok, action:cmd, typed:value, value, target:found.target, verification }; } }
    else if (cmd === 'act') { const action = String(opts.action || opts.cmd || opts._[0] || 'click').toLowerCase(); const found = await resolveLocator(page, target, opts); if (!found.ok) result = found; else if (action === 'click') { await found.locator.click(); await page.waitForLoadState('domcontentloaded').catch(() => {}); result = { ok:true, action, target:found.target, verification:await verify(page, { text:opts.expectText || opts['expect-text'], url:opts.expectUrl || opts['expect-url'], selector:opts.expectSelector || opts['expect-selector'], value:opts.expectValue || opts['expect-value'] }) }; } else if (action === 'type' || action === 'set' || action === 'clear') { const value = action === 'clear' ? '' : (opts.value || opts._.slice(1).join(' ')); await found.locator.fill(value).catch(async () => { await found.locator.click(); await page.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A'); await page.keyboard.insertText(value); }); result = { ok:true, action, target:found.target, verification:await verify(page, { value:opts.expectValue || opts['expect-value'] || value }) }; } else if (action === 'press' || action === 'key') { await found.locator.press(opts.key || opts.name || opts._.slice(1).join(' ') || 'Enter'); result = { ok:true, action, target:found.target, verification:await verify(page, { text:opts.expectText || opts['expect-text'], url:opts.expectUrl || opts['expect-url'] }) }; } else result = { ok:false, error:`unknown act action: ${action}` }; }
    else if (cmd === 'key' || cmd === 'enter' || cmd === 'tab-key' || cmd === 'escape') { const name = cmd === 'enter' ? 'Enter' : cmd === 'tab-key' ? 'Tab' : cmd === 'escape' ? 'Escape' : (opts.name || opts._[0] || 'Enter'); await page.keyboard.press(name); result = { ok:true, key:name }; }
    else if (cmd === 'wait-for-text') { await page.getByText(opts.text || opts._.join(' '), { exact:false }).first().waitFor({ state:'visible', timeout:Number(opts.timeout || 15000) }); result = { ok:true, reason:'text' }; }
    else if (cmd === 'wait-for-selector') { await page.locator(opts.selector || opts._[0]).first().waitFor({ state:'visible', timeout:Number(opts.timeout || 15000) }); result = { ok:true, reason:'selector' }; }
    else if (cmd === 'verify') result = await verify(page, opts);
    else if (cmd === 'back') { await page.goBack({ waitUntil:'domcontentloaded' }).catch(() => null); result = { ok:true, title:await page.title(), url:page.url() }; }
    else if (cmd === 'forward') { await page.goForward({ waitUntil:'domcontentloaded' }).catch(() => null); result = { ok:true, title:await page.title(), url:page.url() }; }
    else if (cmd === 'reload') { await page.reload({ waitUntil:'domcontentloaded' }); result = { ok:true, title:await page.title(), url:page.url() }; }
    else if (cmd === 'scroll') { await page.mouse.wheel(0, Number(opts.y || 700)); result = { ok:true }; }
    else if (cmd === 'scroll-top') { await page.evaluate(() => scrollTo(0, 0)); result = { ok:true }; }
    else if (cmd === 'scroll-bottom') { await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight)); result = { ok:true }; }
    else if (cmd === 'scroll-to-text') { await page.getByText(opts.text || opts._.join(' '), { exact:false }).first().scrollIntoViewIfNeeded(); result = { ok:true }; }
    else throw new Error('Unknown command: ' + cmd);
    console.log(JSON.stringify(result, null, 2));
  } finally {
    await browser.close().catch(() => {});
  }
}

run().catch(error => {
  console.log(JSON.stringify({ ok:false, driver:'playwright', error:String(error.message || error) }, null, 2));
  process.exitCode = 1;
});
