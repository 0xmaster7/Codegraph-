export function humanRelationshipLabel(relation: string, modality?: string): string;
export function evidenceStateLabel(input?: { modality?: string; replayState?: string | null; reviewState?: string }): string;
export function replayStateLabel(value: string | null | undefined): string;
export function humanSymbolName(name: string, kind?: string): string;
export function humanClaimType(value: string): string;
export function humanDispatchState(value: string, candidateCount?: number): string;
export function humanExternalName(semanticSymbol: string, distributionName?: string | null, moduleName?: string | null): string;
export function humanAnswerText(value: string | null | undefined): string;
export function humanRepositoryName(root: string | null | undefined): string;

export function humanBindingLabel(siteKind: string): string;
