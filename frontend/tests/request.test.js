import test from 'node:test';
import assert from 'node:assert/strict';
import { requestJson, RequestScope } from '../src/ui/request.js';

test('latest request owns writes and cleanup across A → B → A', () => {
  const scope = new RequestScope();
  const first = scope.begin(); const second = scope.begin(); const third = scope.begin();
  assert.equal(first.signal.aborted, true);
  assert.equal(first.current(), false); assert.equal(second.current(), false); assert.equal(third.current(), true);
  scope.cancel(); assert.equal(third.current(), false);
});

test('concurrent GETs deduplicate, later GET revalidates, snapshot URLs stay separate', async () => {
  const original = globalThis.fetch; let calls = 0; let release;
  globalThis.fetch = async () => { calls++; await new Promise(resolve => { release = resolve; }); return Response.json({ version: calls }); };
  try {
    const first = requestJson('/snapshot/A'); const second = requestJson('/snapshot/A');
    assert.equal(calls, 1); release(); assert.deepEqual(await first, await second);
    const next = requestJson('/snapshot/A'); assert.equal(calls, 2); release(); await next;
    const other = requestJson('/snapshot/B'); assert.equal(calls, 3); release(); await other;
  } finally { globalThis.fetch = original; }
});

test('failed requests clear pending state and can be retried', async () => {
  const original = globalThis.fetch; let calls = 0;
  globalThis.fetch = async () => ++calls === 1 ? Response.json({ detail: 'Database unavailable' }, { status: 503 }) : Response.json({ ok: true });
  try { await assert.rejects(requestJson('/recover'), /Database unavailable/); assert.deepEqual(await requestJson('/recover'), { ok: true }); }
  finally { globalThis.fetch = original; }
});
