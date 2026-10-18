export type SymbolMatch = { id: string; qualifiedName: string; filePath: string; ambiguous: boolean; mention: string };
export function investigationSymbolMatches(executions: Array<{ request?: { query?: string | null } | null; result: { tool: string; results: Array<Record<string, unknown>> } }>): SymbolMatch[];
export function questionForSymbol(question: string, match: SymbolMatch): string;
