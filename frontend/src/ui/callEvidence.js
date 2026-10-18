// A dispatch projection of an exact binding is supporting evidence, not a second call.
export function exactCallAliases(analysis) {
  const bindings = new Map(analysis.bindings.map(binding => [binding.edge_id, binding]));
  const sites = new Map(analysis.sites.map(site => [site.site_id, site]));
  const provenance = new Map(analysis.provenance.map(record => [record.edge_id, record]));
  const aliases = new Map();
  for (const edge of analysis.derived_edges) {
    if (edge.relation !== 'MAY_DISPATCH_TO' || edge.rule_id !== 'dispatch.exact-target.v1') continue;
    for (const premise of edge.premise_edge_ids) {
      const binding = bindings.get(premise);
      if (!binding || binding.binding_state !== 'EXACT' || sites.get(binding.site_id)?.kind !== 'CALL_CALLEE') continue;
      if (!provenance.has(premise) || !provenance.has(edge.edge_id)) continue;
      if (binding.site_id === edge.source_id && (binding.target_entity_id ?? binding.external_symbol_id) === edge.target_id) {
        aliases.set(edge.edge_id, premise);
        break;
      }
    }
  }
  return aliases;
}
