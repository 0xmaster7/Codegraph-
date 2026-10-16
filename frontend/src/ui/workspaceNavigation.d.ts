export type WorkspacePage = "investigate" | "graph" | "changes" | "history";
export const workspaceNavigation: ReadonlyArray<{ id: WorkspacePage; label: string }>;
export function workspaceTitle(page: string): string;
export function canOpenWorkspace(page: WorkspacePage, hasSnapshot: boolean): boolean;
export function canUseValidatedSnapshot(status: string): boolean;
