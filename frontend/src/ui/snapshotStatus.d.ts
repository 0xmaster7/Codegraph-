export function withSnapshotStatus<T extends { status: string }>(
  analysis: T,
  snapshot: { status?: unknown } | null | undefined,
): T;
export function isSnapshotInProgress(status: string): boolean;
export function snapshotProgressMessage(status: string): string;
