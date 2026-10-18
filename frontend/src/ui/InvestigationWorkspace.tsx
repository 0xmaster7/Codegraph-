import { requestJson, RequestScope } from "./request.js";
import { API_BASE_URL } from "../apiConfig";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ArrowRight, LoaderCircle, MessageSquareText, ShieldCheck } from "lucide-react";
import { evidenceStateLabel, humanAnswerText, humanClaimType, humanRelationshipLabel, replayStateLabel } from "./evidencePresentation.js";
import { investigationSymbolMatches, questionForSymbol } from "./symbolMatches.js";

type Selection = { type: "entity" | "site" | "edge"; id: string };
export type EvidenceItem = {
  evidence_id: string;
  type: string;
  snapshot_id: string;
  source_record_id: string;
  display_label: string;
  provenance_pointer: string;
  review_state?: string;
  replay_outcome?: string | null;
  chunk_id?: string | null;
  chunk_kind?: string | null;
  source_node_id?: string | null;
  file_path?: string | null;
  content_hash?: string | null;
  text?: string | null;
};
type EvidenceRef = { item: EvidenceItem; occurrences: Array<{ originating_tool: string; uncertain: boolean; verified_graph_path: boolean | null }>; conflicting_identity: boolean };
type Claim = {
  claim_id: string;
  claim_type: string;
  subject_id: string;
  object_id: string;
  modality: string;
  status: string;
  reason: string;
  rendered_text: string | null;
  evidence: EvidenceRef[];
};
type PathNode = { canonical_id: string; label: string; kind: string };
type PathStep = { source_id: string; target_id: string; relation: string; modality: string; edge_ids: string[]; evidence_ids: string[]; provenance_ids: string[] };
type EvidencePath = { claim_id: string; snapshot_id: string; nodes: PathNode[]; steps: PathStep[]; hop_count: number; truncated: boolean };
type Barrier = { site_id: string; expression: string; file_path: string; start_line: number | null; resolution: string };
export type RuntimeCall = { snapshot_id: string; runtime_materialization_id: string | null; runtime_environment_id: string | null; test_invocation_id: string | null; run_id: string | null; observation_id: string; source_entity_id: string; target_entity_id: string; site_id: string | null; evidence_id: string };
type RuntimeObservation = { snapshot_id: string; test_definition_id: string; test_invocation_id: string | null; run_ids: string[]; entity_ids: string[]; observation_kinds: string[]; evidence_id: string | null; call_observations: RuntimeCall[] };
export type InvestigationResult = {
  investigation_id: string | null;
  repository_id: string;
  snapshot_id: string;
  query: { plan: { selected_node_id?: string | null; selected_edge_id?: string | null; selected_site_id?: string | null }; truncated: boolean; diagnostics: string[]; tool_executions: Array<{ request?: { query?: string | null } | null; result: { tool: string; results: Array<Record<string, unknown>> } }> };
  answer: { claims: Claim[]; limitations: string[]; rendered_text: string[] };
  evidence_paths: EvidencePath[];
  evidence_paths_truncated: boolean;
  unresolved_barriers: Barrier[];
  unresolved_barriers_truncated: boolean;
  runtime_observations: RuntimeObservation[];
  runtime_observations_truncated: boolean;
};
export type InvestigationSummary = { investigation_id: string; repository_id: string; snapshot_id: string; last_question: string; updated_at: number; note_count: number };
type InvestigationMemory = { investigation_id: string; snapshot_id: string; questions: Array<{ question: string }>; user_notes: Array<{ note_id: string; text: string }>; latest_result: InvestigationResult };

const API = API_BASE_URL;

async function request<T>(path: string, init: RequestInit): Promise<T> {
  return requestJson<T>(`${API}${path}`, init);
}

