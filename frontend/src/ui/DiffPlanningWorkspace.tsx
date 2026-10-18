import { EvidenceSource } from "./SourceEvidence";
import { RequestScope } from "./request.js";
import { API_BASE_URL } from "../apiConfig";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowRight, GitCompareArrows, LoaderCircle } from "lucide-react";
import {
  canExecuteApproval,
  applyAuthoritativeExecution,
  fetchAuthoritativeApproval,
  parseExecutionResponse,
  SingleFlightGuard,
  type ActionApproval,
} from "./runtimeApprovalState";
import { evidenceStateLabel, humanRelationshipLabel } from "./evidencePresentation.js";

type Evidence = {
  evidence_type: string;
  type?: string;
  snapshot_id: string;
  record_id: string;
  source_record_id?: string;
  evidence_id?: string | null;
  file_path?: string | null;
  content_hash?: string | null;
  runtime_materialization_id?: string | null;
  runtime_environment_id?: string | null;
  run_id?: string | null;
  start_line?: number | null;
  end_line?: number | null;
  entity_id?: string | null;
};
type ChangedSymbol = {
  change_id: string;
  classification: string;
  base_snapshot_id: string;
  target_snapshot_id: string;
  base_qualified_name?: string | null;
  target_qualified_name?: string | null;
  base_file_path?: string | null;
  target_file_path?: string | null;
  deterministic_reason: string;
  evidence: Evidence[];
};
type EdgeEvidence = {
  evidence_id: string;
  type: string;
  snapshot_id: string;
  source_record_id: string;
  provenance_pointer: string;
};
type Recommendation = {
  test_definition_id?: string | null;
  test_invocation_id?: string | null;
  pytest_node_id?: string | null;
  tier: "A" | "B" | "C";
  label: string;
  deterministic_reason: string;
  evidence: Evidence[];
  impact_path: ImpactPath | null;
};
type ImpactStep = {
  source_id: string;
  target_id: string;
  relation: string;
  edge_ids: string[];
  site_id?: string | null;
  uncertain: boolean;
  evidence: EdgeEvidence[];
};
type UnresolvedBarrier = {
  site_id: string;
  expression: string;
  file_path: string;
  resolution: string;
};
type ImpactPath = {
  change_id: string;
  snapshot_id: string;
  changed_entity_id: string;
  dependent_entity_id: string;
  distance: number;
  mode: "STRICT_STATIC_BINDING" | "CONSERVATIVE_DISPATCH";
  path_semantics: "STRICT_EXACT" | "CONSERVATIVE" | "INCOMPLETE";
  evidence_complete: boolean;
  uncertainty_count: number;
  path: string[];
  steps: ImpactStep[];
  evidence: Evidence[];
  unresolved_barriers: UnresolvedBarrier[];
};
type DiffResult = {
  base_commit_sha: string;
  target_commit_sha: string;
  base_snapshot_id: string;
  target_snapshot_id: string;
  changed_files: Array<{ change_id: string; status: string; old_path: string | null; new_path: string | null; base_ranges: Array<{ start_line: number; line_count: number }>; target_ranges: Array<{ start_line: number; line_count: number }> }>;
  changed_symbols: ChangedSymbol[];
  impact_paths: ImpactPath[];
  test_plan: { snapshot_id: string; runtime_materialization_id: string | null; runtime_environment_id: string | null; runtime_baseline_available: boolean; collection_status: string; tiers: Record<string, string>; recommendations: Recommendation[]; diagnostics: string[] };
  candidate_count_before_limit: number;
  truncated: boolean;
  diagnostics: string[];
};
type TestExecution = {
  run_id: string;
  snapshot_id: string;
  runtime_environment_id: string;
  status: string;
  exit_code: number | null;
  passed: number;
  failed: number;
  skipped: number;
  stdout: string;
  stderr: string;
  runtime_validation: { observed_call_count: number; mapped_call_count: number; unmapped_call_count: number; mismatch_count: number } | null;
};

const API = API_BASE_URL;

