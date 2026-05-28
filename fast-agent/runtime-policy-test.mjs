#!/usr/bin/env node
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const {
  ACQUISITION_STATES,
  actionFromTool,
  assertContextStillValid,
  classifyTargetContext,
  createOrUpdateContextLock,
  createTargetCandidate,
  createTaskState,
  ensureTargetLocked,
  enrichCandidate,
  finalStatusFromVerification,
  inferTaskIntent,
  injectDefaultExpectedResult,
  parseUserTask,
  requiresLockedContext,
  setAcquisitionState,
  verifyActiveContext,
  validateAction
} = require('./runtime-core.js');

function observation(url, title, text) {
  return {
    url: { url, title },
    snapshot: { url, title, text },
    accessibility: { nodes: [{ role:'heading', name:title }, { role:'text', name:text }] }
  };
}

{
  const task = createTaskState('send "hello" to Customer A');
  const action = actionFromTool('act', ['--action','type','--role','textbox','--name','Message','--value','bye','--expect-value','bye']);
  const validation = validateAction(action, task);
  assert.equal(validation.ok, false, 'Model proposed payload different from original message must be rejected');
}

{
  const task = createTaskState('vào mess trên facebook và nhắn tin cho bé tôm iu chồng nhất: tin nhắn tới từ phim khoa học viễn tưởng');
  assert.equal(task.invariants.targetEntity, 'bé tôm iu chồng nhất', 'target parser must not swallow message payload');
}

{
  const task = createTaskState('open record Customer A and update note "hello"');
  createOrUpdateContextLock(task, observation('https://app.test/customer-a', 'Customer A', 'conversation Customer A composer'));
  const check = assertContextStillValid(task, observation('https://app.test/customer-b', 'Customer B', 'Customer B profile'));
  assert.equal(check.ok, false, 'Unexpected URL/entity context change must stop task');
  assert.equal(check.status, 'CONTEXT_CHANGED');
}

{
  const task = createTaskState('send "hello" to Customer A');
  const action = actionFromTool('act', ['--action','click','--role','button','--name','Send','--expect-text','hello','--confirm']);
  const status = finalStatusFromVerification(action, { ok:false, verification:{ ok:false } }, { ok:true });
  assert.equal(status, 'FAILED_VERIFICATION', 'Tool ok:false or verifier fail must not be success');
}

{
  const task = createTaskState('delete record Customer A');
  const action = actionFromTool('act', ['--action','click','--role','button','--name','Delete','--expect-text','Deleted']);
  const validation = validateAction(action, task);
  assert.equal(validation.ok, false, 'High risk side effect without human confirmation must be rejected');
}

{
  const task = createTaskState('send "hello" to Customer A');
  const action = actionFromTool('act', ['--action','click','--scope','sidebar','--role','button','--name','Send','--expect-text','Sent','--confirm']);
  const validation = validateAction(action, task);
  assert.equal(validation.ok, false, 'Send/submit action inside sidebar/navigation scope must be blocked');
}

{
  const task = createTaskState('send "hello" to Customer A');
  const action = actionFromTool('act', ['--action','click','--role','button','--name','Send','--confirm']);
  const validation = validateAction(action, task);
  assert.equal(validation.ok, false, 'Side effect without expected result must be rejected');
}

{
  const task = createTaskState('send "hello" to Customer A');
  const action = actionFromTool('act', ['--action','type','--role','textbox','--name','Message','--value','hello','--expect-value','hello']);
  const status = finalStatusFromVerification(action, { ok:true, verification:{ ok:true, checks:[{ kind:'value', ok:true }] } }, { ok:true });
  assert.equal(status, 'SUCCESS', 'Draft/input verification is success for the action but not proof of send unless committed criteria is verified by later send action');
  assert.equal(task.invariants.successCriteria.some(c => c.kind === 'committed'), true, 'Send task includes committed success criteria');
}

{
  const task = createTaskState('submit form for Customer A');
  const action = actionFromTool('act', ['--action','click','--role','button','--name','Submit','--expect-text','Saved','--confirm']);
  const status = finalStatusFromVerification(action, { ok:false, error:'network error 500', verification:{ ok:false } }, { ok:true });
  assert.equal(status, 'FAILED_VERIFICATION', 'Network/server failure must not become success');
}

