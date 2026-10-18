import assert from 'node:assert/strict';
import test from 'node:test';
import {connectedRelations, expandConnections, preserveGraphPositions} from '../src/ui/graphFocus.js';
const edges = [ ['ab','a','b'], ['bc','b','c'], ['cd','c','d'], ['xy','x','y'] ].map(([id,a,b])=>({id,source:{id:a},target:{id:b}}));
test('focused popup contains only incident connections, never the wider frontier',()=>{
 assert.deepEqual(connectedRelations(edges,'b').map(edge=>edge.id),['ab','bc']);
 assert.deepEqual(connectedRelations(edges,'ab').map(edge=>edge.id),['ab','bc']);
 assert.deepEqual(connectedRelations(edges,'callsite','b').map(edge=>edge.id),['ab','bc']);
 assert.deepEqual(connectedRelations(edges,'missing'),[]);
});
test('expansion adds only adjacent real edges, retains focus, and bounds results',()=>{
 assert.deepEqual(expandConnections(edges,['ab'],'b'),['ab','bc']);
 assert.deepEqual(expandConnections(edges,['ab'],'ab'),['ab','bc']);
 assert.deepEqual(expandConnections(edges,['ab'],'callsite','b'),['ab','bc']);
 assert.deepEqual(expandConnections(edges,['ab'],'missing'),['ab']);
 assert.equal(expandConnections(edges,['ab'],'b',null,1).length,1);
});
test('expansion preserves positions without overlap; unrelated focus may reset',()=>{
 const previous={points:new Map([['a',{x:120,y:130}],['b',{x:430,y:130}]]),width:740,height:260};
 const points=new Map([['a',{x:0,y:0}],['b',{x:5,y:0}],['c',{x:10,y:0}]]);
 const next=preserveGraphPositions(points,previous,960,520);
 assert.equal(next.preserveView,true);
 assert.deepEqual(next.points.get('a'),previous.points.get('a'));
 assert.deepEqual(next.points.get('b'),previous.points.get('b'));
 assert.notDeepEqual(next.points.get('c'),previous.points.get('a'));
 assert.equal(previous.points.has('c'),false);
 assert.equal(preserveGraphPositions(new Map([['x',{x:0,y:0}]]),previous,740,260).preserveView,false);
});
test('repeated expansion follows the visible connected frontier, not unrelated visible nodes',()=>{
 assert.deepEqual(expandConnections(edges,['ab','bc'],'a'),['ab','bc','cd']);
 assert.deepEqual(expandConnections(edges,['ab','bc'],'ab'),['ab','bc','cd']);
 const withUnrelated = [...edges,{id:'yz',source:{id:'y'},target:{id:'z'}}];
 assert.deepEqual(expandConnections(withUnrelated,['ab','xy'],'a'),['ab','xy','bc']);
 assert.deepEqual(expandConnections(edges,['ab','bc','cd'],'a'),['ab','bc','cd']);
 assert.deepEqual(expandConnections(edges,['ab','bc'],'a',null,2),['ab','bc']);
});
test('dense neighborhoods expand in manageable batches without losing existing edges',()=>{
 const dense = Array.from({length:20},(_,index)=>({id:`edge-${index}`,source:{id:'hub'},target:{id:`node-${index}`}}));
 const first = expandConnections(dense,[],'hub');
 assert.equal(first.length,12);
 const second = expandConnections(dense,first,'hub');
 assert.equal(second.length,20);
 assert.deepEqual(second.slice(0,12),first);
});