export function DiffPlanningWorkspace({ repositoryPath, targetSha, workingTreeDirty, onRuntimeCompleted }: { repositoryPath: string; targetSha: string | null; workingTreeDirty: boolean; onRuntimeCompleted: (runId: string) => void }) {
  const [baseSha, setBaseSha] = useState("");
  const [target, setTarget] = useState(targetSha ?? "");
  const [result, setResult] = useState<DiffResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selectedTestIds, setSelectedTestIds] = useState<string[]>([]);
  const [approval, setApproval] = useState<ActionApproval | null>(null);
  const [execution, setExecution] = useState<TestExecution | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const comparison = useRef(new RequestScope());
  const comparing = useRef(false);
  useEffect(() => () => comparison.current.cancel(), []);
  const executionGuard = useRef(new SingleFlightGuard());
  const completedStep = execution ? 3 : approval ? 2 : result ? 1 : 0;

  async function refreshApprovalState(approvalId: string) {
    try {
      const state = await fetchAuthoritativeApproval(approvalId, fetch, API);
      setApproval(state);
      return state;
    } catch {
      // Unknown approval state must never leave an executable local approval visible.
      setApproval(null);
      return null;
    }
  }

  useEffect(() => setTarget(targetSha ?? ""), [targetSha]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || comparing.current || !repositoryPath || !baseSha.trim() || !target.trim()) return;
    comparing.current = true;
    const task = comparison.current.begin();
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const response = await fetch(`${API}/api/diff/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repository_path: repositoryPath, base_sha: baseSha.trim(), target_sha: target.trim() }),
        signal: AbortSignal.any([task.signal, AbortSignal.timeout(180000)]),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "Commit diff analysis failed");
      if (!task.current()) return;
      setResult(payload as DiffResult);
      setSelectedTestIds([]); setApproval(null); setExecution(null); setActionError("");
    } catch (reason) {
      if (task.current()) setError(reason instanceof Error ? reason.message : "Commit diff analysis failed");
    } finally {
      comparing.current = false;
      if (task.current()) setBusy(false);
    }
  }

  async function prepareExecution() {
    if (!result || !result.test_plan.runtime_environment_id || !selectedTestIds.length || actionBusy) return;
    setActionBusy(true); setActionError(""); setApproval(null); setExecution(null);
    try {
      const response = await fetch(`${API}/api/runtime/action/prepare`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ snapshot_id: result.target_snapshot_id, runtime_environment_id: result.test_plan.runtime_environment_id, test_ids: selectedTestIds }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "Could not prepare this test run");
      setApproval(payload as ActionApproval);
    } catch (reason) { setActionError(reason instanceof Error ? reason.message : "Could not prepare this test run"); }
    finally { setActionBusy(false); }
  }

  async function approveExecution() {
    if (!approval || approval.status !== "PENDING" || actionBusy) return;
    setActionBusy(true); setActionError("");
    try {
      const response = await fetch(`${API}/api/runtime/action/${encodeURIComponent(approval.approval_id)}/approve`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "Could not approve this test run");
      setApproval(payload as ActionApproval);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Could not approve this test run";
      setActionError(message);
      await refreshApprovalState(approval.approval_id);
    }
    finally { setActionBusy(false); }
  }

  async function revokeExecution() {
    if (!approval || !["PENDING", "APPROVED"].includes(approval.status) || actionBusy) return;
    setActionBusy(true); setActionError("");
    try {
      const response = await fetch(`${API}/api/runtime/action/${encodeURIComponent(approval.approval_id)}/revoke`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "Could not discard this approval");
      setApproval(payload as ActionApproval);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Could not discard this approval";
      setActionError(message);
      await refreshApprovalState(approval.approval_id);
    }
    finally { setActionBusy(false); }
  }

  async function executeApproved() {
    if (!approval || !canExecuteApproval(approval, actionBusy) || !executionGuard.current.enter()) return;
    setActionBusy(true); setActionError(""); setExecution(null);
    try {
      const response = await fetch(`${API}/api/runtime/action/${encodeURIComponent(approval.approval_id)}/execute`, { method: "POST" });
      const completed = await parseExecutionResponse<TestExecution>(response);
      const authoritative = await refreshApprovalState(approval.approval_id);
      if (authoritative) {
        const updated = applyAuthoritativeExecution(approval.approval_id, authoritative, completed);
        setApproval(updated.approval);
        setExecution(updated.execution);
      } else {
        // Keep the completed result visible while hiding authorization state that could not
        // be confirmed from the backend.
        setExecution(completed);
        setActionError("Execution completed, but approval state could not be refreshed.");
      }
      onRuntimeCompleted(completed.run_id);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Approved test execution failed";
      setActionError(message);
      await refreshApprovalState(approval.approval_id);
    }
    finally { executionGuard.current.leave(); setActionBusy(false); }
  }

  function toggleTest(testId: string, checked: boolean) {
    setSelectedTestIds((current) => checked ? [...new Set([...current, testId])].sort() : current.filter((item) => item !== testId));
    setApproval(null); setExecution(null); setActionError("");
  }

  const approvalActive = approval?.status === "PENDING" || approval?.status === "APPROVED";

  return <section aria-busy={busy || actionBusy} className="diff-planning" aria-labelledby="diff-planning-title">
    <div className="diff-planning-heading">
      <div><span className="section-label">COMMIT COMPARISON</span><h2 id="diff-planning-title">Compare two versions</h2>
        <p>Choose the earlier and later commits. We’ll trace affected code and show which tests have supporting evidence.</p></div>
      <GitCompareArrows size={18} aria-hidden="true"/>
    </div>
    <ol className="change-steps" aria-label="Change analysis workflow">
      {["Compare commits", "Review impact and tests", "Approve the exact run", "Inspect results"].map((label, index) => <li key={label} className={index === completedStep ? "current" : index < completedStep ? "complete" : ""}><span>{index < completedStep ? "✓" : index + 1}</span><b>{label}</b></li>)}
    </ol>
    {workingTreeDirty && <p className="diff-warning">The selected snapshot recorded uncommitted changes. Comparison uses committed SHAs only, so those changes are not included.</p>}
    <form className="diff-form" onSubmit={(event) => void submit(event)}>
      <label>Earlier commit<input disabled={busy || actionBusy} required minLength={7} maxLength={40} value={baseSha} onChange={(event) => setBaseSha(event.target.value)} placeholder="Paste base commit SHA" autoComplete="off" spellCheck={false}/></label>
      <label>Later commit<input disabled={busy || actionBusy} required minLength={7} maxLength={40} value={target} onChange={(event) => setTarget(event.target.value)} placeholder="Later commit" autoComplete="off" spellCheck={false}/></label>
      <button type="submit" disabled={busy || !repositoryPath || !baseSha.trim() || !target.trim()}>{busy ? <><LoaderCircle className="spin" size={14}/> Analyzing</> : <>Compare commits <ArrowRight size={14}/></>}</button>
    </form>
    {!repositoryPath && <p className="diff-note">Select a repository snapshot to bind this analysis to its repository.</p>}
    {busy && <p className="diff-progress" role="status"><LoaderCircle className="spin" size={13}/> Comparing the saved source and tracing affected code. This can take a little while; keep this page open.</p>}
    {error && <p className="diff-error" role="alert">{error}</p>}
    {result && <div className="diff-results" aria-live="polite">
      <div className="diff-snapshot-pair"><span>Base commit <code>{result.base_commit_sha.slice(0, 12)}</code></span><ArrowRight size={13} aria-hidden="true"/><span>Target commit <code>{result.target_commit_sha.slice(0, 12)}</code></span></div><details className="diff-technical"><summary>Snapshot details</summary><code>Base snapshot {result.base_snapshot_id}</code><code>Target snapshot {result.target_snapshot_id}</code></details>
      {result.diagnostics.map((message) => <p className="diff-note" key={message}>{message}</p>)}
      {result.truncated && <p className="diff-warning">Results are bounded; inspect the diagnostics and result limit before drawing conclusions.</p>}
      <section className="diff-result-section"><h3>What changed <small>{result.changed_symbols.length} / {result.candidate_count_before_limit}</small></h3>
        {result.changed_symbols.filter((item) => item.classification !== "UNMAPPED").length > 0 ? <ul className="diff-symbol-list">{result.changed_symbols.filter((item) => item.classification !== "UNMAPPED").map((item, index) => <ChangedCode key={index} item={item}/>)}</ul> : <p className="diff-note">No changes could be confidently linked to a code element. Review the file changes below.</p>}
        {result.changed_symbols.some((item) => item.classification === "UNMAPPED") && <details className="unmapped-changes"><summary>{result.changed_symbols.filter((item) => item.classification === "UNMAPPED").length} changes need a closer look</summary><p className="diff-note">These changes could not be paired with a supported code element. They remain available for source review.</p><ul className="diff-symbol-list">{result.changed_symbols.filter((item) => item.classification === "UNMAPPED").map((item,index) => <ChangedCode key={index} item={item}/>)}</ul></details>}

      </section>
      <section className="diff-result-section"><h3>What might be affected <small>{result.impact_paths.length}</small></h3>
        {result.impact_paths.length ? <ul className="impact-list">{result.impact_paths.map((item, index) => <li key={`${item.snapshot_id}-${item.change_id}-${item.path.join("-")}-${index}`}>
          <b className="impact-title">{item.evidence.find((evidence) => evidence.entity_id === item.dependent_entity_id)?.file_path ?? item.evidence.find((evidence) => evidence.file_path)?.file_path ?? "Connected code"}</b><span>{item.path_semantics === "STRICT_EXACT" ? "Identified dependency" : item.path_semantics === "CONSERVATIVE" ? "Possible dependency" : "Incomplete evidence"} · {item.distance} {item.distance === 1 ? "step" : "steps"}{item.uncertainty_count ? ` · ${item.uncertainty_count} uncertain` : ""}</span>
          {item.evidence.length > 0 && <small>Source: {[...new Set(item.evidence.flatMap((evidence) => evidence.file_path ? [`${evidence.file_path}${evidence.start_line ? `:${evidence.start_line}` : ""}`] : []))].join(" · ")}</small>}
          {item.steps.length > 0 && <details><summary>How the impact path connects</summary><ol>{item.steps.map((step, stepIndex) => <li key={`${step.source_id}-${step.target_id}-${stepIndex}`}><span>{humanRelationshipLabel(step.relation)} · {evidenceStateLabel({ modality: step.uncertain ? "MAY_DISPATCH" : "EXACT" })}</span><details><summary>Technical details</summary><code>{step.relation} · {step.source_id} → {step.target_id}</code><code>Edge IDs {step.edge_ids.join(", ") || "unavailable"} · call site {step.site_id ?? "unavailable"}</code>{step.evidence.map((evidence) => <code key={evidence.evidence_id}>Evidence {evidence.evidence_id} · {evidence.type} · {evidence.source_record_id}</code>)}</details></li>)}</ol>{item.unresolved_barriers.map((barrier) => <small key={barrier.site_id}>Unresolved target · {barrier.file_path} · {barrier.expression}<details><summary>Technical details</summary><code>{barrier.site_id} · {barrier.resolution}</code></details></small>)}</details>}
          <EvidenceSource snapshotId={item.snapshot_id} entityId={item.dependent_entity_id}/><details><summary>Technical details</summary><code>Snapshot {item.snapshot_id} · change {item.change_id} · path {item.path.join(" → ")}</code></details>
        </li>)}</ul> : <p className="diff-note">No bounded graph dependents were found.</p>}
      </section>
      <section className="diff-result-section"><h3>Recommended tests</h3>
        <details className="tier-details"><summary>Test discovery details</summary><div className="tier-status">{Object.entries(result.test_plan.tiers).map(([tier, state]) => <span key={tier}>Tier {tier}: {state.replaceAll("_", " ")}</span>)}</div></details>
        {!result.test_plan.runtime_environment_id && <p className="diff-note">Test runs are not configured for this repository. You can still review the changes and source evidence.</p>}
        {!result.test_plan.runtime_baseline_available && <p className="diff-note">No recorded test baseline is available for coverage-backed recommendations.</p>}
        {result.test_plan.diagnostics.length > 0 && <details className="runtime-setup-details"><summary>Test environment details</summary>{result.test_plan.diagnostics.map((message) => <p className="diff-note" key={message}>{message}</p>)}</details>}
        {result.test_plan.recommendations.length ? <ol className="test-plan-list">{result.test_plan.recommendations.map((item) => <li key={`${item.pytest_node_id}-${item.tier}`}>
          <label className="test-run-select"><input type="checkbox" checked={!!item.test_invocation_id && selectedTestIds.includes(item.test_invocation_id)} disabled={!item.test_invocation_id || approvalActive || actionBusy} onChange={(event) => item.test_invocation_id && toggleTest(item.test_invocation_id, event.target.checked)}/><span>Select for approved run</span></label>
          <span className={`tier-badge tier-${item.tier.toLowerCase()}`}>{item.label.replaceAll("_", " ").toLowerCase()}</span>
          <span className="test-human-label">{item.pytest_node_id ? `Test ${item.pytest_node_id.split("::").at(-1)}` : "Test recommendation"}</span><details><summary>Technical details</summary><code>Evidence tier {item.tier} · {item.pytest_node_id ?? item.test_definition_id ?? "Test ID unavailable"}</code></details>
          <p>{item.deterministic_reason}</p>
          {item.impact_path && <details><summary>{item.impact_path.path_semantics === "STRICT_EXACT" ? "Exact structural path" : item.impact_path.path_semantics === "CONSERVATIVE" ? "Conservative dispatch path" : "Path evidence incomplete"} · {item.impact_path.uncertainty_count} uncertain step(s)</summary>
            <code>{item.impact_path.path.join(" → ")}</code>
            <ol>{item.impact_path.steps.map((step, index) => <li key={`${step.source_id}-${step.target_id}-${index}`}><span>{step.relation}{!item.impact_path?.evidence_complete ? " · evidence unavailable" : step.uncertain ? " · conservative" : " · exact"}</span><code>{step.source_id} → {step.target_id}</code><small>Edges: {step.edge_ids.join(", ") || "unavailable"}</small>{step.evidence.map((evidence) => <small key={evidence.evidence_id}>Evidence {evidence.evidence_id} · {evidence.type} · {evidence.source_record_id}</small>)}</li>)}</ol>
            {item.impact_path.unresolved_barriers.map((barrier) => <small key={barrier.site_id}>Unresolved barrier · {barrier.file_path} · {barrier.expression} · {barrier.resolution}</small>)}
          </details>}
          <details><summary>Evidence · {item.evidence.length}</summary>{item.evidence.map((evidence) => <code key={`${evidence.snapshot_id}-${evidence.evidence_id ?? evidence.record_id}`}>{evidence.evidence_type} · {evidence.snapshot_id} · {evidence.evidence_id ?? evidence.record_id}{evidence.runtime_materialization_id ? ` · ${evidence.runtime_materialization_id}` : ""}{evidence.runtime_environment_id ? ` · ${evidence.runtime_environment_id}` : ""}</code>)}</details>
        </li>)}</ol> : <p className="diff-note">No test recommendations are available for this diff.</p>}
        {result.test_plan.recommendations.length > 0 && <section className="approved-test-run" aria-label="Explicitly approved test execution">
          <h4>Run selected tests</h4>
          <p>Only selected collected pytest invocations can run. Preparation does not approve execution; approval is single-use and expires after ten minutes. Tests run in the isolated container against the target snapshot.</p>
          {!result.test_plan.runtime_environment_id && <p className="diff-warning">No runtime environment is available for this snapshot; execution is unavailable.</p>}
          <button type="button" disabled={actionBusy || approvalActive || !selectedTestIds.length || !result.test_plan.runtime_environment_id || result.test_plan.collection_status !== "COMPLETED"} onClick={() => void prepareExecution()}>{actionBusy && !approval ? "Preparing…" : `Prepare approval · ${selectedTestIds.length} selected`}</button>
          {approval && <div className="approval-review" aria-live="polite">
            <b>{approval.status === "PENDING" ? "Review exact run before approving" : `Approval ${approval.status.toLowerCase()}`}</b>
            <details><summary>Snapshot and environment</summary><code>{approval.snapshot_id} · {approval.runtime_environment_id}</code></details>
            <small>Approval expires {new Date(approval.expires_at * 1000).toLocaleString()} · command SHA-256 {approval.exact_command_hash}</small>
            <p>Exact deterministic pytest argv inside the isolated container (passed as argv; no shell command is accepted):</p>
            <pre>{JSON.stringify(approval.pytest_command, null, 2)}</pre>
            {approval.status === "PENDING" && <button type="button" disabled={actionBusy} onClick={() => void approveExecution()}>I approve this exact test run</button>}
            {canExecuteApproval(approval, actionBusy) && <button type="button" onClick={() => void executeApproved()}>Execute approved tests once</button>}
            {approval.status === "PENDING" || approval.status === "APPROVED" ? <button type="button" className="text-button" disabled={actionBusy} onClick={() => void revokeExecution()}>Discard this approval</button> : null}
          </div>}
          {actionBusy && <p className="diff-progress" role="status">Waiting for the approved test operation to finish…</p>}
    {actionError && <p className="diff-error" role="alert">{actionError}</p>}
          {execution && <div className="test-run-result" aria-live="polite">
            <b>Test run {execution.status.toLowerCase()} · {execution.passed} passed · {execution.failed} failed · {execution.skipped} skipped</b>
            <details><summary>Run details</summary><code>Run {execution.run_id} · snapshot {execution.snapshot_id} · environment {execution.runtime_environment_id} · exit {execution.exit_code ?? "unavailable"}</code></details>
            {execution.runtime_validation && <small>Runtime mapping: {execution.runtime_validation.mapped_call_count}/{execution.runtime_validation.observed_call_count} calls mapped · {execution.runtime_validation.mismatch_count} mismatch(es)</small>}
            {execution.stdout && <details open={execution.failed > 0}><summary>Test output</summary><pre>{execution.stdout}</pre></details>}
            {execution.stderr && <details><summary>Standard error</summary><pre>{execution.stderr}</pre></details>}
            <p>Runtime observations were recorded for this run when available. Reopen or ask an investigation question to inspect the updated evidence.</p>
          </div>}
        </section>}
      </section>
    </div>}
  </section>;
}

function ChangedCode({ item }: { item: ChangedSymbol }) {
  const title = compactQualifiedName(item.target_qualified_name ?? item.base_qualified_name) ?? item.target_file_path ?? item.base_file_path ?? "File change";
  const label = item.classification === "UNMAPPED" ? "Needs review" : item.classification.toLowerCase().replaceAll("_", " ");
  return <li><div className="changed-code-heading"><b>{title}</b><span className={`diff-classification ${item.classification.toLowerCase().replaceAll("_", "-")}`}>{label}</span></div><small>{item.target_file_path ?? item.base_file_path}</small><p>{item.deterministic_reason.includes("Ambiguous same-signature") ? "Several code elements match this change; analysis could not pair them confidently." : item.deterministic_reason}</p><details><summary>Source evidence · {item.evidence.length}</summary>{item.evidence.map((evidence,index) => <div className="diff-evidence" key={index}><span>{evidence.file_path ?? "No direct source location"}{evidence.start_line ? `:${evidence.start_line}` : ""}</span>{evidence.entity_id && <EvidenceSource key={`${evidence.snapshot_id}-${evidence.entity_id}`} snapshotId={evidence.snapshot_id} entityId={evidence.entity_id}/>}<details><summary>Advanced analysis details</summary><code>{item.classification} · {evidence.evidence_type}</code><code>Snapshot {evidence.snapshot_id} · record {evidence.record_id} · source {evidence.source_record_id ?? "unavailable"} · hash {evidence.content_hash ?? "unavailable"}</code></details></div>)}</details></li>;
}

function compactQualifiedName(value: string | null | undefined) {
  if (!value) return value;
  const parts = value.split(".");
  return parts.slice(-2).join(".");
}