export function InvestigationWorkspace({
  snapshotId,
  selected,
  onInspect,
  onInspectPathEdges,
  onFocusEvidence,
  focusedEvidence,
  runtimeRefreshRunId,
  displayNames,
  displayContexts,
  pathEvidenceInfo,
  onRuntimeCalls,
  onEvidencePathChange,
  exampleQuestions,
  onSavedInvestigationsChange,
  requestedInvestigationId,
  onHistoryRequestHandled,
  graph,
}: {
  snapshotId: string;
  selected: Selection | null;
  onInspect: (selection: Selection) => void;
  onInspectPathEdges: (edgeIds: string[]) => void;
  onFocusEvidence: (evidence: EvidenceItem | null) => void;
  focusedEvidence: EvidenceItem | null;
  runtimeRefreshRunId: string | null;
  displayNames: Record<string, string>;
  displayContexts: Record<string, string>;
  pathEvidenceInfo: Record<string, { location: string; state: string }>;
  onRuntimeCalls: (calls: RuntimeCall[]) => void;
  onEvidencePathChange: (edgeIds: string[]) => void;
  exampleQuestions: string[];
  onSavedInvestigationsChange: (items: InvestigationSummary[]) => void;
  requestedInvestigationId: string | null;
  onHistoryRequestHandled: () => void;
  graph: ReactNode;
}) {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<InvestigationResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stage, setStage] = useState("");
  const [refreshedRunId, setRefreshedRunId] = useState<string | null>(null);
  const [investigationId, setInvestigationId] = useState<string | null>(null);
  const [savedInvestigations, setSavedInvestigations] = useState<InvestigationSummary[]>([]);
  const [notes, setNotes] = useState<Array<{ note_id: string; text: string }>>([]);
  const [noteDraft, setNoteDraft] = useState("");
  const [savingNote, setSavingNote] = useState(false);
  const operation = useRef(new RequestScope());
  const flight = useRef(false);
  useEffect(() => () => operation.current.cancel(), []);
  const lastRequest = useRef<Record<string, string> | null>(null);
  const pendingRefreshRunId = useRef<string | null>(null);
  const currentSnapshotId = useRef(snapshotId);
  currentSnapshotId.current = snapshotId;

  useEffect(() => {
    const calls = result?.snapshot_id === snapshotId
      ? result.runtime_observations.flatMap((observation) => observation.call_observations)
      : [];
    onRuntimeCalls(calls);
  }, [onRuntimeCalls, result, snapshotId]);
  useEffect(() => onSavedInvestigationsChange(savedInvestigations), [onSavedInvestigationsChange, savedInvestigations]);
  useEffect(() => {
    const edgeIds = result?.snapshot_id === snapshotId
      ? result.evidence_paths.flatMap((path) => path.steps.flatMap((step) => step.edge_ids))
      : [];
    onEvidencePathChange(edgeIds);
  }, [onEvidencePathChange, result, snapshotId]);

  useEffect(() => {
    lastRequest.current = null;
    pendingRefreshRunId.current = null;
    setQuestion(""); setResult(null); setError(""); setRefreshedRunId(null);
    setInvestigationId(null); setNotes([]); setNoteDraft(""); setSavedInvestigations([]);
    void request<InvestigationSummary[]>(`/api/investigations?snapshot_id=${encodeURIComponent(snapshotId)}`, {})
      .then((items) => { if (currentSnapshotId.current === snapshotId) setSavedInvestigations(items); })
      .catch((reason) => { if (currentSnapshotId.current === snapshotId) setError(reason instanceof Error ? reason.message : "Could not load saved investigations"); });
  }, [snapshotId]);

  async function openInvestigation(id: string) {
    if (!id || flight.current) return;
    flight.current = true;
    const task = operation.current.begin();
    setBusy(true); setError(""); setStage("Opening your saved question and evidence…");
    try {
      const memory = await request<InvestigationMemory>(`/api/investigations/${encodeURIComponent(id)}?snapshot_id=${encodeURIComponent(snapshotId)}`, { signal: task.signal });
      if (task.current() && currentSnapshotId.current === snapshotId) {
        setInvestigationId(memory.investigation_id);
        setResult(memory.latest_result);
        const lastQuestion = memory.questions.at(-1)?.question ?? "";
        setQuestion(lastQuestion);
        setNotes(memory.user_notes);
        const plan = memory.latest_result.query.plan;
        lastRequest.current = {
          snapshot_id: snapshotId,
          question: lastQuestion,
          investigation_id: memory.investigation_id,
          ...(plan.selected_node_id ? { selected_node_id: plan.selected_node_id } : {}),
          ...(plan.selected_edge_id ? { selected_edge_id: plan.selected_edge_id } : {}),
          ...(plan.selected_site_id ? { selected_site_id: plan.selected_site_id } : {}),
        };
        setRefreshedRunId(null);
      }
    } catch (reason) {
      if (task.current()) setError(reason instanceof Error ? reason.message : "Could not reopen this investigation");
    } finally { flight.current = false; if (task.current()) { setBusy(false); setStage(""); } }
  }

  async function saveNote(event: FormEvent) {
    event.preventDefault();
    if (!investigationId || !noteDraft.trim() || savingNote) return;
    setSavingNote(true); setError("");
    try {
      const memory = await request<InvestigationMemory>(`/api/investigations/${encodeURIComponent(investigationId)}/notes`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ snapshot_id: snapshotId, text: noteDraft.trim() }),
      });
      setNotes(memory.user_notes); setNoteDraft("");
      setSavedInvestigations((items) => items.map((item) => item.investigation_id === investigationId ? { ...item, note_count: memory.user_notes.length, updated_at: Date.now() / 1000 } : item));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not save this note"); }
    finally { setSavingNote(false); }
  }

  useEffect(() => {
    if (runtimeRefreshRunId && lastRequest.current) pendingRefreshRunId.current = runtimeRefreshRunId;
    const runId = pendingRefreshRunId.current;
    const payload = lastRequest.current;
    if (!runId || !payload || busy) return;
    pendingRefreshRunId.current = null;
    setBusy(true); setError(""); setStage("Updating the answer with the recorded test results…");
    void request<InvestigationResult>("/api/query/investigate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then((updated) => {
      if (updated.snapshot_id !== payload.snapshot_id) throw new Error("Investigation response crossed snapshot scope");
      if (currentSnapshotId.current === payload.snapshot_id) {
        setResult(updated); setInvestigationId(updated.investigation_id); setRefreshedRunId(runId);
        if (updated.investigation_id) lastRequest.current = { ...payload, investigation_id: updated.investigation_id };
      }
    }).catch((reason) => {
      setError(reason instanceof Error ? reason.message : "Could not refresh the investigation after test execution");
    }).finally(() => {
      setBusy(false); setStage("");
    });
  }, [runtimeRefreshRunId, busy]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    await ask(question);
  }

  async function ask(questionText: string) {
    if (!questionText.trim() || busy || flight.current) return;
    flight.current = true;
    const task = operation.current.begin();
    setQuestion(questionText);
    setBusy(true);
    setError("");
    setResult(null);
    setStage("Finding connections and checking the supporting source…");
    try {
      const payload: Record<string, string> = { snapshot_id: snapshotId, question: questionText.trim() };
      if (investigationId) payload.investigation_id = investigationId;
      if (selected?.type === "entity") payload.selected_node_id = selected.id;
      if (selected?.type === "site") payload.selected_site_id = selected.id;
      if (selected?.type === "edge") payload.selected_edge_id = selected.id;
      lastRequest.current = payload;
      setRefreshedRunId(null);
      const updated = await request<InvestigationResult>("/api/query/investigate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload), signal: task.signal,
      });
      if (updated.snapshot_id !== payload.snapshot_id) throw new Error("Investigation response crossed snapshot scope");
      if (task.current() && currentSnapshotId.current === payload.snapshot_id) {
        setResult(updated); setInvestigationId(updated.investigation_id);
        if (updated.investigation_id) lastRequest.current = { ...payload, investigation_id: updated.investigation_id };
        if (updated.investigation_id) {
          const summaries = await request<InvestigationSummary[]>(`/api/investigations?snapshot_id=${encodeURIComponent(snapshotId)}`, {});
          if (task.current()) setSavedInvestigations(summaries);
        }
      }
    } catch (reason) {
      if (task.current()) setError(reason instanceof Error ? reason.message : "Investigation failed");
    } finally {
      flight.current = false;
      if (task.current()) { setBusy(false); setStage(""); }
    }
  }

  useEffect(() => {
    if (!requestedInvestigationId) return;
    void openInvestigation(requestedInvestigationId).finally(onHistoryRequestHandled);
  }, [requestedInvestigationId, onHistoryRequestHandled, snapshotId]);

  const acceptedClaims = result?.answer.claims.filter((claim) => claim.status === "SUPPORTED" || claim.status === "QUALIFIED") ?? [];
  const pathsByClaim = new Map((result?.evidence_paths ?? []).map((path) => [path.claim_id, path]));
  const passages = acceptedClaims.filter((claim) => claim.claim_type === "PASSAGE_STATEMENT");
  const symbolMatches = investigationSymbolMatches(result?.query.tool_executions ?? []);
  const ambiguousMatches = symbolMatches.filter((match) => match.ambiguous);
  const definitionLookup = /^\s*where\b.*\bdefined\b/i.test(lastRequest.current?.question ?? question);

  return <section aria-busy={busy} className="investigation-workspace" aria-labelledby="investigation-title">
    <div className="investigation-heading">
      <div><span className="section-label">ASK YOUR CODE</span><h2 id="investigation-title">What would you like to understand?</h2><p>Find callers, follow a path, or understand a function — with source evidence.</p></div>
    </div>
    <details className="investigation-memory-controls"><summary>Reopen a saved question</summary><div>
      <label className="sr-only" htmlFor="saved-investigation">Saved investigations</label>
      <select id="saved-investigation" value={investigationId ?? ""} onChange={(event) => {
        const id = event.target.value;
        if (id) void openInvestigation(id);
        else { setInvestigationId(null); setResult(null); setNotes([]); setQuestion(""); lastRequest.current = null; }
      }} disabled={busy}>
        <option value="">New investigation</option>
        {savedInvestigations.map((item) => <option key={item.investigation_id} value={item.investigation_id}>{item.last_question} · {new Date(item.updated_at * 1000).toLocaleString()}</option>)}
      </select>
    </div></details>
    <form className="question-form" onSubmit={(event) => void submit(event)}>
      <label className="sr-only" htmlFor="investigation-question">Investigation question</label>
      <MessageSquareText size={16}/>
      <input id="investigation-question" maxLength={4096} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="How do these functions connect?" />
      <button type="submit" disabled={busy || !question.trim()}>{busy ? <><LoaderCircle className="spin" size={14}/> Working</> : <>Investigate <ArrowRight size={14}/></>}</button>
    </form>
    {!result && exampleQuestions.length > 0 && <div className="example-questions" aria-label="Example questions"><span>Try one</span>{exampleQuestions.map((example) => <button key={example} type="button" disabled={busy} onClick={() => void ask(example)}>{Object.entries(displayNames).filter(([id]) => !id.startsWith("ent_")).sort((a, b) => b[0].length - a[0].length).reduce((text, [id, name]) => text.replaceAll(id, name), example)}<ArrowRight size={13}/></button>)}</div>}
    {selected && <div className="query-context"><span>Follow-up scope:</span><span>{selected.type === "edge" ? "selected relationship" : selected.type === "entity" ? "selected code element" : "selected call site"} from the Inspector</span></div>}
    {busy && <><p className="query-progress" role="status"><LoaderCircle className="spin" size={14}/>{stage}</p><div className="answer-skeleton" aria-hidden="true"><div/><div/><div/></div></>}
    {error && <div className="investigation-error" role="alert">{error}<button className="text-button" disabled={busy} onClick={() => void ask(question)}>Try again</button></div>}
    {result && <div className="investigation-results" aria-live="polite">
      <div className="result-meta"><b>Answer</b><span>{result.query.truncated ? "Results are bounded; see limitations" : "Based on this saved version of the code"}</span>{refreshedRunId && <span role="status">Answer refreshed after a test run</span>}</div>
      {acceptedClaims.length > 0 ? <div className="claim-list">{acceptedClaims.map((claim) => {
        const path = pathsByClaim.get(claim.claim_id);
        const isPassage = claim.claim_type === "PASSAGE_STATEMENT";
        return <article className={`claim-card ${isPassage ? "passage-claim" : ""}`} key={claim.claim_id}>
          <div className="claim-card-top"><span className="claim-status">{claim.status === "QUALIFIED" ? "Evidence has limitations" : claim.modality === "EXACT" ? "Identified in code" : claim.modality === "DERIVED" ? "Inferred from code relationships" : claim.modality === "MAY" ? "Possible connection" : `${claim.modality} passage`}</span><span className="claim-kind">{humanClaimType(claim.claim_type)}</span></div>
          <p className="claim-text">{claim.rendered_text ? Object.entries(displayNames).filter(([id]) => claim.rendered_text!.includes(id)).sort((a,b) => b[0].length - a[0].length).reduce((text, [id, label]) => text.replaceAll(id, label), humanAnswerText(claim.rendered_text)) : null}</p>
          {path && <EvidencePathView path={path} evidence={claim.evidence} displayNames={displayNames} displayContexts={displayContexts} pathEvidenceInfo={pathEvidenceInfo} onInspect={onInspect} onInspectPathEdges={onInspectPathEdges}/> }
          <details className="claim-evidence"><summary>More supporting evidence · {claim.evidence.length}</summary><div className="claim-evidence-list">{claim.evidence.map((ref) => <article className="evidence-ref" key={`${claim.claim_id}-${ref.item.evidence_id}`}><button className="evidence-link" onClick={() => {
            if (["STATIC_BINDING", "STATIC_DERIVATION", "STATIC_STRUCTURE"].includes(ref.item.type)) onInspect({ type: "edge", id: ref.item.source_record_id });
            else onFocusEvidence(ref.item);
          }}><b>{ref.item.type.replaceAll("_", " ").toLowerCase()}</b><span>{ref.item.display_label}</span>{ref.item.file_path && <small>{ref.item.file_path}</small>}</button><details><summary>Technical details</summary><code>Evidence {ref.item.evidence_id}</code><code>Source record {ref.item.source_record_id}</code><code>Provenance {ref.item.provenance_pointer}</code></details></article>)}</div></details>
        </article>;
      })}</div> : <div className="no-claims">{ambiguousMatches.length > 0 ? "This name matches multiple definitions. Choose the function you mean below." : definitionLookup && symbolMatches.length > 0 ? "Found the definition in this saved version. Open its source below." : symbolMatches.length > 0 ? "The symbol was found, but no supported connections were returned. This does not prove there are no callers or callees; see the analysis limits below." : "No supported answer was found for this question. Check that the function exists in the selected repository snapshot; see the analysis limits below."}</div>}
      {symbolMatches.length > 0 && (ambiguousMatches.length > 0 || acceptedClaims.length === 0) && <section className="symbol-matches" aria-label="Matching definitions">
        <b>{ambiguousMatches.length > 0 ? "Which definition did you mean?" : "Matching definitions"}</b>
        {ambiguousMatches.length > 0 && <p>Select a qualified name to rerun your question without guessing.</p>}
        {symbolMatches.map((match) => <button key={match.id} type="button" disabled={busy} onClick={() => match.ambiguous ? void ask(questionForSymbol(lastRequest.current?.question ?? question, match)) : onInspect({ type: "entity", id: match.id })}>
          <strong>{match.qualifiedName}</strong><small>{match.filePath} · {match.ambiguous ? "Use this definition" : "Open source"}</small>
        </button>)}
      </section>}
      {passages.length > 0 && <section className="passage-panel"><div><h3>Retrieved passages</h3><p>Text evidence is separate from structural graph relationships.</p></div>{passages.flatMap((claim) => claim.evidence.map((ref) => <button className="passage-record" key={ref.item.evidence_id} onClick={() => onFocusEvidence(ref.item)}><span>{ref.item.type.replaceAll("_", " ").toLowerCase()} · {ref.item.file_path}</span><q>{ref.item.text}</q><details><summary>Technical details</summary><code>{ref.item.evidence_id}</code></details></button>))}</section>}
      {result.runtime_observations.length > 0 && <details className="runtime-panel"><summary>Runtime observations · {result.runtime_observations.reduce((count, record) => count + record.call_observations.length, 0)} calls recorded</summary><div><p>These calls were observed during the linked test run; they are not static proof.</p>{result.runtime_observations.map((record) => <article key={`${record.test_definition_id}-${record.test_invocation_id}-${record.run_ids.join(",")}`}><b>Observed test run</b>{record.call_observations.map((call) => <div className="runtime-call-pair" key={call.observation_id}><span>Runtime observation · {displayNames[call.source_entity_id] ?? "Code element"} → {displayNames[call.target_entity_id] ?? "Code element"}</span>{call.site_id && <button onClick={() => onInspect({ type: "site", id: call.site_id! })}>Inspect call site</button>}<details><summary>Technical details</summary><code>Run {call.run_id ?? "unknown"} · invocation {call.test_invocation_id ?? "unknown"} · materialization {call.runtime_materialization_id ?? "unavailable"}</code><code>Observation {call.observation_id} · evidence {call.evidence_id} · snapshot {call.snapshot_id}</code></details></div>)}</article>)}</div></details>}
      {focusedEvidence && <section className="focused-evidence"><div><h3>Source evidence</h3><button onClick={() => onFocusEvidence(null)}>Close</button></div><b>{focusedEvidence.type.replaceAll("_", " ").toLowerCase()}</b><span>{focusedEvidence.display_label}</span>{focusedEvidence.file_path && <span>{focusedEvidence.file_path}</span>}{focusedEvidence.replay_outcome && <span>{replayStateLabel(focusedEvidence.replay_outcome)}</span>}{focusedEvidence.text && <blockquote>{focusedEvidence.text}</blockquote>}<details><summary>Technical details</summary><code>Evidence {focusedEvidence.evidence_id}</code><code>Snapshot {focusedEvidence.snapshot_id}</code><code>Source record {focusedEvidence.source_record_id}</code><code>Provenance {focusedEvidence.provenance_pointer}</code>{focusedEvidence.content_hash && <code>Content hash {focusedEvidence.content_hash}</code>}</details></section>}
    </div>}
    {graph}
    {result && <>
      <details className="investigation-memory-notes"><summary>Investigation notes <span>Workspace annotations stay separate from graph facts.</span></summary><div className="investigation-memory-note-content">{notes.map((note) => <p className="investigation-memory-note" key={note.note_id}>{note.text}</p>)}<form onSubmit={(event) => void saveNote(event)}><label className="sr-only" htmlFor="investigation-note">Add a note</label><textarea id="investigation-note" maxLength={4000} value={noteDraft} onChange={(event) => setNoteDraft(event.target.value)} placeholder="Add an annotation for this investigation…"/><button type="submit" disabled={savingNote || !noteDraft.trim()}>{savingNote ? "Saving" : "Save note"}</button></form></div></details>
      {(result.answer.limitations.length > 0 || result.query.diagnostics.length > 0 || result.unresolved_barriers.length > 0 || result.evidence_paths_truncated || result.unresolved_barriers_truncated || result.runtime_observations_truncated) && <details className="limitations-panel"><summary>Limits, unresolved calls, and diagnostics</summary><div className="limitations-content"><ul>{[...new Set([...result.answer.limitations, ...result.query.diagnostics, ...(result.evidence_paths_truncated ? ["Evidence paths were truncated at the display limit."] : []), ...(result.unresolved_barriers_truncated ? ["Unresolved barriers were truncated at the display limit."] : []), ...(result.runtime_observations_truncated ? ["Runtime observations were truncated at the display limit."] : [])])].map((item) => <li key={item}>{item}</li>)}</ul>{result.unresolved_barriers.length > 0 && <div className="barrier-list"><b>Unresolved call barriers</b>{result.unresolved_barriers.map((barrier) => <button key={barrier.site_id} onClick={() => onInspect({ type: "site", id: barrier.site_id })}><span>{barrier.expression}</span><small>{barrier.file_path}{barrier.start_line ? `:${barrier.start_line}` : ""} · Target unresolved</small><details><summary>Technical details</summary><code>Call site {barrier.site_id} · {barrier.resolution}</code></details></button>)}</div>}</div></details>}
    </>}
  </section>;
}

