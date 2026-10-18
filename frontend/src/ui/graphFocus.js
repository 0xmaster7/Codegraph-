// Keep expansion scoped to records already present in the selected snapshot.
export function connectedRelations(edges, selectedId, contextId = null) {
  const selected = edges.find(edge => edge.id === selectedId);
  const focus = new Set(selected ? [selected.source.id, selected.target.id] : [selectedId]);
  if (contextId) focus.add(contextId);
  return edges.filter(edge => focus.has(edge.source.id) || focus.has(edge.target.id));
}

export function expandConnections(edges, visibleIds, selectedId, contextId = null, limit = 72) {
  const selected = edges.find(edge => edge.id === selectedId);
  const focus = new Set(selected ? [selected.source.id, selected.target.id] : [selectedId]);
  if (contextId) focus.add(contextId);
  const visible = new Set(visibleIds);
  const adjacent = edges.filter(edge => focus.has(edge.source.id) || focus.has(edge.target.id));
  let additions = adjacent.filter(edge => !visible.has(edge.id));
  if (!additions.length) {
    let changed = true;
    while (changed) {
      changed = false;
      for (const edge of edges) {
        if (!visible.has(edge.id) || (!focus.has(edge.source.id) && !focus.has(edge.target.id))) continue;
        for (const endpoint of [edge.source.id, edge.target.id]) {
          if (!focus.has(endpoint)) { focus.add(endpoint); changed = true; }
        }
      }
    }
    additions = edges.filter(edge => !visible.has(edge.id) && (focus.has(edge.source.id) || focus.has(edge.target.id)));
  }
  return [...new Set([...visibleIds, ...additions.slice(0, 12).map(edge => edge.id)])].slice(0, limit);
}

export function preserveGraphPositions(points, previous, width, height) {
  if (!previous || ![...previous.points.keys()].every(id => points.has(id))) return { points, width, height, preserveView: false };
  const result = new Map(previous.points);
  const added = [...points.keys()].filter(id => !result.has(id));
  // Fill free slots around the current focus before extending the canvas.
  for (const id of added) {
    let found = false;
    for (let row = 0; !found; row++) {
      for (let column = 0; column < Math.max(2, Math.floor(previous.width / 310)); column++) {
        const point = { x: 120 + column * 310, y: 40 + row * 92 };
        if ([...result.values()].every(other => Math.abs(other.x-point.x) >= 200 || Math.abs(other.y-point.y) >= 70)) {
          result.set(id, point); found = true; break;
        }
      }
    }
  }
  return { points: result, width: Math.max(previous.width, ...[...result.values()].map(p => p.x + 120)), height: Math.max(previous.height, ...[...result.values()].map(p => p.y + 65)), preserveView: true };
}
