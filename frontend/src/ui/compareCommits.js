import { requestJson } from './request.js';

function pause(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(signal.reason ?? new DOMException('Aborted', 'AbortError')); };
    const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, milliseconds);
    if (signal?.aborted) abort();
    else signal?.addEventListener('abort', abort, { once: true });
  });
}

export async function compareCommits(api, payload, { signal, onStatus, transport = requestJson, wait = pause, now = Date.now, maxWaitMs = 900000 } = {}) {
  let job = await transport(`${api}/api/diff/jobs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), signal,
  });
  const id = job.job_id;
  if (!/^diff_[a-f0-9]{32}$/.test(id ?? '')) throw new Error('The server returned an invalid comparison job. Please retry.');
  const deadline = now() + maxWaitMs;
  while (true) {
    signal?.throwIfAborted();
    if (job.job_id !== id) throw new Error('Comparison identity changed. Please retry.');
    onStatus?.(job.status);
    if (job.status === 'COMPLETED') {
      if (!job.result) throw new Error('The completed comparison has no result. Please retry.');
      return job.result;
    }
    if (job.status === 'FAILED') throw new Error(job.error || 'Comparison failed. Please retry.');
    if (!['QUEUED', 'RUNNING'].includes(job.status)) throw new Error('Unknown comparison state. Please retry.');
    if (now() >= deadline) throw new Error('Comparison is taking longer than expected. It may still be running; retry shortly.');
    await wait(1500, signal);
    job = await transport(`${api}/api/diff/jobs/${encodeURIComponent(id)}`, { signal });
  }
}
