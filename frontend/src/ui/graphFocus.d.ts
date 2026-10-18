type Point = { x: number; y: number };
type Relation = { id: string; source: { id: string }; target: { id: string } };
export function connectedRelations<RelationType extends Relation>(edges: RelationType[], selectedId: string, contextId?: string | null): RelationType[];
export function expandConnections(edges: Relation[], visibleIds: string[], selectedId: string, contextId?: string | null, limit?: number): string[];
export function preserveGraphPositions(points: Map<string, Point>, previous: { points: Map<string, Point>; width: number; height: number } | null, width: number, height: number): { points: Map<string, Point>; width: number; height: number; preserveView: boolean };