function replayEligibility(outcome: string) {
  if (outcome === "EDGE_NOT_REPRODUCED") return "Replay mismatch · excluded from exact paths";
  if (outcome === "SOURCE_HASH_MISMATCH") return "Source changed · excluded from exact paths";
  if (outcome === "PASS") return "Replay verified";
  if (outcome === "NOT_RUN") return "Replay not run";
  return "Replay inconclusive · original evidence retained";
}

function EvidencePathView({ path, evidence, displayNames, displayContexts, pathEvidenceInfo, onInspect, onInspectPathEdges }: { path: EvidencePath; evidence: EvidenceRef[]; displayNames: Record<string, string>; displayContexts: Record<string, string>; pathEvidenceInfo: Record<string, { location: string; state: string }>; onInspect: (selection: Selection) => void; onInspectPathEdges: (edgeIds: string[]) => void }) {
  return <section className="evidence-path" aria-label="Evidence path">
    <div className="path-heading"><b>How the code connects</b><span>{path.hop_count} {path.hop_count === 1 ? "step" : "steps"}</span>{path.truncated && <strong>More paths are available</strong>}</div>
    <ol className="path-steps compact-path">
      {path.nodes.map((node, index) => {
        const name = displayNames[node.canonical_id] ?? displayNames[node.label] ?? node.label.split(".").at(-1) ?? "Code element";
        const context = displayContexts[node.canonical_id] ?? node.kind;
        const step = path.steps[index];
        return <li className="path-node-row" key={`${path.claim_id}-${node.canonical_id}-${index}`}>
          <button className="path-node" title={context} onClick={() => onInspect({ type: "entity", id: node.canonical_id })}><b>{name}</b><small>{context}</small></button>
          {step && <button className={`path-source-link ${step.modality.toLowerCase()}`} title={evidenceStateLabel({ modality: step.modality })} onClick={() => onInspectPathEdges(step.edge_ids)}><span>{humanRelationshipLabel(step.relation, step.modality)} →</span><small>View source</small></button>}
        </li>;
      })}
    </ol>
    <details className="path-more"><summary>Connection details</summary>{path.steps.map((step, index) => <div className="path-step-details" key={index}><b>{humanRelationshipLabel(step.relation, step.modality)}</b><p>{evidenceStateLabel({ modality: step.modality })}</p>{step.edge_ids.map((id) => pathEvidenceInfo[id] && <p key={id}>{pathEvidenceInfo[id].location} · {pathEvidenceInfo[id].state}</p>)}<button className="text-button" onClick={() => onInspectPathEdges(step.edge_ids)}>View source evidence <ArrowRight size={12}/></button><details className="path-technical"><summary>Advanced analysis details</summary><code>Relationship · {step.relation} · {step.modality}</code><code>Edge IDs · {step.edge_ids.join(", ") || "unavailable"}</code><code>Evidence IDs · {step.evidence_ids.join(", ") || "unavailable"}</code><code>Provenance IDs · {step.provenance_ids.join(", ") || "unavailable"}</code></details></div>)}<details className="path-technical"><summary>Code identities</summary>{path.nodes.map((node,index) => <code key={index}>{node.label} · {node.canonical_id}</code>)}</details></details>
  </section>;
}
