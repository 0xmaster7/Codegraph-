// Concurrent GETs share transport only. Results are never cached across evidence changes.
const pending = new Map();
export async function requestJson(url, init = {}) {
  const method = init.method ?? 'GET';
  const key = method === 'GET' && !init.signal ? url : null;
  if (key && pending.has(key)) return pending.get(key);
  const perform = async () => {
    const timeout = AbortSignal.timeout(method === 'GET' ? 30000 : 180000);
    const signal = init.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
    try {
      const response = await fetch(url, { ...init, signal });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = data?.detail;
        const message = typeof detail === 'string' ? detail : detail?.message ?? `Request failed (${response.status}). Please try again.`;
        throw new Error(message.includes('No such file or directory') ? 'Repository path was not found on the server. Check the path and try again.' : message);
      }
      if (data === null) throw new Error('The server returned an unreadable response. Please try again.');
      return data;
    } catch (error) {
      if (timeout.aborted) throw new Error('The request timed out. Check the service and try again. Server-side analysis may still be running.');
      if (error instanceof TypeError) throw new Error('Cannot reach CodeGraph. Check the API connection, then retry.');
      throw error;
    }
  };
  const promise = perform();
  if (key) pending.set(key, promise);
  try { return await promise; } finally { if (key && pending.get(key) === promise) pending.delete(key); }
}

// The latest context alone owns writes and loading cleanup, including A → B → A.
export class RequestScope {
  generation = 0;
  controller = null;
  begin() {
    this.controller?.abort();
    const generation = ++this.generation;
    const controller = new AbortController();
    this.controller = controller;
    return { signal: controller.signal, current: () => generation === this.generation && !controller.signal.aborted };
  }
  cancel() { this.controller?.abort(); this.generation++; }
}
