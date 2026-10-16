import assert from "node:assert/strict";
import test from "node:test";
import { evidenceStateLabel, humanAnswerText, humanClaimType, humanDispatchState, humanExternalName, humanRepositoryName, humanRelationshipLabel, humanSymbolName, replayStateLabel } from "../src/ui/evidencePresentation.js";

test("relationship labels preserve graph meaning and uncertainty", () => {
  assert.equal(humanRelationshipLabel("DEFINES_METHOD"), "Defines method");
  assert.equal(humanRelationshipLabel("CALL_SITE_BINDING", "EXACT"), "Calls");
  assert.equal(humanRelationshipLabel("MAY_DISPATCH_TO", "MAY_DISPATCH"), "May call");
  assert.equal(humanRelationshipLabel("INHERITS"), "Inherits from");
  assert.equal(humanRelationshipLabel("UNRESOLVED", "UNRESOLVED"), "Target unresolved");
  assert.equal(humanRelationshipLabel("RUNTIME_OBSERVATION", "RUNTIME"), "Runtime observed call");
});

test("static binding and replay remain separate states", () => {
  assert.equal(evidenceStateLabel({ modality: "EXACT", replayState: "NOT_RUN" }), "Exact static relationship · replay not run");
  assert.equal(evidenceStateLabel({ modality: "EXACT", replayState: "PASS" }), "Replay verified");
  assert.equal(evidenceStateLabel({ modality: "MAY_DISPATCH", replayState: "NOT_RUN" }), "Possible call");
  assert.equal(evidenceStateLabel({ modality: "RUNTIME_OBSERVATION", replayState: "PASS" }), "Runtime observation");
  assert.equal(evidenceStateLabel({ modality: "EXACT", replayState: "EDGE_NOT_REPRODUCED" }), "Replay mismatch · excluded from exact paths");
  assert.equal(replayStateLabel("NOT_RUN"), "Replay not run");
});

test("user-facing labels remove schema casing without losing symbol context", () => {
  assert.equal(humanSymbolName("parse_args", "Method"), "parse_args()");
  assert.equal(humanSymbolName("_OptionParser", "Class"), "_OptionParser");
  assert.equal(humanClaimType("STRUCTURAL_PATH"), "Call path");
  assert.equal(humanDispatchState("MULTI_CANDIDATE", 3), "Several possible targets · 3");
});

test("external symbols and answer summaries use human wording", () => {
  assert.equal(humanExternalName("scip-python python python-stdlib 3.11 builtins/ValueError#"), "ValueError");
  assert.equal(humanExternalName("local 3", "requests", "requests.api"), "requests");
  assert.equal(humanAnswerText("A verified graph path connects Editor to edit_files"), "An exact static path connects Editor to edit_files");
});

test("repository display hides generated worktree identifiers", () => {
  assert.equal(humanRepositoryName("/private/tmp/repos/click"), "click");
  assert.equal(humanRepositoryName("/private/tmp/repos/8b19813f2bfc-eb2b9a4489ca4085b8310f4a1145957e"), "Python repository");
  assert.equal(humanRepositoryName(null), "Python repository");
});