{
  const task = createTaskState('open Customer A');
  const action = actionFromTool('act', ['--action','click','--role','link','--name','Customer B','--expect-text','Customer B']);
  const validation = validateAction(action, task);
  assert.equal(validation.ok, true, 'Navigate intent should not require immutable target lock semantics');
}

{
  const task = createTaskState('send "đang làm gì z" to bé tôm iu chồng nhất');
  const findResult = { ok:true, text:'bé tôm iu chồng nhất', selector:'[role="listitem"]', role:'link', rect:{ x:1, y:2, w:120, h:28 } };
  const candidate = createTargetCandidate(task.invariants.targetEntity, findResult);
  task.acquisition.candidates = [candidate];
  task.acquisition.selectedCandidate = candidate;
  setAcquisitionState(task, ACQUISITION_STATES.TARGET_CANDIDATE_FOUND);
  assert.equal(task.acquisition.state, ACQUISITION_STATES.TARGET_CANDIDATE_FOUND, 'find ok:true must create target candidate state');
}

{
  const task = createTaskState('send "hello" to Customer A');
  const action = actionFromTool('act', ['--action','type','--role','textbox','--name','Message','--value','hello','--expect-value','hello']);
  const validation = validateAction(action, task);
  assert.equal(validation.ok, false, 'side-effect type before TARGET_LOCKED must be blocked');
}

{
  const task = createTaskState('send "hello" to Customer A');
  const candidate = createTargetCandidate('Customer A', { ok:true, text:'Customer A', selector:'[role="row"]', role:'link', rect:{x:0,y:0,w:100,h:20} });
  task.acquisition.candidates = [candidate];
  task.acquisition.selectedCandidate = candidate;
  const obs = observation('https://app.test/customer-a', 'Customer A', 'Message composer for Customer A');
  createOrUpdateContextLock(task, obs);
  const action = actionFromTool('act', ['--action','type','--role','textbox','--name','Message','--value','hello','--expect-value','hello']);
  const validation = validateAction(action, task);
  assert.equal(validation.ok, true, 'side-effect after TARGET_LOCKED with immutable payload should pass');
}

{
  const task = createTaskState('send "đang làm gì z" to bé tôm iu chồng nhất');
  const action = actionFromTool('act', ['--action','click','--role','button','--name','Send']);
  injectDefaultExpectedResult(action, task);
  assert.equal(Boolean(action.expected?.policy?.type === 'message_sent'), true, 'missing expectedResult for send must be auto-injected');
}

{
  const task = createTaskState('send "hello" to Customer A');
  const action = actionFromTool('act', ['--action','type','--role','textbox','--name','composer','--value','hello']);
  injectDefaultExpectedResult(action, task);
  const validation = validateAction(action, task);
  assert.equal(validation.ok, false, 'before context lock, send/type must remain blocked');
}

{
  const task = createTaskState('vào youtube mở nhạc hà anh tuấn');
  assert.equal(inferTaskIntent(task), 'youtube_search', 'youtube search request should be parsed before DOM targeting');
  assert.equal(requiresLockedContext(task), false, 'youtube search must not require TARGET_LOCKED context');
  assert.equal(task.invariants.targetEntity, '', 'youtube search must not use raw request as targetText');
  assert.equal(task.invariants.intendedPayload, 'nhạc hà anh tuấn', 'youtube search value must be parsed query');
}

{
  const task = createTaskState('mở 1 bài của bùi anh tuấn trên youtube');
  const action = actionFromTool('type', ['--name','search_query','--value','bùi anh tuấn']);
  const validation = validateAction(action, task);
  assert.equal(validation.ok, true, 'youtube search typing should not require expected-result side-effect policy');
}

{
  const parsed = parseUserTask('vào youtube');
  assert.equal(parsed.intent, 'open_website');
  assert.equal(parsed.app, 'youtube');
  const task = createTaskState('vào youtube');
  assert.equal(requiresLockedContext(task), false, 'open website must not require context lock');
  assert.equal(task.invariants.targetEntity, '', 'open website must not create targetText from raw request');
}

