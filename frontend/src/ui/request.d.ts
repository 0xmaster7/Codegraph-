export function requestJson<T>(url: string, init?: RequestInit): Promise<T>;
export class RequestScope { begin(): { signal: AbortSignal; current: () => boolean }; cancel(): void; }
