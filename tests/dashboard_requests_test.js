'use strict';
const assert = require('node:assert/strict');
global.window = {};
require('../static/dashboard-requests.js');
const requests = window.DashboardRequests;
const aborted = () => Object.assign(new Error('aborted'), { name: 'AbortError' });
const hanging = signal => new Promise((resolve, reject) => {
  if (signal.aborted) reject(aborted());
  else signal.addEventListener('abort', () => reject(aborted()), { once: true });
});

(async () => {
  let calls = 0;
  global.fetch = async () => {
    calls++;
    return { ok: true, json: async () => ({ total: 45 }) };
  };
  const jobs = requests.begin('jobs', 1000);
  assert.equal(requests.begin('jobs', 1000), null);
  const metrics = requests.begin('metrics', 1000);
  assert.ok(metrics);
  assert.deepEqual(await jobs.json('/jobs'), { total: 45 });
  assert.equal(calls, 1);
  assert.equal(jobs.timedOut, false);
  jobs.finish();
  const retry = requests.begin('jobs', 1000);
  assert.ok(retry);
  jobs.finish(); // Idempotent cleanup cannot unlock the newer request.
  assert.equal(requests.begin('jobs', 1000), null);
  retry.finish();
  metrics.finish();

  global.fetch = (url, { signal }) => hanging(signal);
  const timeout = requests.begin('jobs', 20);
  await assert.rejects(timeout.json('/jobs'), { name: 'AbortError' });
  assert.equal(timeout.timedOut, true);
  timeout.finish();
  const afterTimeout = requests.begin('jobs', 1000);
  assert.ok(afterTimeout);
  afterTimeout.finish();

  global.fetch = async (url, { signal }) => ({ ok: true, json: () => hanging(signal) });
  const body = requests.begin('jobs', 20);
  await assert.rejects(body.json('/jobs'), { name: 'AbortError' });
  assert.equal(body.timedOut, true);
  body.finish();

  let peerAborted = false;
  global.fetch = async (url, { signal }) => {
    if (url === '/health') return { ok: false, text: async () => 'HTTP 503' };
    signal.addEventListener('abort', () => { peerAborted = true; }, { once: true });
    return { ok: true, json: () => hanging(signal) };
  };
  const failed = requests.begin('metrics', 1000);
  try {
    await assert.rejects(Promise.all([failed.json('/metrics'), failed.json('/health')]), /HTTP 503/);
    assert.equal(failed.timedOut, false);
  } finally {
    failed.finish();
  }
  assert.equal(peerAborted, true);
  const recovered = requests.begin('metrics', 1000);
  global.fetch = async () => ({ ok: true, json: async () => { throw new SyntaxError('invalid JSON'); } });
  await assert.rejects(recovered.json('/metrics'), /invalid JSON/);
  recovered.finish();
  const final = requests.begin('metrics', 1000);
  global.fetch = async () => ({ ok: true, json: async () => ({ meetings: 44 }) });
  assert.deepEqual(await final.json('/metrics'), { meetings: 44 });
  final.finish();
  console.log('dashboard requests OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
