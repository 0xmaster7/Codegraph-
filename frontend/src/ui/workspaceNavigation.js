export const workspaceNavigation = [
  { id: "investigate", label: "Investigate" },
  { id: "graph", label: "Explore graph" },
  { id: "changes", label: "Changes & tests" },
  { id: "history", label: "History" },
];

export function workspaceTitle(page) {
  return workspaceNavigation.find((item) => item.id === page)?.label ?? "Investigate";
}

export function canOpenWorkspace(page, hasSnapshot) {
  return page === "investigate" || page === "history" || hasSnapshot;
}

export function canUseValidatedSnapshot(status) {
  return status === "READY";
}