{
  const parsed = parseUserTask('mở youtube tìm sơn tùng mtp');
  assert.equal(parsed.intent, 'youtube_search');
  assert.equal(parsed.query, 'sơn tùng mtp');
}

{
  const parsed = parseUserTask('vào mess trên facebook và nhắn tin cho bé tôm iu chồng nhất: tin nhắn tới từ phim khoa học viễn tưởng');
  assert.equal(parsed.intent, 'send_message');
  assert.equal(parsed.target, 'bé tôm iu chồng nhất');
  assert.equal(parsed.searchTarget, 'bé tôm iu chồng nhất');
  assert.equal(parsed.payload, 'tin nhắn tới từ phim khoa học viễn tưởng');
  assert.equal(parsed.target.includes(parsed.payload), false, 'send target must not contain payload');
  assert.equal(parsed.target.includes(':'), false, 'send target must not contain colon');
  const task = createTaskState('vào mess trên facebook và nhắn tin cho bé tôm iu chồng nhất: tin nhắn tới từ phim khoa học viễn tưởng');
  assert.equal(task.invariants.targetEntity, 'bé tôm iu chồng nhất', 'send_message must not use raw request as target');
  assert.equal(task.invariants.intendedPayload, 'tin nhắn tới từ phim khoa học viễn tưởng');
  assert.equal(requiresLockedContext(task), true, 'send_message must require context lock');
}

{
  const listObs = observation('https://messenger.com/t', 'Messenger', 'bé tôm iu chồng nhất Tin nhắn chưa đọc 5 phút');
  const ctx = classifyTargetContext(listObs, 'bé tôm iu chồng nhất');
  assert.equal(ctx.targetLikelyInList, true, 'target visible with unread/timestamp should be classified as list-only');
  assert.equal(ctx.targetActive, false, 'list-only visibility must not be treated as active lock');
}

{
  const activeObs = observation('https://messenger.com/t/123', 'Messenger', 'Conversation với bé tôm iu chồng nhất composer message box');
  const ctx = classifyTargetContext(activeObs, 'bé tôm iu chồng nhất');
  assert.equal(ctx.targetActive, true, 'target in active/header/composer region should be lockable');
}

{
  const task = createTaskState('send "hello" to Customer A');
  const candidate = createTargetCandidate('Customer A', { ok:true, text:'Customer A', selector:'', role:'', rect:null });
  const obs = { snapshot: { elements:[{ text:'Customer A', selector:'[role="listitem"]', role:'link', x:5, y:6, w:100, h:20 }] } };
  const enriched = enrichCandidate(candidate, obs);
  assert.equal(Boolean(enriched.selector), true, 'enrichCandidate should deterministically fill missing selector/container data');
}

{
  const task = createTaskState('send "đang làm gì z" to bé tôm iu chồng nhất');
  const listObs = observation('https://messenger.com/e2ee', 'Messenger', 'bé tôm iu chồng nhất chưa đọc 1 phút người khác');
  const ensure = ensureTargetLocked(task, listObs);
  assert.equal(ensure.ok, false, 'visible candidate without active evidence should not lock');
  assert.equal(ensure.status, 'REQUIRES_ACQUIRE_TARGET', 'list/sidebar candidate should require open/acquire step');
}

{
  const task = createTaskState('send "đang làm gì z" to bé tôm iu chồng nhất');
  const activeObs = observation('https://messenger.com/e2ee/t/157695', 'Messenger', 'conversation bé tôm iu chồng nhất composer nhập tin nhắn');
  const ensure = ensureTargetLocked(task, activeObs);
  assert.equal(ensure.ok, true, 'active context evidence should lock');
  assert.equal(Boolean(task.contextLock?.locked), true, 'ensureTargetLocked should create lock');
}

{
  const task = createTaskState('send "đang làm gì z" to bé tôm iu chồng nhất');
  const obs = observation('https://www.messenger.com/e2ee/t/1576952440056609/', 'Messenger', 'Đoạn chat bé tôm iu chồng nhất Tin nhắn chưa đọc · 1 phút Chuyển đến phần soạn');
  const ensure = ensureTargetLocked(task, obs);
  assert.equal(ensure.ok, true, 'thread URL + composer anchor should be sufficient active-context evidence');
}

