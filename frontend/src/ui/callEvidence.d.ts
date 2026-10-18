type CallAnalysis = {
  bindings: Array<{ edge_id: string; binding_state: string; site_id: string; target_entity_id: string | null; external_symbol_id: string | null }>;
  sites: Array<{ site_id: string; kind: string }>;
  provenance: Array<{ edge_id: string }>;
  derived_edges: Array<{ edge_id: string; relation: string; rule_id: string; source_id: string; target_id: string; premise_edge_ids: string[] }>;
};
export function exactCallAliases(analysis: CallAnalysis): Map<string, string>;
