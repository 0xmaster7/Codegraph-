const IN_PROGRESS_STATUSES = new Set([
  "DISCOVERED",
  "MATERIALIZING",
  "PARSING",
  "SEMANTIC_INDEXING",
  "VALIDATING",
  "PUBLISHING",
]);

export function withSnapshotStatus(analysis, snapshot) {
  const status = typeof snapshot?.status === "string" ? snapshot.status : analysis.status;
  return { ...analysis, status };
}

export function isSnapshotInProgress(status) {
  return IN_PROGRESS_STATUSES.has(status.toUpperCase());
}

export function snapshotProgressMessage(status) {
  switch (status.toUpperCase()) {
    case "DISCOVERED": return "This repository is queued for analysis.";
    case "MATERIALIZING": return "Preparing a source-bound copy of the repository.";
    case "PARSING": return "Reading Python files and building code relationships.";
    case "SEMANTIC_INDEXING": return "Indexing code symbols and their connections.";
    case "PUBLISHING": return "Saving the completed snapshot.";
    case "VALIDATING": return "Checking that the saved graph matches its source.";
    default: return "Preparing this repository snapshot.";
  }
}
