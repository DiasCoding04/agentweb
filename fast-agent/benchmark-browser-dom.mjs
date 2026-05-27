#!/usr/bin/env node
import { execFile } from 'node:child_process';

const tool = new URL('./browser-dom.mjs', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');

function run(args) {
  return new Promise((resolve, reject) => {
    execFile('node.exe', [tool, ...args], { timeout: 20000, maxBuffer: 2_000_000 }, (error, stdout, stderr) => {
      if (error) return reject(new Error(`${args.join(' ')} failed: ${error.message}\n${stderr}`));
      try {
        resolve(JSON.parse(stdout));
      } catch {
        reject(new Error(`${args.join(' ')} returned non-JSON:\n${stdout}\n${stderr}`));
      }
    });
  });
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const html = `<!doctype html>
<html>
<head><title>Browser DOM Benchmark</title></head>
<body>
  <main id="target">
    <button id="direct">Direct Action</button>
    <button class="dupe">Duplicate</button>
    <button class="dupe">Duplicate</button>
    <div style="position:relative;width:160px;height:40px">
      <button id="covered-action">Covered Action</button>
      <div id="cover" style="position:absolute;inset:0;background:rgba(255,255,255,.01)"></div>
    </div>
    <div id="editor" contenteditable="true" role="textbox" aria-label="Message editor"></div>
    <form><input name="email" placeholder="Email address"><button>Submit form</button></form>
    <div id="shadow-host"></div>
    <iframe id="same-frame" srcdoc="<button id='frame-action'>Frame Action</button><input placeholder='Frame input'>"></iframe>
  </main>
  <script>
    const root = document.querySelector('#shadow-host').attachShadow({ mode: 'open' });
    root.innerHTML = '<button id="shadow-action">Shadow Action</button><input placeholder="Shadow input">';
  </script>
  <div role="dialog" id="popup" style="display:none"></div>
</body>
</html>`;

const url = `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;

await run(['goto', url]);

const scoped = await run(['elements', '--selector', '#target']);
assert(scoped.some(e => e.text.includes('Direct Action')), 'elements --selector should include scoped children');
assert(scoped.every(e => e.x >= 0), 'scoped elements should have viewport coordinates');

const shadow = await run(['find', '--text', 'Shadow Action']);
assert(shadow.ok && shadow.text.includes('Shadow Action'), 'find should traverse open shadow roots');

const frame = await run(['find', '--text', 'Frame Action']);
assert(frame.ok && frame.text.includes('Frame Action'), 'find should traverse same-origin/srcdoc iframes');
assert(frame.rect.x > 0 && frame.rect.y > 0, 'iframe element coordinates should be viewport-relative');

const forms = await run(['forms']);
assert(Array.isArray(forms.looseFields) && forms.looseFields.some(f => f.text.includes('Message editor')), 'forms should include loose contenteditable fields');
assert(forms.looseFields.some(f => f.text.includes('Shadow input') || f.placeholder === 'Shadow input'), 'forms should include shadow fields');

const ax = await run(['accessibility', '--limit', '100']);
assert(Array.isArray(ax.nodes) && ax.nodes.some(n => n.name === 'Direct Action'), 'accessibility should return actionable names');

const roleClick = await run(['find', '--role', 'button', '--name', 'Direct Action']);
assert(roleClick.ok && roleClick.text.includes('Direct Action'), 'semantic role/name locator should resolve targets');

const ambiguous = await run(['find', '--role', 'button', '--name', 'Duplicate']);
assert(!ambiguous.ok && /ambiguous/i.test(ambiguous.error), 'multiple equal buttons should return ambiguity error');

const covered = await run(['act', '--action', 'click', '--role', 'button', '--name', 'Covered Action', '--expect-text', 'Covered Action']);
assert(!covered.ok && /occluded|clickable/i.test(covered.error || ''), 'covered target should not be clicked blindly');

const verify = await run(['verify', '--text', 'Direct Action']);
assert(verify.ok, 'verify should check page state deterministically');

const acted = await run(['act', '--action', 'set', '--role', 'textbox', '--name', 'Message editor', '--value', 'hello', '--expect-value', 'hello']);
assert(acted.ok && acted.verification?.ok && acted.verification.checks?.length > 0, 'act should execute and verify in one call');

const duplicateSafe = await run(['act', '--action', 'type', '--role', 'textbox', '--name', 'Message editor', '--value', 'hello', '--expect-value', 'hello']);
assert(duplicateSafe.ok && duplicateSafe.skipped, 'retry type should not duplicate existing payload');

const state = await run(['state']);
assert(state.ok && typeof state.readyState === 'string', 'state should expose deterministic page state');

console.log(JSON.stringify({ ok: true, checks: 14 }, null, 2));
