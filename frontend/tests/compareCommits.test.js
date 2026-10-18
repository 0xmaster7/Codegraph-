import test from 'node:test';
import assert from 'node:assert/strict';
import { compareCommits } from '../src/ui/compareCommits.js';
const id = 'diff_' + 'a'.repeat(32);

test('long comparisons use short polling requests and retain the original job identity', async () => {
  const calls = [], states = [];
  const replies = [{job_id:id,status:'QUEUED'},{job_id:id,status:'RUNNING'},{job_id:id,status:'COMPLETED',result:{changed_symbols:['evidence']}}];
  const result = await compareCommits('', {base_sha:'base'}, {
    transport: async (url, init) => { calls.push({url,method:init.method??'GET'}); return replies.shift(); },
    wait: async()=>{}, onStatus: status=>states.push(status),
  });
  assert.deepEqual(result.changed_symbols,['evidence']);
  assert.deepEqual(states,['QUEUED','RUNNING','COMPLETED']);
  assert.deepEqual(calls.map(x=>x.method),['POST','GET','GET']);
  assert.equal(calls[1].url,'/api/diff/jobs/'+id);
});

test('failed, mismatched and expired jobs cannot supply comparison results', async () => {
  for (const reply of [
    {job_id:id,status:'FAILED',error:'Cannot compare commits'},
    {job_id:'diff_'+'b'.repeat(32),status:'COMPLETED',result:{}},
    {job_id:id,status:'COMPLETED'},
  ]) {
    let first=true;
    await assert.rejects(compareCommits('',{}, {transport:async()=>first?(first=false,{job_id:id,status:'QUEUED'}):reply,wait:async()=>{}}));
  }
  await assert.rejects(compareCommits('',{}, {transport:async()=>({job_id:id,status:'RUNNING'}),wait:async()=>{},maxWaitMs:0}),/longer than expected/);
});

test('leaving a workspace aborts polling without submitting a second comparison', async () => {
  const controller=new AbortController();let requests=0;
  await assert.rejects(compareCommits('',{}, {
    signal:controller.signal,
    transport:async()=>{requests++;return {job_id:id,status:'RUNNING'};},
    wait:async()=>{controller.abort();controller.signal.throwIfAborted();},
  }), {name:'AbortError'});
  assert.equal(requests,1);
});
