import assert from 'node:assert/strict';
import test from 'node:test';
import { exactCallAliases } from '../src/ui/callEvidence.js';
const fixture = () => ({
  sites: [{site_id:'site',kind:'CALL_CALLEE'}],
  bindings: [{edge_id:'exact',site_id:'site',target_entity_id:'target',external_symbol_id:null,binding_state:'EXACT'}],
  provenance: [{edge_id:'exact'},{edge_id:'derived'}],
  derived_edges: [{edge_id:'derived',relation:'MAY_DISPATCH_TO',rule_id:'dispatch.exact-target.v1',source_id:'site',target_id:'target',premise_edge_ids:['exact']}]
});
test('same-site exact dispatch shares one Calls arrow and retains both evidence IDs', () => {
  assert.deepEqual([...exactCallAliases(fixture())], [['derived','exact']]);
  const external = fixture(); external.bindings[0].target_entity_id = null; external.bindings[0].external_symbol_id = 'target';
  assert.equal(exactCallAliases(external).get('derived'), 'exact');
});
test('different call sites, targets, uncertain dispatch, references, and missing evidence remain separate', () => {
  for (const mutate of [
    a => a.derived_edges[0].source_id = 'other-site',
    a => a.derived_edges[0].target_id = 'other-target',
    a => a.derived_edges[0].rule_id = 'dispatch.receiver-mro.v1',
    a => a.derived_edges[0].premise_edge_ids = [],
    a => a.bindings[0].binding_state = 'UNRESOLVED',
    a => a.sites[0].kind = 'IMPORT_REFERENCE',
    a => a.provenance = [{edge_id:'derived'}],
  ]) { const a = fixture(); mutate(a); assert.equal(exactCallAliases(a).size,0); }
});
