/**
 * Place unresolved sites below the relationship layout in a separate band.
 * @param {number} index
 * @param {number} relationshipHeight
 * @returns {{x: number, y: number}}
 */
export function unresolvedSitePosition(index, relationshipHeight) {
  return {
    x: 120 + (index % 3) * 300,
    y: relationshipHeight + 40 + Math.floor(index / 3) * 78,
  };
}

/**
 * Keep sparse graphs compact while preserving a readable native scale for larger graphs.
 * @param {number} layoutHeight
 * @returns {number}
 */
export function graphViewportHeight(layoutHeight) {
  return Math.max(320, Math.min(1400, layoutHeight));
}

/**
 * Distribute relationship nodes inside the relationship band, before unresolved cards.
 * @param {number} index
 * @param {number} columnSize
 * @param {number} relationshipHeight
 * @returns {number}
 */
export function relationshipNodeY(index, columnSize, relationshipHeight) {
  return ((index + 1) * relationshipHeight) / (columnSize + 1);
}
