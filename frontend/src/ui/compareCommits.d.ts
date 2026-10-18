export function compareCommits<T>(api: string, payload: unknown, options?: { signal?: AbortSignal; onStatus?: (status: string) => void }): Promise<T>;
