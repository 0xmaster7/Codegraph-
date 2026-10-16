import assert from "node:assert/strict";
import test from "node:test";
import { selectEvidencePathEdge } from "../src/ui/graphPathSelection.js";
import { graphViewportHeight, relationshipNodeY, unresolvedSitePosition } from "../src/ui/graphLayout.js";
import {
  applyAuthoritativeExecution,
  canExecuteApproval,
  fetchAuthoritativeApproval,
  parseExecutionResponse,
  SingleFlightGuard,
} from "../src/ui/runtimeApprovalState.js";

const approval = (status) => ({
  approval_id: "approval_1",
  snapshot_id: "snap_1",
  runtime_environment_id: "runtime_1",
  run_id: "run_1",
  test_ids: ["invocation_1"],
  pytest_command: ["python", "-m", "pytest"],
  exact_command_hash: "hash",
  expires_at: 2_000_000_000,
  status,
});

test("only an authoritative approved state can expose execution", () => {
  assert.equal(canExecuteApproval(approval("APPROVED"), false), true);
  assert.equal(canExecuteApproval(approval("APPROVED"), true), false);
  for (const status of ["PENDING", "CONSUMED", "REVOKED", "EXPIRED"]) {
    assert.equal(canExecuteApproval(approval(status), false), false);
  }
  assert.equal(canExecuteApproval(null, false), false);
});

test("successful execution adopts authoritative CONSUMED state and retains result", () => {
  const execution = {
    run_id: "run_1",
    status: "COMPLETED",
    snapshot_id: "snap_1",
    runtime_environment_id: "runtime_1",
    passed: 2,
  };
  const updated = applyAuthoritativeExecution(
    "approval_1",
    approval("CONSUMED"),
    execution,
  );
  assert.equal(updated.approval.status, "CONSUMED");
  assert.equal(canExecuteApproval(updated.approval, false), false);
  assert.equal(updated.execution, execution);
  assert.equal(updated.execution.run_id, "run_1");
});

test("duplicate clicks are rejected while the first execution is pending", () => {
  const guard = new SingleFlightGuard();
  assert.equal(guard.enter(), true);
  assert.equal(guard.enter(), false);
  guard.leave();
  assert.equal(guard.enter(), true);
  guard.leave();
});

test("backend rejection refreshes consumed, revoked, and expired states", async () => {
  for (const status of ["CONSUMED", "REVOKED", "EXPIRED"]) {
    const state = await fetchAuthoritativeApproval("approval_1", async (url) => {
      assert.match(url, /approval_1$/);
      return { ok: true, json: async () => approval(status) };
    });
    assert.equal(state.status, status);
    assert.equal(canExecuteApproval(state, false), false);
  }
});

test("approval refresh targets a separately configured local backend", async () => {
  const state = await fetchAuthoritativeApproval("approval_1", async (url) => {
    assert.equal(url, "http://127.0.0.1:18080/api/runtime/action/approval_1");
    return { ok: true, json: async () => approval("APPROVED") };
  }, "http://127.0.0.1:18080/");
  assert.equal(state.status, "APPROVED");
});

test("approval refresh rejects mismatched identity and backend failure", async () => {
  await assert.rejects(
    fetchAuthoritativeApproval("approval_1", async () => ({
      ok: true,
      json: async () => ({ ...approval("APPROVED"), approval_id: "other" }),
    })),
    /different approval identity/,
  );
  await assert.rejects(
    fetchAuthoritativeApproval("approval_1", async () => ({
      ok: false,
      json: async () => ({ detail: "approval already consumed" }),
    })),
    /already consumed/,
  );
});

test("execution update cannot attach a result to a different approval", () => {
  assert.throws(
    () => applyAuthoritativeExecution(
      "approval_1",
      { ...approval("CONSUMED"), approval_id: "other" },
      { run_id: "run_1" },
    ),
    /different approval identity/,
  );
});

test("backend execution failure is rejected before a successful result can be applied", async () => {
  await assert.rejects(
    parseExecutionResponse({
      ok: false,
      json: async () => ({ detail: "approval already consumed" }),
    }),
    /approval already consumed/,
  );
});

test("evidence-path inspection prefers structural proof over sorted provenance premises", () => {
  const edgeIds = ["syntax.site-context.v1", "scip.binding.v1"];
  assert.equal(
    selectEvidencePathEdge(
      edgeIds,
      new Set(["scip.binding.v1"]),
      new Set(["syntax.site-context.v1"]),
    ),
    "scip.binding.v1",
  );
});

test("unresolved site cards occupy a separate, non-overlapping graph band", () => {
  const first = unresolvedSitePosition(0, 1100);
  const third = unresolvedSitePosition(2, 1100);
  const fourth = unresolvedSitePosition(3, 1100);
  assert.equal(first.y, 1140);
  assert.equal(third.y, first.y);
  assert.equal(fourth.y - first.y, 78);
  assert.ok(third.x - first.x >= 300);
});

test("graph viewport stays compact for small graphs and scales for dense graphs", () => {
  assert.equal(graphViewportHeight(260), 320);
  assert.equal(graphViewportHeight(920), 920);
  assert.equal(graphViewportHeight(1900), 1400);
});

test("relationship nodes stay above the separate unresolved-site band", () => {
  const lastRelationshipY = relationshipNodeY(9, 10, 1000);
  const firstUnresolvedY = unresolvedSitePosition(0, 1000).y;
  assert.ok(lastRelationshipY < 1000);
  assert.ok(firstUnresolvedY > 1000);
});
