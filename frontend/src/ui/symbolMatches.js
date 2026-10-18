export function investigationSymbolMatches(executions) {
  const matches = new Map();
  for (const execution of executions) {
    if (execution.result.tool !== "resolve_symbol") continue;
    for (const record of execution.result.results) {
      if (typeof record.id !== "string" || typeof record.qualified_name !== "string" || typeof record.file_path !== "string") continue;
      if (!["UNIQUE", "AMBIGUOUS"].includes(record.resolution_status)) continue;
      matches.set(record.id, {
        id: record.id,
        qualifiedName: record.qualified_name,
        filePath: record.file_path,
        ambiguous: record.resolution_status === "AMBIGUOUS",
        mention: execution.request?.query ?? "",
      });
    }
  }
  return [...matches.values()];
}

export function questionForSymbol(question, match) {
  return match.mention && question.includes(match.mention)
    ? question.replaceAll(match.mention, match.qualifiedName)
    : `Who calls ${match.qualifiedName}?`;
}