{
  const task = createTaskState('send "robot test" to bé tôm iu chồng nhất');
  task.acquisition.selectedCandidate = createTargetCandidate('bé tôm iu chồng nhất', { ok:true, text:'bé tôm iu chồng nhất', selector:'div > a', role:'link', rect:{x:10,y:10,w:120,h:20} });
  const obs = observation('https://www.messenger.com/e2ee/t/1193963669482316/', 'Messenger', 'Đang mở đoạn chat Chuyển đến phần soạn Đoạn chat · 3 tin nhắn chưa đọc');
  const ensure = ensureTargetLocked(task, obs);
  assert.equal(ensure.ok, true, 'active anchors with thread URL should lock context after acquire');
}

{
  const task = createTaskState('vào messenger, vào hội thoại với bé tôm iu chồng nhất, gửi robot test');
  task.invariants.targetEntity = 'messenger';
  task.acquisition.selectedCandidate = createTargetCandidate('bé tôm iu chồng nhất', { ok:true, text:'bé tôm iu chồng nhất', selector:'div > a', role:'link', rect:{x:10,y:10,w:120,h:20} });
  const obs = observation('https://www.messenger.com/e2ee/t/1193963669482316/', 'bé tôm iu chồng nhất | Messenger', 'Đang mở đoạn chat Chuyển đến phần soạn');
  const ensure = ensureTargetLocked(task, obs);
  assert.equal(ensure.ok, true, 'candidate target should override malformed invariant target when locking context');
}

{
  const task = createTaskState('send "robot test" to bé tôm iu chồng nhất');
  task.acquisition.selectedCandidate = createTargetCandidate('bé tôm iu chồng nhất', { ok:true, text:'bé tôm iu chồng nhất', selector:'div > a', role:'link', rect:{x:10,y:10,w:120,h:20} });
  task.invariants.targetHints = ['nonexistent target hint'];
  const obs = observation('https://www.messenger.com/e2ee/t/1193963669482316/', 'bé tôm iu chồng nhất | Messenger', 'Đang mở đoạn chat Chuyển đến phần soạn');
  const ensure = ensureTargetLocked(task, obs);
  assert.equal(ensure.ok, true, 'fallback force-lock should work with strong active-context evidence');
}

{
  const task = createTaskState('send "đang làm gì z" to bé tôm iu chồng nhất');
  task.acquisition.selectedCandidate = createTargetCandidate('bé tôm iu chồng nhất', { ok:true, text:'bé tôm iu chồng nhất đã xem lúc 20:29', selector:'div > span > img', role:'img', rect:{x:1,y:1,w:14,h:14} });
  const obs = observation('https://www.messenger.com/e2ee/t/1576952440056609/', 'Messenger', 'Chuyển đến phần soạn Đoạn chat');
  const ensure = ensureTargetLocked(task, obs);
  assert.equal(ensure.ok, true, 'active thread with composer should still lock even if candidate is status badge');
}

{
  const task = createTaskState('send "đang làm gì z" to bé tôm iu chồng nhất');
  const obs = observation('https://messenger.com/e2ee/t/157695', 'Messenger', 'Messenger generic page no input area no active header');
  const verify = verifyActiveContext(task, obs, createTargetCandidate('bé tôm iu chồng nhất', { ok:true, text:'bé tôm iu chồng nhất' }));
  assert.equal(verify.ok, false, 'URL and generic title alone are insufficient for lock');
}

{
  const task = createTaskState('send "robot test" to bé tôm iu chồng nhất');
  task.acquisition.selectedCandidate = createTargetCandidate('bé tôm iu chồng nhất', { ok:true, text:'bé tôm iu chồng nhất', selector:'div > a', role:'link', rect:{x:10,y:10,w:120,h:20} });
  const obs = observation('https://www.messenger.com/e2ee/t/1193963669482316/', 'bé tôm iu chồng nhất | Messenger', 'Messenger generic text without explicit composer anchor');
  const ensure = ensureTargetLocked(task, obs);
  assert.equal(ensure.ok, true, 'title+thread fallback should lock when thread title matches candidate target');
}

console.log(JSON.stringify({ ok:true, checks:41 }, null, 2));
