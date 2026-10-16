/**
 * Prefer a structural relationship when an evidence-path step contains both
 * its proof edge and provenance premise edges. Preserve path order within each
 * evidence class so Inspector navigation remains predictable.
 * @param {string[]} edgeIds
 * @param {Set<string>} structuralEdgeIds
 * @param {Set<string>} provenanceEdgeIds
 * @returns {string | undefined}
 */
export function selectEvidencePathEdge(edgeIds, structuralEdgeIds, provenanceEdgeIds) {
  return edgeIds.find((id) => structuralEdgeIds.has(id))
    ?? edgeIds.find((id) => provenanceEdgeIds.has(id));
}
