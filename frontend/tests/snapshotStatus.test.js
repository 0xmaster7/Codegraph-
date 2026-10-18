import test from "node:test";
import assert from "node:assert/strict";
import { isSnapshotInProgress, snapshotProgressMessage, withSnapshotStatus } from "../src/ui/snapshotStatus.js";

test("the graph snapshot record wins over stale cached analysis status", () => {
  const analysis = { snapshot_id: "snap_test", status: "VALIDATING", entities: [] };
  assert.deepEqual(withSnapshotStatus(analysis, { status: "READY" }), {
    ...analysis,
    status: "READY",
  });
});

test("missing snapshot status preserves the analysis status", () => {
  const analysis = { status: "VALIDATING" };
  assert.deepEqual(withSnapshotStatus(analysis, {}), analysis);
});

test("in-progress snapshots have clear progress copy", () => {
  assert.equal(isSnapshotInProgress("VALIDATING"), true);
  assert.equal(snapshotProgressMessage("VALIDATING"), "Checking that the saved graph matches its source.");
  assert.equal(isSnapshotInProgress("READY"), false);
  assert.equal(isSnapshotInProgress("FAILED"), false);
});
