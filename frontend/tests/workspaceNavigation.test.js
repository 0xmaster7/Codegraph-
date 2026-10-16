import test from "node:test";
import assert from "node:assert/strict";
import { canOpenWorkspace, canUseValidatedSnapshot, workspaceNavigation, workspaceTitle } from "../src/ui/workspaceNavigation.js";

test("primary navigation follows the four product tasks with human-readable labels", () => {
  assert.deepEqual(workspaceNavigation.map(({ id }) => id), ["investigate", "graph", "changes", "history"]);
  assert.deepEqual(workspaceNavigation.map(({ label }) => label), ["Investigate", "Explore graph", "Changes & tests", "History"]);
});

test("repository-dependent workspaces stay unavailable until a snapshot is selected", () => {
  assert.equal(canOpenWorkspace("investigate", false), true);
  assert.equal(canOpenWorkspace("history", false), true);
  assert.equal(canOpenWorkspace("graph", false), false);
  assert.equal(canOpenWorkspace("changes", false), false);
  assert.equal(canOpenWorkspace("graph", true), true);
});

test("workspace titles are visible words rather than implementation route names", () => {
  assert.equal(workspaceTitle("graph"), "Explore graph");
  assert.equal(workspaceTitle("changes"), "Changes & tests");
});

test("failed and incomplete snapshots cannot use validated graph workflows", () => {
  assert.equal(canUseValidatedSnapshot("READY"), true);
  for (const status of ["FAILED", "INGESTING", "VALIDATING", "CANCELLED", "INTERRUPTED"]) {
    assert.equal(canUseValidatedSnapshot(status), false);
  }
});
