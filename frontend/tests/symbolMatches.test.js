import assert from "node:assert/strict";
import test from "node:test";
import { investigationSymbolMatches, questionForSymbol } from "../src/ui/symbolMatches.js";

const record = { id: "method-1", qualified_name: "src.click.core.Command.parse_args", file_path: "src/click/core.py", resolution_status: "AMBIGUOUS" };
const execution = { request: { query: "parse_args" }, result: { tool: "resolve_symbol", results: [record] } };

test("ambiguous matches retain canonical identity and rerun the original intent", () => {
  const [match] = investigationSymbolMatches([execution]);
  assert.equal(match.ambiguous, true);
  assert.equal(match.filePath, "src/click/core.py");
  assert.equal(questionForSymbol("What functions call parse_args?", match), "What functions call src.click.core.Command.parse_args?");
  assert.equal(questionForSymbol("Where is parse_args defined?", match), "Where is src.click.core.Command.parse_args defined?");
});

test("only resolver records with usable identities appear, without duplicates", () => {
  const unique = { ...execution, result: { ...execution.result, results: [{ ...record, resolution_status: "UNIQUE" }, { id: "invalid" }] } };
  const matches = investigationSymbolMatches([unique, unique, { result: { tool: "search_code_text", results: [record] } }]);
  assert.equal(matches.length, 1);
  assert.equal(matches[0].ambiguous, false);
  assert.deepEqual(investigationSymbolMatches([]), []);
});
