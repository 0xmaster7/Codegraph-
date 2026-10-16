import { API_BASE_URL } from "../apiConfig";
import { selectEvidencePathEdge } from "./graphPathSelection.js";
import { graphViewportHeight, relationshipNodeY, unresolvedSitePosition } from "./graphLayout.js";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowUpRight,
  Boxes,
  Braces,
  Check,
  ChevronRight,
  Copy,
  FileCode2,
  GitCommitHorizontal,
  GitCompareArrows,
  History,
  Link2,
  LoaderCircle,
  MessageSquareText,
  Play,
  RefreshCw,
  Search,
  ShieldCheck,
  Waypoints,
  X,
} from "lucide-react";
import { InvestigationWorkspace, type EvidenceItem, type InvestigationSummary, type RuntimeCall } from "./InvestigationWorkspace";
import { DiffPlanningWorkspace } from "./DiffPlanningWorkspace";
import { canOpenWorkspace, canUseValidatedSnapshot, workspaceNavigation, workspaceTitle, type WorkspacePage } from "./workspaceNavigation.js";
import { evidenceStateLabel, humanDispatchState, humanExternalName, humanRelationshipLabel, humanRepositoryName, humanSymbolName, replayStateLabel } from "./evidencePresentation.js";

type Span = { start_byte: number; end_byte: number; start_line: number; start_column: number; end_line: number; end_column: number };
type Entity = { entity_id: string; kind: string; name: string; qualified_name: string; file_path: string; span: Span; parent_id: string | null };
type Site = { site_id: string; kind: string; expression: string; file_path: string; span: Span; context_id: string; syntax_node_type: string };
type Provenance = { provenance_id: string; edge_id: string; snapshot_id: string; file_path: string; file_content_hash: string; span: Span; syntax_node_type: string; extractor_name: string; extractor_version: string; rule_id: string; premise_edge_ids: string[]; replay_scope: string; replay_state: string; review_state: string };
type Binding = { binding_id: string; edge_id: string; site_id: string; target_entity_id: string | null; external_symbol_id: string | null; scip_symbol: string; occurrence_start_byte: number; binding_state: string; resolver: string };
type DerivedEdge = { edge_id: string; relation: string; source_id: string; target_id: string; rule_id: string; premise_edge_ids: string[] };
type Dispatch = { site_id: string; state: string; candidate_entity_ids: string[]; mro_entity_ids: string[]; mro_qualified_names: string[] };
type Diagnostic = { code: string; message: string; file_path?: string; span?: Span };
type Analysis = {
  repository: { repository_id: string; root: string; head_commit: string | null; is_dirty: boolean; is_shallow: boolean; is_detached: boolean };
  snapshot_id: string; status: string; counts: Record<string, number>; entities: Entity[]; sites: Site[];
  provenance: Provenance[]; bindings: Binding[]; derived_edges: DerivedEdge[]; dispatch: Dispatch[];
  external_symbols: Array<{ external_symbol_id: string; semantic_symbol: string; module_name?: string; distribution_name?: string; distribution_version?: string; external_kind: string }>;
  diagnostics: Diagnostic[]; semantic_indexing: string; analysis_environment: { environment_id: string; python_version: string; scip_python_version: string; scip_cli_version: string } | null;
};
type SnapshotRow = { snapshot_id: string; repository_id: string; repository_root?: string; status: string; head_commit?: string; updated_at?: number; ready_at?: number; is_dirty?: boolean };
type Job = { job_id: string; snapshot_id: string; status: string; attached?: boolean; cancel_requested?: boolean; error?: string | null };
type SourcePreview = { file_path: string; file_content_hash: string; start_byte: number; end_byte: number; start_line: number; start_column: number; end_line: number; end_column: number; text: string };
type Selection = { type: "entity" | "site" | "edge"; id: string };
type GraphEndpoint = { id: string; label: string; detail: string; type: "entity" | "site" | "external" };
type GraphRelation = { id: string; source: GraphEndpoint; target: GraphEndpoint; label: string; state: string; visualClass: string; file: string; selection: Selection };
type GraphLooseSite = { id: string; label: string; detail: string; file: string; selection: Selection };
type ReviewItem = { key: string; title: string; detail: string; file?: string; line?: number; selection?: Selection; state: string };
type NeighborResult = { results?: Array<Record<string, unknown>>; candidate_count_before_limit?: number; mode?: string; truncated?: boolean };

const API = API_BASE_URL;

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, init);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail ?? "The local CodeGraph API request failed");
  return data as T;
}

export default function App() {
  const [path, setPath] = useState("");
  const [snapshots, setSnapshots] = useState<SnapshotRow[]>([]);
  const [snapshotId, setSnapshotId] = useState("");
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [page, setPage] = useState<WorkspacePage>("investigate");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [source, setSource] = useState<SourcePreview | null>(null);
  const [neighbors, setNeighbors] = useState<NeighborResult | null>(null);
  const [showPremises, setShowPremises] = useState(false);
  const [copied, setCopied] = useState(false);
  const [queueFilter, setQueueFilter] = useState("ALL");
  const [focusedEvidence, setFocusedEvidence] = useState<EvidenceItem | null>(null);
  const [runtimeRefreshRunId, setRuntimeRefreshRunId] = useState<string | null>(null);
  const [runtimeCalls, setRuntimeCalls] = useState<RuntimeCall[]>([]);
  const [highlightedPathEdgeIds, setHighlightedPathEdgeIds] = useState<string[]>([]);
  const [savedInvestigations, setSavedInvestigations] = useState<InvestigationSummary[]>([]);
  const [historyRequestId, setHistoryRequestId] = useState<string | null>(null);
  const startupSnapshotChecked = useRef(false);
  const handleRuntimeCalls = useCallback((calls: RuntimeCall[]) => setRuntimeCalls(calls), []);
  const handleEvidencePathChange = useCallback((edgeIds: string[]) => setHighlightedPathEdgeIds(edgeIds), []);
  const handleSavedInvestigations = useCallback((items: InvestigationSummary[]) => setSavedInvestigations(items), []);
  const handleHistoryRequestOpened = useCallback(() => setHistoryRequestId(null), []);
  const inspectEvidencePathEdges = useCallback((edgeIds: string[]) => {
    const structuralEdgeIds = new Set([
      ...(analysis?.bindings.map((item) => item.edge_id) ?? []),
      ...(analysis?.derived_edges.map((item) => item.edge_id) ?? []),
    ]);
    const provenanceEdgeIds = new Set(analysis?.provenance.map((item) => item.edge_id) ?? []);
    const edgeId = selectEvidencePathEdge(edgeIds, structuralEdgeIds, provenanceEdgeIds);
    if (edgeId) { setSelection({ type: "edge", id: edgeId }); setPage("graph"); }
  }, [analysis]);

  async function refreshSnapshots() {
    try {
      const rows = (await api<{ snapshots: SnapshotRow[] }>("/api/snapshots?limit=50")).snapshots;
      setSnapshots(rows);
      if (!startupSnapshotChecked.current) {
        startupSnapshotChecked.current = true;
        const ready = rows.find((item) => item.status === "READY");
        if (ready) void openSnapshot(ready.snapshot_id);
      }
    }
    catch { /* Health and repository inspection remain usable if the graph store is unavailable. */ }
  }

  useEffect(() => { void refreshSnapshots(); }, []);

  async function startIngestion() {
    if (!path.trim() || busy) return;
    setBusy(true); setError(""); setNotice(""); setAnalysis(null); setSelection(null); setSource(null); setJob(null);
    try {
      const started = await api<Job>("/api/snapshots/ingest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: path.trim(), include_untracked: false }) });
      setSnapshotId(started.snapshot_id); setJob(started);
      let current = started;
      while (!["COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"].includes(current.status)) {
        await delay(900);
        const status = await api<{ snapshot: Record<string, unknown> }>(`/api/snapshots/${encodeURIComponent(started.snapshot_id)}`);
        const metadata = status.snapshot;
        current = { ...current, status: String(metadata.job_status ?? metadata.status ?? current.status), error: metadata.error ? String(metadata.error) : null };
        setJob(current);
        if (["READY", "COMPLETED"].includes(current.status) || metadata.status === "READY") {
          current = { ...current, status: "COMPLETED" }; setJob(current); break;
        }
      }
      if (["FAILED", "CANCELLED", "INTERRUPTED"].includes(current.status)) throw new Error(current.error ?? `Ingestion ${current.status.toLowerCase()}`);
      const [finalStatus, loaded] = await Promise.all([
        api<{ snapshot: Record<string, unknown> }>(`/api/snapshots/${encodeURIComponent(started.snapshot_id)}`),
        api<Analysis>(`/api/snapshots/${encodeURIComponent(started.snapshot_id)}/analysis`),
      ]);
      setAnalysis({ ...loaded, status: String(finalStatus.snapshot.status ?? loaded.status) }); setPage("investigate"); await refreshSnapshots();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not start repository ingestion"); }
    finally { setBusy(false); }
  }

  async function cancelIngestion() {
    if (!job || !snapshotId) return;
    try { setJob(await api<Job>(`/api/snapshots/${encodeURIComponent(snapshotId)}/cancel`, { method: "POST" })); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not cancel ingestion"); }
  }

  async function openSnapshot(id: string) {
    setError(""); setNotice(""); setAnalysis(null); setSnapshotId(id); setSelection(null); setSource(null); setRuntimeRefreshRunId(null); setBusy(true);
    try {
      const [status, loaded] = await Promise.all([
        api<{ snapshot: Record<string, unknown> }>(`/api/snapshots/${encodeURIComponent(id)}`),
        api<Analysis>(`/api/snapshots/${encodeURIComponent(id)}/analysis`),
      ]);
      const snapshotStatus = String(status.snapshot.status ?? loaded.status);
      setAnalysis({ ...loaded, status: snapshotStatus });
      if (snapshotStatus !== "READY") setError(`This snapshot is ${snapshotStatus.toLowerCase()} and is kept out of normal graph queries.`);
      setPage("investigate");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not load this snapshot"); }
    finally { setBusy(false); }
  }

  const selectedEntity = selection?.type === "entity" ? analysis?.entities.find((item) => item.entity_id === selection.id) : undefined;
  const selectedSite = selection?.type === "site" ? analysis?.sites.find((item) => item.site_id === selection.id) : undefined;
  const selectedProvenance = selection?.type === "edge" ? analysis?.provenance.find((item) => item.edge_id === selection.id) : undefined;
  const selectedBinding = analysis && selectedProvenance
    ? analysis.bindings.find((item) => item.edge_id === selectedProvenance.edge_id)
    : analysis && selection?.type === "site"
      ? analysis.bindings.find((item) => item.site_id === selection.id)
      : undefined;
  const selectedDerived = selectedProvenance && analysis ? analysis.derived_edges.find((item) => item.edge_id === selectedProvenance.edge_id) : undefined;
  const selectedSiteForEdge = selectedBinding && analysis ? analysis.sites.find((item) => item.site_id === selectedBinding.site_id) : undefined;
  const relatedEdgeIds = analysis && selection?.type === "entity"
    ? [
        ...analysis.bindings.filter((item) => item.target_entity_id === selection.id).map((item) => item.edge_id),
        ...analysis.derived_edges.filter((item) => item.source_id === selection.id || item.target_id === selection.id).map((item) => item.edge_id),
      ]
    : analysis && selection?.type === "site"
      ? analysis.bindings.filter((item) => item.site_id === selection.id).map((item) => item.edge_id)
      : [];
  const relatedProvenance = analysis && selection?.type !== "edge"
    ? analysis.provenance.filter((item) => relatedEdgeIds.includes(item.edge_id))
    : [];
  const selected = selectedEntity ?? selectedSite ?? selectedProvenance;
  const selectedPath = selectedEntity?.file_path ?? selectedSite?.file_path ?? selectedProvenance?.file_path;
  const selectedSpan = selectedEntity?.span ?? selectedSite?.span ?? selectedProvenance?.span;

  useEffect(() => {
    setSource(null); setNeighbors(null); setShowPremises(false);
    if (!analysis || !selection || !selectedPath || !selectedSpan) return;
    const params = new URLSearchParams({ file_path: selectedPath, start_byte: String(selectedSpan.start_byte), end_byte: String(selectedSpan.end_byte) });
    void api<SourcePreview>(`/api/snapshots/${encodeURIComponent(analysis.snapshot_id)}/source?${params.toString()}`).then(setSource).catch((reason) => setError(reason instanceof Error ? reason.message : "Source preview is unavailable"));
  }, [analysis, selection, selectedPath, selectedSpan]);

  const reviewItems = useMemo(() => buildReviewQueue(analysis), [analysis]);
  const exampleQuestions = useMemo(() => {
    if (!analysis) return [];
    const callable = analysis.entities.find((item) => ["FUNCTION", "METHOD"].includes(item.kind));
    if (!callable) return [];
    return [
      `Where is ${callable.qualified_name} defined?`,
      `Who calls ${callable.qualified_name}?`,
      `What does ${callable.qualified_name} call?`,
    ];
  }, [analysis]);

  function startNewRepository() {
    startupSnapshotChecked.current = true;
    setAnalysis(null); setSnapshotId(""); setSelection(null); setSource(null); setFocusedEvidence(null); setPage("investigate");
  }

  async function replayEdge(edgeId: string) {
    if (!analysis) return;
    setError(""); setNotice("");
    try {
      const result = await api<{ message: string }>(`/api/evidence/${encodeURIComponent(edgeId)}/replay`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ snapshot_id: analysis.snapshot_id }) });
      setAnalysis(await api<Analysis>(`/api/snapshots/${encodeURIComponent(analysis.snapshot_id)}/analysis`));
      setNotice(result.message);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Replay failed"); }
  }

  async function loadNeighbors() {
    if (!analysis || !selection) return;
    setError(""); setNeighbors(null);
    try {
      if (selection.type === "site") setNeighbors(await api<NeighborResult>(`/api/snapshots/${analysis.snapshot_id}/graph/callees?site_id=${encodeURIComponent(selection.id)}`));
      else if (selection.type === "entity") setNeighbors(await api<NeighborResult>(`/api/snapshots/${analysis.snapshot_id}/graph/callers?target_id=${encodeURIComponent(selection.id)}`));
      else if (selectedBinding?.target_entity_id) setNeighbors(await api<NeighborResult>(`/api/snapshots/${analysis.snapshot_id}/graph/callers?target_id=${encodeURIComponent(selectedBinding.target_entity_id)}`));
      else if (selectedDerived?.target_id) setNeighbors(await api<NeighborResult>(`/api/snapshots/${analysis.snapshot_id}/graph/callers?target_id=${encodeURIComponent(selectedDerived.target_id)}`));
      else setNeighbors({ results: [] });
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Neighbor query failed"); }
  }

  async function copyDebugId() {
    if (!selection) return;
    await navigator.clipboard.writeText(selection.id); setCopied(true); window.setTimeout(() => setCopied(false), 1400);
  }

  const displayNames = Object.fromEntries([
    ...((analysis?.entities ?? []).flatMap((entity) => {
      const parent = analysis?.entities.find((item) => item.entity_id === entity.parent_id);
      const name = humanSymbolName(entity.name, entity.kind);
      const contextName = parent?.kind === "Class" ? `${parent.name}.${name}` : `${entity.qualified_name.split(".").slice(-2, -1)[0] ?? ""}.${name}`;
      return [[entity.entity_id, name], [entity.qualified_name, contextName]] as const;
    })),
    ...((analysis?.external_symbols ?? []).map((symbol) => {
      return [symbol.external_symbol_id, humanExternalName(symbol.semantic_symbol, symbol.distribution_name, symbol.module_name)] as const;
    })),
  ]);
  const displayContexts = Object.fromEntries((analysis?.entities ?? []).map((entity) => {
    const parent = analysis?.entities.find((item) => item.entity_id === entity.parent_id);
    return [entity.entity_id, `${parent?.kind === "Class" ? parent.name : entity.qualified_name.split(".").slice(-2, -1)[0] ?? "Code"} · ${entity.file_path}:${entity.span.start_line}`];
  }));
  const pathEvidenceInfo = Object.fromEntries((analysis?.provenance ?? []).map((record) => {
    const binding = analysis?.bindings.find((item) => item.edge_id === record.edge_id);
    const derived = analysis?.derived_edges.find((item) => item.edge_id === record.edge_id);
    const modality = binding?.binding_state ?? derived?.relation ?? "EXACT";
    return [record.edge_id, { location: `${record.file_path}:${record.span.start_line}`, state: evidenceStateLabel({ modality, replayState: record.replay_state, reviewState: record.review_state }) }];
  }));
  const repositorySnapshot = snapshots.find((item) => item.snapshot_id === analysis?.snapshot_id) ?? snapshots.find((item) => item.repository_id === analysis?.repository.repository_id);
  const repositoryName = humanRepositoryName(repositorySnapshot?.repository_root ?? analysis?.repository.root);
  const navIcon = { investigate: <MessageSquareText size={17}/>, graph: <Waypoints size={17}/>, changes: <GitCompareArrows size={17}/>, history: <History size={17}/> };

  return <main className="product-shell">
    <aside className="product-sidebar">
      <div className="product-brand"><span className="product-brand-mark"><Braces size={18}/></span><span><b>CodeGraph</b><small>Python code investigation</small></span></div>
      <nav className="product-navigation" aria-label="Primary navigation">
        <span className="nav-caption">WORKSPACE</span>
        {workspaceNavigation.map((item) => <button key={item.id} className={page === item.id ? "active" : ""} aria-current={page === item.id ? "page" : undefined} disabled={!canOpenWorkspace(item.id, !!analysis)} onClick={() => setPage(item.id)}>{navIcon[item.id]}<span>{item.label}</span>{item.id === "history" && <small>{snapshots.length}</small>}</button>)}
      </nav>
      <div className="sidebar-footer"><span className="local-indicator"/><span>Local workspace</span></div>
    </aside>
    <section className="product-main">
      <header className="product-header">
        <div className="header-page"><span>CODEGRAPH</span><h1>{workspaceTitle(page)}</h1></div>
        <div className="header-context">
          {analysis ? <>
            <label className="sr-only" htmlFor="active-snapshot">Repository and snapshot</label>
            <select id="active-snapshot" value={snapshotId} onChange={(event) => void openSnapshot(event.target.value)} aria-label="Switch repository snapshot">
              {snapshots.some((item) => item.snapshot_id === analysis.snapshot_id) ? null : <option value={analysis.snapshot_id}>{repositoryName} · {analysis.repository.head_commit?.slice(0, 7) ?? "uncommitted"}</option>}
              {snapshots.map((item) => <option key={item.snapshot_id} value={item.snapshot_id}>{item.repository_root?.split("/").filter(Boolean).at(-1) ?? "Repository"} · {item.head_commit?.slice(0, 7) ?? "snapshot"} · {item.status.toLowerCase()}</option>)}
            </select>
            <span className={`context-status ${analysis.status.toLowerCase()}`}><i/>{analysis.status.toLowerCase()}</span>
            <button className="header-secondary-action" onClick={startNewRepository}>Add repository</button>
          </> : <><span className="context-placeholder">Local Python workspace</span>{snapshots.length > 0 && <select value={snapshotId} onChange={(event) => void openSnapshot(event.target.value)} aria-label="Open repository snapshot"><option value="">Choose a recent snapshot</option>{snapshots.map((item) => <option key={item.snapshot_id} value={item.snapshot_id}>{item.repository_root?.split("/").filter(Boolean).at(-1) ?? "Repository"} · {item.status.toLowerCase()}</option>)}</select>}</>}
        </div>
      </header>
      <div className={`product-content ${analysis && selection && (page === "investigate" || page === "graph") ? "has-inspector" : ""}`}>
        {!analysis ? page === "history" ? <div className="workspace-view history-view">
          <div className="view-introduction"><div><span className="section-label">SAVED WORK · LOCAL SNAPSHOTS</span><h2>History</h2><p>Open a previously analyzed repository snapshot or start a new analysis.</p></div><button className="header-secondary-action" onClick={startNewRepository}>Analyze a repository</button></div>
          <section className="history-section"><div className="history-section-heading"><h3>Repository snapshots</h3><button className="text-button" onClick={() => void refreshSnapshots()}><RefreshCw size={14}/> Refresh</button></div>{snapshots.length ? <SnapshotList snapshots={snapshots} selectedId={snapshotId} onSelect={(id) => void openSnapshot(id)} onRefresh={() => void refreshSnapshots()}/> : <p className="history-empty">No analyzed snapshots are available yet. Start with a local Python repository to create one.</p>}</section>
          <section className="history-section"><div className="history-section-heading"><h3>Saved investigations</h3><span>0</span></div><p className="history-empty">Saved questions appear here after you open a repository and run an investigation.</p></section>
        </div> : <section className="repository-start">
          <div className="start-intro"><span className="section-label">START WITH YOUR CODE</span><h2>Ask about your Python code. Follow every answer to its evidence.</h2><p>CodeGraph builds a versioned code graph from a local Git repository. Ask how code connects, inspect the answer in a graph, then review relevant tests before approving a run.</p>
            <ol className="workflow-summary"><li><span>1</span><div><b>Analyze a repository</b><small>Create a source-bound snapshot on this machine.</small></div></li><li><span>2</span><div><b>Investigate a question</b><small>Get an answer with a traceable path through code.</small></div></li><li><span>3</span><div><b>Review changes and tests</b><small>See impact and approve any sandboxed execution.</small></div></li></ol>
          </div>
          <section className="repository-form-panel" aria-labelledby="repository-form-title">
            <div><span className="section-label">LOCAL REPOSITORY</span><h3 id="repository-form-title">Analyze a Python repository</h3><p>Enter the absolute path to a Git checkout available to this machine.</p></div>
            <form className="repository-form" onSubmit={(event) => { event.preventDefault(); void startIngestion(); }}>
              <label htmlFor="repository-path">Repository path</label><div className="repository-path-input"><Search size={16}/><input id="repository-path" value={path} onChange={(event) => setPath(event.target.value)} placeholder="/path/to/python-project" autoComplete="off" spellCheck={false}/></div>
              <button type="submit" disabled={!path.trim() || busy}>{busy ? <><LoaderCircle className="spin" size={15}/>{job?.status?.replaceAll("_", " ") ?? "Starting analysis"}</> : <>Analyze repository <ArrowUpRight size={16}/></>}</button>
            </form>
            <div className="repository-form-foot"><span><ShieldCheck size={14}/> Source stays on this machine</span><span>Python · Git history · manifests</span></div>
            {busy && job && <div className="job-strip"><span className="status-pip"/><b>{job.status.replaceAll("_", " ")}</b><span>Analysis is preparing a validated snapshot.</span>{job.cancel_requested ? <span>Cancellation requested</span> : <button className="text-button danger" onClick={() => void cancelIngestion()}><X size={13}/> Cancel</button>}</div>}
            {snapshots.length > 0 && <div className="available-snapshots"><b>Or open a recent snapshot</b><SnapshotList snapshots={snapshots.slice(0, 4)} selectedId={snapshotId} onSelect={(id) => void openSnapshot(id)} onRefresh={() => void refreshSnapshots()} compact/> </div>}
          </section>
        </section> : <>
          <div className="workspace-view" hidden={page !== "investigate"}>
            <div className="view-introduction"><div><span className="section-label">{repositoryName} · {analysis.repository.head_commit?.slice(0, 7) ?? "working tree"}</span><h2>Investigate this codebase</h2><p>{canUseValidatedSnapshot(analysis.status) ? "Ask a question, then follow its validated evidence into the graph." : "This snapshot has not passed publication validation."}</p></div><span className={`context-status ${analysis.status.toLowerCase()}`}><i/>Snapshot {analysis.status.toLowerCase()}</span></div>
            {canUseValidatedSnapshot(analysis.status) ? <InvestigationWorkspace key={analysis.snapshot_id} snapshotId={analysis.snapshot_id} selected={selection} onInspectPathEdges={inspectEvidencePathEdges} onFocusEvidence={setFocusedEvidence} focusedEvidence={focusedEvidence} runtimeRefreshRunId={runtimeRefreshRunId} onRuntimeCalls={handleRuntimeCalls} onEvidencePathChange={handleEvidencePathChange} displayNames={displayNames} displayContexts={displayContexts} pathEvidenceInfo={pathEvidenceInfo} exampleQuestions={exampleQuestions} onSavedInvestigationsChange={handleSavedInvestigations} requestedInvestigationId={historyRequestId} onHistoryRequestHandled={handleHistoryRequestOpened} onInspect={(item) => { setSelection(item); setPage("graph"); }} graph={<GraphExplorer analysis={analysis} selection={selection} runtimeCalls={runtimeCalls} highlightedPathEdgeIds={highlightedPathEdgeIds} onSelect={setSelection} />} /> : <UnreadySnapshotNotice status={analysis.status}/>}
          </div>
          <div className="workspace-view" hidden={page !== "graph"}>
            <div className="view-introduction"><div><span className="section-label">SNAPSHOT GRAPH · {analysis.repository.head_commit?.slice(0, 7) ?? "working tree"}</span><h2>Explore code relationships</h2><p>{canUseValidatedSnapshot(analysis.status) ? "Filter real snapshot relationships, then select an edge or node to inspect its source evidence." : "Graph relationships are available after publication validation passes."}</p></div>{canUseValidatedSnapshot(analysis.status) && <span className="graph-record-count">{analysis.provenance.length.toLocaleString()} provenance records</span>}</div>
            {page === "graph" && (canUseValidatedSnapshot(analysis.status) ? <GraphExplorer analysis={analysis} selection={selection} runtimeCalls={runtimeCalls} highlightedPathEdgeIds={highlightedPathEdgeIds} onSelect={setSelection} /> : <UnreadySnapshotNotice status={analysis.status}/>)}
          </div>
          <div className="workspace-view" hidden={page !== "changes"}>
            <div className="view-introduction"><div><span className="section-label">CHANGE IMPACT · APPROVED EXECUTION</span><h2>Changes &amp; tests</h2><p>Compare commits, review graph-backed impact, then select and approve a bounded test run.</p></div></div>
            {canUseValidatedSnapshot(analysis.status) ? <DiffPlanningWorkspace repositoryPath={analysis.repository.root} targetSha={analysis.repository.head_commit} workingTreeDirty={analysis.repository.is_dirty} onRuntimeCompleted={setRuntimeRefreshRunId}/> : <UnreadySnapshotNotice status={analysis.status}/>}
          </div>
          <div className="workspace-view history-view" hidden={page !== "history"}>
            <div className="view-introduction"><div><span className="section-label">SAVED WORK · SNAPSHOT BOUND</span><h2>History</h2><p>Return to a repository snapshot or a saved investigation.</p></div><button className="header-secondary-action" onClick={startNewRepository}>Analyze another repository</button></div>
            <section className="history-section"><div className="history-section-heading"><h3>Saved investigations</h3><span>{savedInvestigations.length}</span></div>{savedInvestigations.length ? <div className="saved-investigations">{savedInvestigations.map((item) => <button key={item.investigation_id} className="saved-investigation-row" onClick={() => { setHistoryRequestId(item.investigation_id); setPage("investigate"); }}><MessageSquareText size={16}/><span><b>{item.last_question}</b><small>{new Date(item.updated_at * 1000).toLocaleString()} · {item.note_count} notes</small></span><ChevronRight size={16}/></button>)}</div> : <p className="history-empty">Saved questions appear here after an investigation.</p>}</section>
            <section className="history-section"><div className="history-section-heading"><h3>Repository snapshots</h3><button className="text-button" onClick={() => void refreshSnapshots()}><RefreshCw size={14}/> Refresh</button></div><SnapshotList snapshots={snapshots} selectedId={snapshotId} onSelect={(id) => void openSnapshot(id)} onRefresh={() => void refreshSnapshots()}/></section>
            <details className="advanced-history"><summary>Review queue and diagnostics</summary><div className="history-advanced-content"><ReviewQueue items={reviewItems} filter={queueFilter} setFilter={setQueueFilter} onSelect={(item) => { if (item.selection) { setSelection(item.selection); setPage("graph"); } }}/>{analysis.diagnostics.length > 0 && <Diagnostics diagnostics={analysis.diagnostics}/>}<details className="snapshot-details"><summary><GitCommitHorizontal size={14}/> Snapshot details</summary><div className="snapshot-meta"><span>{analysis.repository.is_shallow ? "Shallow clone" : "Full history"}</span><span>{analysis.repository.is_dirty ? "Working tree dirty" : "Working tree clean"}</span><span>Python {analysis.analysis_environment?.python_version ?? "unknown"}</span><span>scip-python {analysis.analysis_environment?.scip_python_version ?? analysis.semantic_indexing}</span></div><div className="metrics"><Metric label="Python files" value={analysis.counts.analyzed_python_files ?? 0}/><Metric label="Definitions" value={analysis.entities.length}/><Metric label="Sites" value={analysis.sites.length}/><Metric label="Review items" value={reviewItems.length}/></div><details className="technical-details"><summary>Technical details</summary><code>{analysis.snapshot_id}</code></details></details></div></details>
          </div>
        </>}
        {analysis && canUseValidatedSnapshot(analysis.status) && selection && selected && (page === "investigate" || page === "graph") && <Inspector analysis={analysis} selection={selection} selected={selected} provenance={selectedProvenance} relatedProvenance={relatedProvenance} binding={selectedBinding} derived={selectedDerived} linkedSite={selectedSiteForEdge} source={source} neighbors={neighbors} showPremises={showPremises} setShowPremises={setShowPremises} onReplay={replayEdge} onNeighbors={() => void loadNeighbors()} onCopy={() => void copyDebugId()} onClose={() => setSelection(null)} copied={copied} onSelect={setSelection}/>}
        {error && <div className="error-banner" role="alert"><AlertTriangle size={15}/><span>{error}</span><button aria-label="Dismiss" onClick={() => setError("")}><X size={14}/></button></div>}
        {notice && <div className="notice-banner" role="status"><Check size={15}/><span>{notice}</span><button aria-label="Dismiss" onClick={() => setNotice("")}><X size={14}/></button></div>}
      </div>
    </section>
  </main>;
}

function delay(ms: number) { return new Promise((resolve) => window.setTimeout(resolve, ms)); }
function UnreadySnapshotNotice({ status }: { status: string }) { return <section className="panel-card snapshot-unavailable" role="alert"><AlertTriangle size={20}/><div><h3>Snapshot {status.toLowerCase()}</h3><p>Cached analysis is retained in History for diagnostics. Investigations, graph relationships, and change impact require a READY snapshot.</p></div></section>; }
function Metric({ label, value }: { label: string; value: number }) { return <div className="metric"><span>{label}</span><strong>{value.toLocaleString()}</strong></div>; }

function SnapshotList({ snapshots, selectedId, onSelect, onRefresh, compact = false }: { snapshots: SnapshotRow[]; selectedId: string; onSelect: (id: string) => void; onRefresh: () => void; compact?: boolean }) {
  if (!snapshots.length) return null;
  return <section className={`snapshot-list ${compact ? "compact" : ""}`}><div className="list-heading"><div><span className="section-label">RECENT SNAPSHOTS</span><span className="subtle">{snapshots.length} stored</span></div><button className="text-button" onClick={onRefresh}>Refresh list</button></div><div className="snapshot-list-items">{snapshots.slice(0, compact ? 4 : 8).map((item) => <button key={item.snapshot_id} className={`snapshot-list-row ${item.snapshot_id === selectedId ? "current" : ""}`} onClick={() => onSelect(item.snapshot_id)}><span className={`status-dot ${item.status.toLowerCase()}`}/><span className="snapshot-list-name">{humanRepositoryName(item.repository_root)}<small>{item.head_commit?.slice(0, 10) ?? "Commit unavailable"}</small></span><span className="snapshot-list-status">{item.status}</span><ChevronRight size={14}/></button>)}</div></section>;
}

function Overview({ analysis, reviewItems, onOpenGraph, onOpenReview, onSelect }: { analysis: Analysis; reviewItems: ReviewItem[]; onOpenGraph: () => void; onOpenReview: () => void; onSelect: (item: Selection) => void }) {
  const exact = analysis.bindings.filter((item) => item.binding_state === "EXACT").length;
  const dispatch = analysis.dispatch.filter((item) => item.state === "MULTI_CANDIDATE").length;
  const boundSiteIds = new Set(analysis.bindings.map((binding) => binding.site_id));
  const unresolved = analysis.sites.filter((site) => !boundSiteIds.has(site.site_id)).length;
  const dispatchSites = analysis.dispatch.filter((item) => item.candidate_entity_ids.length > 0).slice(0, 8);
  return <div className="overview-grid">
    <section className="panel-card wide"><div className="panel-title"><div><h2>Evidence graph</h2><p>Verified bindings and conservative dispatch candidates for this snapshot.</p></div><button className="text-button" onClick={onOpenGraph}>Open explorer <ArrowUpRight size={13}/></button></div><div className="evidence-stats"><div><span className="stat-mark exact"><Link2 size={14}/></span><strong>{exact}</strong><small>Exact bindings</small></div><div><span className="stat-mark dispatch"><Waypoints size={14}/></span><strong>{dispatch}</strong><small>Multi candidate sites</small></div><div><span className="stat-mark unresolved"><AlertTriangle size={14}/></span><strong>{unresolved}</strong><small>Unresolved sites</small></div></div><div className="relation-preview">{analysis.bindings.slice(0, 4).map((binding) => { const site = analysis.sites.find((item) => item.site_id === binding.site_id); const target = analysis.entities.find((item) => item.entity_id === binding.target_entity_id); return <button key={binding.edge_id} className="relation-row" onClick={() => onSelect({ type: "edge", id: binding.edge_id })}><span className="relation-node">{site?.expression ?? binding.site_id}</span><span className="relation-line"/><span className="relation-type">EXACT</span><span className="relation-node">{target ? humanSymbolName(target.name, target.kind) : humanExternalName(analysis.external_symbols.find((item) => item.external_symbol_id === binding.external_symbol_id)?.semantic_symbol ?? "", analysis.external_symbols.find((item) => item.external_symbol_id === binding.external_symbol_id)?.distribution_name, analysis.external_symbols.find((item) => item.external_symbol_id === binding.external_symbol_id)?.module_name)}</span><ChevronRight size={14}/></button>; })}{analysis.bindings.length === 0 && <div className="empty-inline">No exact binding edges are recorded for this snapshot.</div>}</div></section>
    <section className="panel-card dispatch-card"><div className="panel-title"><div><h2>Possible dispatch</h2><p>Conservative candidates; these are not exact call bindings.</p></div><Waypoints size={16}/></div>{dispatchSites.length ? dispatchSites.map((item) => { const site = analysis.sites.find((candidate) => candidate.site_id === item.site_id); return <button className="dispatch-row" key={item.site_id} onClick={() => onSelect({ type: "site", id: item.site_id })}><span><b>{site?.expression ?? item.site_id}</b><small>{item.state.replaceAll("_", " ")} · {site?.file_path ?? "source unavailable"}{site ? `:${site.span.start_line}` : ""}</small><em>{item.candidate_entity_ids.map((id) => analysis.entities.find((entity) => entity.entity_id === id)?.qualified_name ?? id).join(" · ")}</em></span><ArrowUpRight size={13}/></button>; }) : <div className="empty-inline">No conservative dispatch candidates are recorded.</div>}</section>
    <section className="panel-card"><div className="panel-title"><div><h2>Review queue</h2><p>Items that need a closer look.</p></div><button className="count-chip" onClick={onOpenReview}>{reviewItems.length}</button></div>{reviewItems.slice(0, 4).map((item) => <button className="review-preview" key={item.key} onClick={() => { if (item.selection) onSelect(item.selection); onOpenReview(); }}><span className="review-icon"><AlertTriangle size={13}/></span><span><b>{item.title}</b><small>{item.file ? `${item.file}${item.line ? `:${item.line}` : ""}` : item.state}</small></span></button>)}{reviewItems.length === 0 && <div className="empty-inline success"><Check size={15}/> Nothing currently needs review.</div>}</section>
    <section className="panel-card"><div className="panel-title"><div><h2>Analysis environment</h2><p>Tools and resolver used for this snapshot.</p></div><ShieldCheck size={17} className="green-icon"/></div><InfoRow label="Python" value={analysis.analysis_environment?.python_version ?? "Unavailable"}/><InfoRow label="scip-python" value={analysis.analysis_environment?.scip_python_version ?? "Not run"}/><InfoRow label="SCIP CLI" value={analysis.analysis_environment?.scip_cli_version ?? "Not run"}/><InfoRow label="Environment ID" value={analysis.analysis_environment?.environment_id.slice(0, 18) ?? "—"}/></section>
    <section className="panel-card"><div className="panel-title"><div><h2>Recent diagnostics</h2><p>Parser and resolver notes.</p></div><span className="subtle">{analysis.diagnostics.length} total</span></div>{analysis.diagnostics.slice(0, 4).map((item, index) => <div className="diagnostic-row" key={`${item.code}-${index}`}><AlertTriangle size={13}/><span><b>{item.code}</b><small>{item.file_path ?? item.message}</small></span></div>)}{analysis.diagnostics.length === 0 && <div className="empty-inline success"><Check size={15}/> No diagnostics reported.</div>}</section>
  </div>;
}

function GraphExplorer({ analysis, selection, runtimeCalls, highlightedPathEdgeIds, onSelect }: { analysis: Analysis; selection: Selection | null; runtimeCalls: RuntimeCall[]; highlightedPathEdgeIds: string[]; onSelect: (item: Selection) => void }) {
  const [filter, setFilter] = useState("");
  const [kind, setKind] = useState<"all" | "nodes" | "sites" | "edges">("all");
  const [visibleRowCount, setVisibleRowCount] = useState(100);
  const [visibleEdgeCount, setVisibleEdgeCount] = useState(12);
  const bindingsByEdge = useMemo(() => new Map(analysis.bindings.map((item) => [item.edge_id, item])), [analysis.bindings]);
  const derivedByEdge = useMemo(() => new Map(analysis.derived_edges.map((item) => [item.edge_id, item])), [analysis.derived_edges]);
  const entitiesById = useMemo(() => new Map(analysis.entities.map((item) => [item.entity_id, item])), [analysis.entities]);
  const sitesById = useMemo(() => new Map(analysis.sites.map((item) => [item.site_id, item])), [analysis.sites]);
  const externalSymbolsById = useMemo(() => new Map(analysis.external_symbols.map((item) => [item.external_symbol_id, item])), [analysis.external_symbols]);
  const highlightedPath = useMemo(() => new Set(highlightedPathEdgeIds), [highlightedPathEdgeIds]);
  const normalized = filter.trim().toLowerCase();
  const matchingEdges = useMemo(() => {
    const provenanceByEdge = new Map(analysis.provenance.map((record) => [record.edge_id, record]));
    const endpoint = (id: string, fallback: string): GraphEndpoint => {
      const entity = entitiesById.get(id);
      if (entity) return { id, label: humanSymbolName(entity.name, entity.kind), detail: `${entity.file_path}:${entity.span.start_line}`, type: "entity" };
      const site = sitesById.get(id);
      if (site) return { id, label: site.expression, detail: `${site.file_path}:${site.span.start_line}`, type: "site" };
      const external = externalSymbolsById.get(id);
      if (external) {
        const label = humanExternalName(external.semantic_symbol, external.distribution_name, external.module_name);
        const detail = external.external_kind.toLowerCase() === "builtin" ? "Python built-in" : external.external_kind.toLowerCase() === "stdlib" ? "Python standard library" : external.distribution_name ?? external.module_name ?? "External library";
        return { id, label, detail, type: "external" };
      }
      return { id, label: fallback || "Unresolved record", detail: "Identity unavailable in this snapshot", type: "site" };
    };
    const relationMatches = (item: GraphRelation) => highlightedPath.has(item.id) || !normalized || `${item.source.label} ${item.source.detail} ${item.target.label} ${item.target.detail} ${item.label} ${item.state} ${item.file}`.toLowerCase().includes(normalized);
    const staticEdges: GraphRelation[] = analysis.bindings.flatMap((binding) => {
      const record = provenanceByEdge.get(binding.edge_id);
      const site = sitesById.get(binding.site_id);
      if (!record || !site) return [];
      const targetId = binding.target_entity_id ?? binding.external_symbol_id;
      if (!targetId) return [];
      const source = endpoint(site.context_id, site.expression);
      const target = endpoint(targetId, "Bound target");
      const state = edgeStateLabel(record, binding);
      const item: GraphRelation = { id: binding.edge_id, source, target, label: humanRelationshipLabel("CALL_SITE_BINDING", binding.binding_state), state, visualClass: edgeVisualClass(state, record, binding), file: `${site.file_path}:${site.span.start_line}`, selection: { type: "edge", id: binding.edge_id } };
      return relationMatches(item) ? [item] : [];
    });
    const derivedEdges: GraphRelation[] = analysis.derived_edges.flatMap((derived) => {
      const record = provenanceByEdge.get(derived.edge_id);
      if (!record) return [];
      const site = sitesById.get(derived.source_id);
      const sourceId = site?.context_id ?? derived.source_id;
      const source = endpoint(sourceId, site?.expression ?? "Source unavailable");
      const target = endpoint(derived.target_id, "Target unavailable");
      const state = edgeStateLabel(record, undefined, derived);
      const item: GraphRelation = { id: derived.edge_id, source, target, label: humanRelationshipLabel(derived.relation), state, visualClass: edgeVisualClass(state, record, undefined, derived), file: `${record.file_path}:${record.span.start_line}`, selection: { type: "edge", id: derived.edge_id } };
      return relationMatches(item) ? [item] : [];
    });
    const observed = runtimeCalls.flatMap((call) => {
      if (call.snapshot_id !== analysis.snapshot_id) return [];
      const source = entitiesById.get(call.source_entity_id);
      const target = entitiesById.get(call.target_entity_id);
      const left = source?.qualified_name ?? call.source_entity_id;
      const right = target?.qualified_name ?? call.target_entity_id;
      const state = `RUNTIME OBSERVED · ${call.run_id ?? "run ID unavailable"}`;
      const selection: Selection = call.site_id && sitesById.has(call.site_id)
        ? { type: "site", id: call.site_id }
        : source ? { type: "entity", id: call.source_entity_id }
        : target ? { type: "entity", id: call.target_entity_id }
        : { type: "site", id: call.site_id ?? call.observation_id };
      const runtimeProvenance = `Observed call · environment ${call.runtime_environment_id ?? "unavailable"} · materialization ${call.runtime_materialization_id ?? "unavailable"} · invocation ${call.test_invocation_id ?? "unavailable"} · observation ${call.observation_id} · evidence ${call.evidence_id}`;
      const item: GraphRelation = { id: `runtime:${call.observation_id}`, source: endpoint(call.source_entity_id, left), target: endpoint(call.target_entity_id, right), label: "Observed call", state, visualClass: "runtime", file: source ? `${source.file_path} · ${runtimeProvenance}` : runtimeProvenance, selection };
      return !normalized || `${left} ${right} ${state} ${call.observation_id} ${runtimeProvenance}`.toLowerCase().includes(normalized)
        ? [item]
        : [];
    });
    return [...staticEdges, ...derivedEdges, ...observed].sort((a, b) => Number(!highlightedPath.has(a.id)) - Number(!highlightedPath.has(b.id)) || Number(!a.source.detail.startsWith("src/requests/")) - Number(!b.source.detail.startsWith("src/requests/")) || a.source.label.localeCompare(b.source.label) || a.target.label.localeCompare(b.target.label) || a.label.localeCompare(b.label));
  }, [analysis, bindingsByEdge, derivedByEdge, entitiesById, externalSymbolsById, highlightedPath, normalized, runtimeCalls, sitesById]);
  const visibleEdges = matchingEdges.slice(0, visibleEdgeCount);
  const unresolvedSites = useMemo(() => {
    const exactSiteIds = new Set(analysis.bindings.map((item) => item.site_id));
    if (!normalized) return [];
    return analysis.sites.filter((site) => !exactSiteIds.has(site.site_id) && ["CALL_CALLEE", "VALUE_REFERENCE", "IMPORT_REFERENCE", "CALLBACK_REFERENCE"].includes(site.kind) && `${site.expression} ${site.file_path} ${site.kind}`.toLowerCase().includes(normalized)).slice(0, 12).map((site) => ({ id: site.site_id, label: site.expression, detail: `${site.file_path}:${site.span.start_line} · no target inferred`, file: site.file_path, selection: { type: "site" as const, id: site.site_id } }));
  }, [analysis, normalized]);
  const unresolvedCount = useMemo(() => {
    const bound = new Set(analysis.bindings.map((item) => item.site_id));
    return analysis.sites.filter((site) => !bound.has(site.site_id) && ["CALL_CALLEE", "VALUE_REFERENCE", "IMPORT_REFERENCE", "CALLBACK_REFERENCE"].includes(site.kind)).length;
  }, [analysis]);
  const rows = useMemo(() => {
    const result: Array<{ type: Selection["type"]; id: string; label: string; kind: string; file: string; line: number }> = [];
    const add = (item: typeof result[number]) => {
      if (`${item.label} ${item.kind} ${item.file}`.toLowerCase().includes(normalized)) result.push(item);
    };
    if (kind === "all" || kind === "nodes") {
      for (const item of analysis.entities) {
        add({ type: "entity", id: item.entity_id, label: item.qualified_name, kind: item.kind, file: item.file_path, line: item.span.start_line });
        if (result.length === visibleRowCount) return result;
      }
    }
    if (kind === "all" || kind === "sites") {
      for (const item of analysis.sites) {
        add({ type: "site", id: item.site_id, label: item.expression, kind: item.kind, file: item.file_path, line: item.span.start_line });
        if (result.length === visibleRowCount) return result;
      }
    }
    if (kind === "all" || kind === "edges") {
      for (const record of analysis.provenance) {
        const binding = bindingsByEdge.get(record.edge_id);
        const derived = derivedByEdge.get(record.edge_id);
        add({ type: "edge", id: record.edge_id, label: edgeLabel(record, binding, derived, analysis), kind: edgeStateLabel(record, binding, derived), file: record.file_path, line: record.span.start_line });
        if (result.length === visibleRowCount) return result;
      }
    }
    return result;
  }, [analysis, bindingsByEdge, derivedByEdge, kind, normalized, visibleRowCount]);
  const updateFilter = (value: string) => { setFilter(value); setVisibleRowCount(100); setVisibleEdgeCount(12); };
  const updateKind = (value: typeof kind) => { setKind(value); setVisibleRowCount(100); };
  const statusCount = (status: string) => matchingEdges.filter((edge) => edge.visualClass === status).length;
  return <section className="explorer-shell">
    <div className="explorer-toolbar"><div><span className="section-label">GRAPH EXPLORER</span><span className="subtle">Calls, unresolved targets, and observed runtime behavior</span></div><div className="explorer-search"><Search size={14}/><input value={filter} onChange={(event) => updateFilter(event.target.value)} placeholder="Find symbol, site, file, or run…" /></div></div>
    <div className="explorer-body"><div className="explorer-browser"><div className="filter-tabs">{(["all", "nodes", "sites", "edges"] as const).map((value) => <button className={kind === value ? "active" : ""} key={value} onClick={() => updateKind(value)}>{value === "all" ? "All" : value === "nodes" ? "Code" : value === "sites" ? "Call sites" : "Relationships"}</button>)}</div><div className="explorer-rows">{rows.map((row) => <button className={`explorer-row ${selection?.id === row.id ? "selected" : ""}`} key={`${row.type}-${row.id}`} onClick={() => onSelect({ type: row.type, id: row.id })}><span className={`object-icon ${row.type}`}>{row.type === "entity" ? <Boxes size={13}/> : row.type === "site" ? <FileCode2 size={13}/> : <Link2 size={13}/>}</span><span className="explorer-row-label"><b>{row.label}</b><small>{row.file}:{row.line}</small></span><span className="row-type">{row.type === "entity" ? "Code" : row.type === "site" ? "Call site" : "Relationship"}</span></button>)}{rows.length === 0 && <div className="empty-inline">No graph records match this filter.</div>}{rows.length === visibleRowCount && visibleRowCount < 400 && <button className="text-button" onClick={() => setVisibleRowCount((count) => Math.min(count + 100, 400))}>Show next {Math.min(100, 400 - visibleRowCount)} records</button>}</div></div>
    <GraphCanvas edges={visibleEdges} unresolved={unresolvedSites} selection={selection} highlightedPathEdgeIds={highlightedPath} counts={{verified:statusCount("verified"), exact:statusCount("exact"), derived:statusCount("derived"), possible:statusCount("possible"), runtime:statusCount("runtime"), unresolved:unresolvedCount}} totalEdges={matchingEdges.length} provenanceCount={analysis.provenance.length} runtimeCount={runtimeCalls.filter((call) => call.snapshot_id === analysis.snapshot_id).length} onSelect={onSelect} onShowMore={() => setVisibleEdgeCount((count) => Math.min(count + 18, 72))}/></div>
  </section>;
}

function GraphCanvas({ edges, unresolved, selection, highlightedPathEdgeIds, counts, totalEdges, provenanceCount, runtimeCount, onSelect, onShowMore }: { edges: GraphRelation[]; unresolved: GraphLooseSite[]; selection: Selection | null; highlightedPathEdgeIds: Set<string>; counts: Record<string, number>; totalEdges: number; provenanceCount: number; runtimeCount: number; onSelect: (item: Selection) => void; onShowMore: () => void }) {
  const nodes = useMemo(() => {
    const byId = new Map<string, GraphEndpoint>();
    for (const edge of edges) { byId.set(edge.source.id, edge.source); byId.set(edge.target.id, edge.target); }
    return [...byId.values()];
  }, [edges, unresolved]);
  const layout = useMemo(() => {
    if (!nodes.length) {
      const relationshipHeight = 520;
      const unresolvedRows = Math.ceil(unresolved.length / 3);
      return { points: new Map<string, { x: number; y: number }>(), width: 960, height: relationshipHeight + (unresolvedRows ? 40 + unresolvedRows * 78 : 0), relationshipHeight };
    }
    const ranks = new Map(nodes.map((node) => [node.id, 0]));
    const incoming = new Set(edges.map((edge) => edge.target.id));
    for (const node of nodes) if (incoming.has(node.id)) ranks.set(node.id, 1);
    for (let pass = 0; pass < Math.min(nodes.length, 5); pass += 1) {
      let changed = false;
      for (const edge of edges) {
        const next = Math.min(3, (ranks.get(edge.source.id) ?? 0) + 1);
        if (next > (ranks.get(edge.target.id) ?? 0)) { ranks.set(edge.target.id, next); changed = true; }
      }
      if (!changed) break;
    }
    const columns = new Map<number, GraphEndpoint[]>();
    for (const node of nodes) { const rank = ranks.get(node.id) ?? 0; columns.set(rank, [...(columns.get(rank) ?? []), node]); }
    for (const column of columns.values()) column.sort((a, b) => a.label.localeCompare(b.label) || a.id.localeCompare(b.id));
    const maxRank = Math.max(...columns.keys());
    const maxColumnSize = Math.max(...[...columns.values()].map((column) => column.length));
    const relationshipHeight = Math.max(260, maxColumnSize * 92 + 80);
    const unresolvedRows = Math.ceil(unresolved.length / 3);
    const height = relationshipHeight + (unresolvedRows ? 40 + unresolvedRows * 78 : 0);
    const width = Math.max(unresolved.length ? 960 : 640, 220 + maxRank * 310 + 210);
    const points = new Map<string, { x: number; y: number }>();
    for (const [rank, column] of columns) column.forEach((node, index) => points.set(node.id, { x: 120 + rank * 310, y: relationshipNodeY(index, column.length, relationshipHeight) }));
    return { points, width, height, relationshipHeight };
  }, [edges, nodes, unresolved.length]);
  const [view, setView] = useState({ x: 0, y: 0, width: 960, height: 520 });
  const [drag, setDrag] = useState<{ x: number; y: number; viewX: number; viewY: number } | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  useEffect(() => setView({ x: 0, y: 0, width: layout.width, height: layout.height }), [layout]);
  const zoom = (factor: number) => setView((current) => { const width = Math.max(360, Math.min(layout.width * 2, current.width * factor)); const height = Math.max(260, Math.min(layout.height * 2, current.height * factor)); return { x: current.x + (current.width - width) / 2, y: current.y + (current.height - height) / 2, width, height }; });
  const edgePairs = new Map<string, number[]>();
  for (const edge of edges) { const key = `${edge.source.id}\u0000${edge.target.id}`; edgePairs.set(key, [...(edgePairs.get(key) ?? []), edges.indexOf(edge)]); }
  const pairOffset = (edge: GraphRelation) => { const indices = edgePairs.get(`${edge.source.id}\u0000${edge.target.id}`) ?? []; return (indices.indexOf(edges.indexOf(edge)) - (indices.length - 1) / 2) * 22; };
  const relationColor = (visualClass: string) => visualClass === "possible" ? "#a36a10" : visualClass === "runtime" ? "#5263a8" : visualClass === "unresolved" ? "#6f7d86" : visualClass === "verified" ? "#247a52" : visualClass === "derived" ? "#52758c" : "#39728a";
  const nodeSelection = (node: GraphEndpoint): Selection | undefined => node.type === "entity" || node.type === "site" ? { type: node.type, id: node.id } : undefined;
  const panStart = (event: React.PointerEvent<SVGSVGElement>) => { if (event.target !== event.currentTarget) return; const rect = event.currentTarget.getBoundingClientRect(); setDrag({ x: event.clientX / rect.width, y: event.clientY / rect.height, viewX: view.x, viewY: view.y }); event.currentTarget.setPointerCapture(event.pointerId); };
  const panMove = (event: React.PointerEvent<SVGSVGElement>) => { if (!drag) return; const rect = event.currentTarget.getBoundingClientRect(); setView((current) => ({ ...current, x: drag.viewX - (event.clientX / rect.width - drag.x) * current.width, y: drag.viewY - (event.clientY / rect.height - drag.y) * current.height })); };
  const svgText = (value: string, limit = 27) => value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
  return <div className="relation-canvas">
    <div className="canvas-heading"><span>CODE RELATIONSHIPS</span><small>{edges.length} shown · {totalEdges} matching · {runtimeCount} runtime observations</small></div>
    <div className="graph-legend" aria-label="Relationship evidence legend"><span className="verified"><i/>Replay verified</span><span className="exact"><i/>Exact static · replay unconfirmed</span><span className="possible"><i/>Possible call</span><span className="runtime"><i/>Runtime observation</span><span className="unresolved"><i/>Unresolved target</span></div>
    <div className="graph-counts"><span>Replay verified <b>{counts.verified}</b></span><span>Exact static <b>{counts.exact}</b></span><span>Derived <b>{counts.derived}</b></span><span>Possible calls <b>{counts.possible}</b></span><span>Runtime observations <b>{counts.runtime}</b></span><span>Unresolved targets <b>{counts.unresolved}</b></span></div>
    <div className="graph-controls"><span>Drag to pan · scroll to zoom · select nodes or edges to inspect</span><div><button type="button" aria-label="Zoom out" onClick={() => zoom(1.2)}>−</button><button type="button" aria-label="Zoom in" onClick={() => zoom(0.82)}>+</button><button type="button" onClick={() => setView({ x: 0, y: 0, width: layout.width, height: layout.height })}>Fit graph</button></div></div>
    {nodes.length === 0 && unresolved.length === 0 ? <div className="canvas-empty"><Waypoints size={22}/><p>{totalEdges === 0 ? "No evidence-backed relationships match this search." : "The current filter has no relationships to display."}</p></div> : <div className="graph-viewport" style={{height: graphViewportHeight(layout.height)}}><svg className="graph-svg" style={{height: graphViewportHeight(layout.height)}} role="group" aria-label="Interactive snapshot relationship graph" viewBox={`${view.x} ${view.y} ${view.width} ${view.height}`} onPointerDown={panStart} onPointerMove={panMove} onPointerUp={() => setDrag(null)} onPointerCancel={() => setDrag(null)} onWheel={(event) => { event.preventDefault(); zoom(event.deltaY > 0 ? 1.12 : 0.89); }}>
      <defs>{["verified", "exact", "possible", "runtime", "unresolved", "derived"].map((state) => <marker key={state} id={`graph-arrow-${state}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill={relationColor(state)}/></marker>)}</defs>
      <g className="graph-links">{edges.map((edge) => { const start = layout.points.get(edge.source.id); const end = layout.points.get(edge.target.id); if (!start || !end) return null; const offset = pairOffset(edge); const middle = (start.x + end.x) / 2; const selected = selection?.id === edge.selection.id; const inPath = highlightedPathEdgeIds.has(edge.id); const active = selected || hovered === edge.id; return <g key={edge.id} className={`graph-link ${edge.visualClass} ${selected ? "selected" : ""} ${inPath ? "path-highlight" : ""} ${hovered === edge.id ? "hovered" : ""}`} onMouseEnter={() => setHovered(edge.id)} onMouseLeave={() => setHovered(null)}><title>{`${inPath ? "Validated Evidence Path · " : ""}${edge.source.label} → ${edge.target.label} · ${edge.state} · ${edge.file}`}</title><path d={`M ${start.x + 96} ${start.y} C ${middle + offset} ${start.y + offset}, ${middle + offset} ${end.y + offset}, ${end.x - 96} ${end.y}`} markerEnd={`url(#graph-arrow-${edge.visualClass})`} onClick={() => onSelect(edge.selection)} role="button" tabIndex={0} aria-label={`${inPath ? "Validated Evidence Path: " : ""}Inspect relationship: ${edge.source.label} to ${edge.target.label}, ${edge.state}`} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(edge.selection); } }}/>{active && <text x={middle} y={(start.y + end.y) / 2 + offset - 4} textAnchor="middle">{svgText(edge.label, 22)}</text>}</g>; })}</g>
      <g className="graph-nodes">{nodes.map((node) => { const point = layout.points.get(node.id); if (!point) return null; const selected = selection?.id === node.id; const inPath = edges.some((edge) => highlightedPathEdgeIds.has(edge.id) && (edge.source.id === node.id || edge.target.id === node.id)); const onClick = () => { const target = nodeSelection(node); if (target) onSelect(target); else { const edge = edges.find((candidate) => candidate.source.id === node.id || candidate.target.id === node.id); if (edge) onSelect(edge.selection); } }; return <g key={node.id} className={`graph-node ${node.type} ${selected ? "selected" : ""} ${inPath ? "path-highlight" : ""} ${hovered && edges.some((edge) => edge.id === hovered && (edge.source.id === node.id || edge.target.id === node.id)) ? "related" : ""}`} transform={`translate(${point.x - 90} ${point.y - 29})`} role="button" tabIndex={0} aria-label={`Inspect ${node.type}: ${node.label} · ${node.detail}${inPath ? " · on validated Evidence Path" : ""}`} onClick={onClick} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onClick(); } }}><title>{`${node.label} · ${node.detail}${inPath ? " · validated Evidence Path" : ""}`}</title><rect width="180" height="58" rx="10"/><text x="12" y="24">{svgText(node.label.split(".").slice(-2).join("."), 26)}</text><text className="graph-node-detail" x="12" y="43">{svgText(node.detail, 29)}</text></g>; })}</g>
      <g className="graph-unresolved-nodes">{unresolved.map((site, index) => { const { x, y } = unresolvedSitePosition(index, layout.relationshipHeight ?? layout.height); return <g key={site.id} className={`graph-node site unresolved-node ${selection?.id === site.id ? "selected" : ""}`} transform={`translate(${x - 90} ${y - 24})`} role="button" tabIndex={0} aria-label={`Inspect unresolved site: ${site.label}, no target inferred, ${site.file}`} onClick={() => onSelect(site.selection)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(site.selection); } }}><title>{`${site.label} · ${site.detail}`}</title><rect width="180" height="48" rx="9"/><text x="12" y="21">{svgText(site.label, 25)}</text><text className="graph-node-detail" x="12" y="38">No target · {svgText(site.file, 17)}</text></g>; })}</g>
    </svg></div>}
    {edges.length < totalEdges && edges.length < 72 && <button className="text-button canvas-more" onClick={onShowMore}>Show {Math.min(18, 72 - edges.length)} more relationships · {totalEdges} match this filter</button>}
  </div>;
}

function Inspector({ analysis, selection, selected, provenance, relatedProvenance, binding, derived, linkedSite, source, neighbors, showPremises, setShowPremises, onReplay, onNeighbors, onCopy, onClose, copied, onSelect }: { analysis: Analysis; selection: Selection | null; selected?: Entity | Site | Provenance; provenance?: Provenance; relatedProvenance: Provenance[]; binding?: Binding; derived?: DerivedEdge; linkedSite?: Site; source: SourcePreview | null; neighbors: NeighborResult | null; showPremises: boolean; setShowPremises: (value: boolean) => void; onReplay: (id: string) => void; onNeighbors: () => void; onCopy: () => void; onClose: () => void; copied: boolean; onSelect: (item: Selection) => void }) {
  const entity = selection?.type === "entity" ? selected as Entity | undefined : undefined;
  const site = selection?.type === "site" ? selected as Site | undefined : linkedSite;
  const edge = selection?.type === "edge" ? selected as Provenance | undefined : undefined;
  const parentEntity = entity?.parent_id ? analysis.entities.find((item) => item.entity_id === entity.parent_id) : undefined;
  const title = entity ? humanSymbolName(entity.name, entity.kind) : site?.expression ?? (edge ? edgeLabel(edge, binding, derived, analysis) : "Select graph evidence");
  const file = entity?.file_path ?? site?.file_path ?? edge?.file_path;
  const span = entity?.span ?? site?.span ?? edge?.span;
  const target = binding?.target_entity_id ? analysis.entities.find((item) => item.entity_id === binding.target_entity_id) : derived ? analysis.entities.find((item) => item.entity_id === derived.target_id) : undefined;
  const recordId = entity?.entity_id ?? site?.site_id ?? edge?.edge_id;
  const relation = binding ? "CALL_SITE_BINDING" : derived?.relation ?? edge?.rule_id ?? "";
  const replayState = edge?.replay_state ?? (linkedSite ? analysis.provenance.find((item) => item.edge_id === binding?.edge_id)?.replay_state : undefined);
  const evidenceModality = binding?.binding_state ?? derived?.relation ?? (edge ? "EXACT" : "UNRESOLVED");
  const exactFile = edge && analysis.provenance.find((item) => item.edge_id === edge.edge_id)?.file_path;
  const dispatch = site ? analysis.dispatch.find((item) => item.site_id === site.site_id) : undefined;
  const external = binding?.external_symbol_id ? analysis.external_symbols.find((item) => item.external_symbol_id === binding.external_symbol_id) : undefined;
  return <aside className="inspector-panel" aria-label="Graph inspector">
    <div className="inspector-head"><div><span className="section-label">GRAPH INSPECTOR</span><h2>{title}</h2></div><div className="inspector-head-actions"><button className="icon-button" title={copied ? "Copied" : "Copy canonical ID"} aria-label={copied ? "Copied canonical ID" : "Copy canonical ID"} onClick={onCopy}>{copied ? <Check size={14}/> : <Copy size={14}/>}</button><button className="icon-button" title="Close inspector" aria-label="Close inspector" onClick={onClose}><X size={14}/></button></div></div>
    {!selection || !selected ? <div className="inspector-empty"><div><Search size={20}/></div><b>Choose code or a relationship</b><p>See what it connects, where the source is, and what evidence supports it.</p></div> : <>
      <div className="inspector-scroll">
        <div className="identity-block"><span className={`type-tag ${selection.type}`}>{selection.type === "edge" ? "Relationship" : selection.type === "entity" ? entity?.kind ?? "Code" : "Call site"}</span>{entity && parentEntity && <small className="inspector-context">In {parentEntity.name} · {entity.file_path}</small>}{edge && <InfoRow label="Meaning" value={humanRelationshipLabel(relation, evidenceModality)}/ >}{entity && <InfoRow label="Type" value={entity.kind}/ >}{site && <InfoRow label="Target" value={binding?.binding_state === "EXACT" ? "Exact static target" : "Unresolved target"}/ >}{dispatch && <InfoRow label="Possible dispatch" value={humanDispatchState(dispatch.state, dispatch.candidate_entity_ids.length)}/ >}<InfoRow label="Source" value={file ?? "—"}/><InfoRow label="Location" value={span ? `${span.start_line}:${span.start_column} – ${span.end_line}:${span.end_column}` : "—"}/>{target && <InfoRow label="Connected to" value={humanSymbolName(target.name, target.kind)}/ >}{external && <InfoRow label="External target" value={external.distribution_name ?? external.module_name ?? "External library"}/ >}
          <details className="technical-details"><summary>Technical details</summary><code>{recordId}</code><InfoRow label="Snapshot" value={analysis.snapshot_id}/>{source && <InfoRow label="Source hash" value={source.file_content_hash}/ >}{span && <InfoRow label="UTF-8 bytes" value={`${span.start_byte}–${span.end_byte}`}/ >}{entity && <InfoRow label="Parent ID" value={entity.parent_id ?? "Top-level"}/ >}{site && <><InfoRow label="Site kind" value={site.kind}/><InfoRow label="Syntax node" value={site.syntax_node_type}/><InfoRow label="Resolver" value={binding?.resolver ?? "—"}/><InfoRow label="SCIP symbol" value={binding?.scip_symbol ?? "—"}/></>}{edge && <><InfoRow label="Replay scope" value={edge.replay_scope}/><InfoRow label="Extractor" value={`${edge.extractor_name} ${edge.extractor_version}`}/><InfoRow label="Rule ID" value={edge.rule_id}/><InfoRow label="Review state" value={edge.review_state}/><InfoRow label="File hash" value={edge.file_content_hash}/></>}</details>
        </div>
        {edge && <section className="inspector-section"><div className="section-title">EVIDENCE</div><InfoRow label="Classification" value={evidenceStateLabel({ modality: evidenceModality, replayState: edge.replay_state, reviewState: edge.review_state })}/><InfoRow label="Replay" value={replayStateLabel(edge.replay_state)}/><details className="technical-details"><summary>Provenance and premises · {edge.premise_edge_ids.length}</summary><InfoRow label="Extractor" value={`${edge.extractor_name} ${edge.extractor_version}`}/><InfoRow label="Rule" value={edge.rule_id}/><InfoRow label="Review state" value={edge.review_state}/>{edge.premise_edge_ids.length > 0 && <div className="premise-list">{edge.premise_edge_ids.map((id) => <button key={id} onClick={() => onSelect({ type: "edge", id })}><Link2 size={12}/><code>{id}</code></button>)}</div>}</details></section>}
        {!edge && relatedProvenance.length > 0 && <section className="inspector-section"><div className="section-title">Related evidence <span>{relatedProvenance.length}</span></div><div className="premise-list">{relatedProvenance.map((record) => <div className="related-evidence" key={record.edge_id}><button onClick={() => onSelect({ type: "edge", id: record.edge_id })}><Link2 size={12}/><span><b>{humanRelationshipLabel(analysis.bindings.some((item) => item.edge_id === record.edge_id) ? "CALL_SITE_BINDING" : analysis.derived_edges.find((item) => item.edge_id === record.edge_id)?.relation ?? record.rule_id)}</b><small>{record.file_path}:{record.span.start_line} · {replayStateLabel(record.replay_state)}</small></span></button><details><summary>Technical details</summary><code>Provenance {record.provenance_id} · {record.edge_id} · {record.extractor_name} {record.extractor_version}</code></details></div>)}</div></section>}
        {site && !edge && <section className="inspector-section"><div className="section-title">Connection</div><InfoRow label="Static target" value={binding?.binding_state === "EXACT" ? "Exact static target" : "Target unresolved"}/><InfoRow label="Possible dispatch" value={humanDispatchState(dispatch?.state ?? "NOT_APPLICABLE", dispatch?.candidate_entity_ids.length ?? 0)}/>{binding && <button className="subtle-action" onClick={() => onSelect({ type: "edge", id: binding.edge_id })}>Open binding provenance<ChevronRight size={13}/></button>}</section>}
        {entity && <section className="inspector-section"><div className="section-title">Code element</div><InfoRow label="Name" value={humanSymbolName(entity.name, entity.kind)}/><InfoRow label="Container" value={parentEntity?.name ?? entity.qualified_name.split(".").slice(-2, -1)[0] ?? "Top level"}/></section>}
        {edge && <div className="inspector-actions"><button className="action-primary" onClick={() => onReplay(edge.edge_id)}><Play size={13}/> Replay evidence</button><button className="action-secondary" onClick={onNeighbors}><Waypoints size={13}/> Show neighbors</button></div>}
        {!edge && <div className="inspector-actions"><button className="action-secondary full" onClick={onNeighbors}><Waypoints size={13}/> Show neighbors</button></div>}
        <section className="source-section"><div className="source-title"><span><FileCode2 size={14}/> Source preview</span></div>{source ? <><div className="source-location">{source.file_path} · {source.start_line}:{source.start_column}</div><pre className="source-code"><span>{String(source.start_line).padStart(3, "0")}</span>{source.text || "(empty source span)"}</pre></> : <div className="source-missing">{file && exactFile ? "Loading hash verified source…" : "Source preview loads from this snapshot's content addressed bytes."}</div>}</section>
        {neighbors && <section className="inspector-section"><div className="section-title">NEIGHBORS <span>{neighbors.candidate_count_before_limit ?? neighbors.results?.length ?? 0}</span></div><div className="neighbor-list">{neighbors.results?.slice(0, 20).map((item, index) => { const id = String(item.site_id ?? item.id ?? item.target_id ?? index); return <button key={id} onClick={() => { if (analysis.sites.some((candidate) => candidate.site_id === id)) onSelect({ type: "site", id }); else if (analysis.entities.some((candidate) => candidate.entity_id === id)) onSelect({ type: "entity", id }); }}><Link2 size={12}/><span>{String(item.expression ?? item.qualified_name ?? item.name ?? item.kind ?? id)}</span><small>{String(item.file_path ?? "")}</small></button>; })}{(!neighbors.results || neighbors.results.length === 0) && <div className="empty-inline">No connected records returned.</div>}</div></section>}
      </div>
    </>}
  </aside>;
}

function ReviewQueue({ items, filter, setFilter, onSelect }: { items: ReviewItem[]; filter: string; setFilter: (value: string) => void; onSelect: (item: ReviewItem) => void }) {
  const categories = ["ALL", ...new Set(items.map((item) => item.state))];
  const visible = filter === "ALL" ? items : items.filter((item) => item.state === filter);
  return <section className="review-queue"><div className="review-queue-head"><div><h2>Evidence requiring review</h2><p>Resolver disagreements, replay failures, unresolved sites, and diagnostics stay visible until examined.</p></div><span className="queue-total"><AlertTriangle size={14}/>{items.length}</span></div><div className="queue-filters">{categories.map((item) => <button key={item} className={filter === item ? "active" : ""} onClick={() => setFilter(item)}>{item.replaceAll("_", " ")}{item === "ALL" && <span>{items.length}</span>}</button>)}</div>{visible.length ? <div className="queue-list">{visible.map((item) => <button className="queue-item" key={item.key} onClick={() => onSelect(item)}><span className="queue-item-icon"><AlertTriangle size={15}/></span><span className="queue-item-main"><b>{item.title}</b><small>{item.detail}</small><code>{item.file ? `${item.file}${item.line ? `:${item.line}` : ""}` : item.state}</code></span><span className="queue-state">{item.state.replaceAll("_", " ")}</span><ChevronRight size={15}/></button>)}</div> : <div className="empty-queue"><div><Check size={18}/></div><b>No items in this review filter.</b><p>Current resolver, replay, and diagnostic signals are accounted for.</p></div>}</section>;
}

function Diagnostics({ diagnostics }: { diagnostics: Diagnostic[] }) { return <section className="diagnostics"><div className="panel-title"><div><h2>Analysis diagnostics</h2><p>Parser and resolver information for this snapshot.</p></div><span className="subtle">{diagnostics.length}</span></div>{diagnostics.slice(0, 20).map((item, index) => <div className="diagnostic-row" key={`${item.code}-${index}`}><AlertTriangle size={13}/><span><b>{item.code}</b><small>{item.message}{item.file_path ? ` · ${item.file_path}` : ""}</small></span></div>)}</section>; }
function InfoRow({ label, value }: { label: string; value: string }) { return <div className="info-row"><span>{label}</span><b title={value}>{value}</b></div>; }
function edgeStateLabel(record: Provenance, binding?: Binding, derived?: DerivedEdge) {
  return evidenceStateLabel({ modality: binding?.binding_state ?? derived?.relation ?? "EXACT", replayState: record.replay_state, reviewState: record.review_state });
}
function edgeVisualClass(_state: string, record?: Provenance, binding?: Binding, derived?: DerivedEdge) {
  if (derived?.relation === "MAY_DISPATCH_TO") return "possible";
  if (record?.replay_state === "EDGE_NOT_REPRODUCED" || record?.replay_state === "SOURCE_HASH_MISMATCH" || record?.replay_state === "ERROR" || record?.replay_state === "ENVIRONMENT_MISMATCH" || record?.replay_state === "EXTRACTOR_VERSION_MISMATCH" || record?.replay_state === "PREMISE_MISSING") return "unresolved";
  if (binding?.binding_state === "EXACT" && record?.replay_state === "PASS" && record.review_state === "NONE") return "verified";
  if (derived) return "derived";
  return binding?.binding_state === "EXACT" ? "exact" : "unresolved";
}
function edgeLabel(record: Provenance, binding: Binding | undefined, derived: DerivedEdge | undefined, analysis: Analysis) { const site = binding && analysis.sites.find((item) => item.site_id === binding.site_id); const source = site && analysis.entities.find((item) => item.entity_id === site.context_id); const target = binding?.target_entity_id ? analysis.entities.find((item) => item.entity_id === binding.target_entity_id) : derived ? analysis.entities.find((item) => item.entity_id === derived.target_id) : undefined; const from = source ? humanSymbolName(source.name, source.kind) : "Code"; const to = target ? humanSymbolName(target.name, target.kind) : "External code"; return `${from} → ${to}`; }
function buildReviewQueue(analysis: Analysis | null): ReviewItem[] {
  if (!analysis) return [];
  const items: ReviewItem[] = [];
  for (const record of analysis.provenance) {
    if (record.review_state !== "NONE" || (record.replay_state !== "NOT_RUN" && record.replay_state !== "PASS")) items.push({ key: `edge:${record.edge_id}`, title: record.review_state !== "NONE" ? record.review_state.replaceAll("_", " ") : `Replay ${record.replay_state.toLowerCase().replaceAll("_", " ")}`, detail: `${record.extractor_name} · ${record.rule_id}`, file: record.file_path, line: record.span.start_line, selection: { type: "edge", id: record.edge_id }, state: record.review_state !== "NONE" ? record.review_state : record.replay_state });
  }
  const boundSites = new Set(analysis.bindings.map((item) => item.site_id));
  for (const site of analysis.sites) {
    if (!boundSites.has(site.site_id) && ["CALL_CALLEE", "VALUE_REFERENCE", "IMPORT_REFERENCE", "CALLBACK_REFERENCE"].includes(site.kind)) items.push({ key: `site:${site.site_id}`, title: "Unresolved site", detail: `${site.kind} · ${site.expression}`, file: site.file_path, line: site.span.start_line, selection: { type: "site", id: site.site_id }, state: "UNRESOLVED" });
  }
  for (const [index, diagnostic] of analysis.diagnostics.entries()) {
    const matchingEvidence = diagnostic.file_path && diagnostic.span
      ? analysis.provenance.find((record) => record.file_path === diagnostic.file_path && record.span.start_byte === diagnostic.span?.start_byte && record.span.end_byte === diagnostic.span?.end_byte)
      : undefined;
    const matchingSite = diagnostic.file_path && diagnostic.span
      ? analysis.sites.find((site) => site.file_path === diagnostic.file_path && site.span.start_byte === diagnostic.span?.start_byte && site.span.end_byte === diagnostic.span?.end_byte)
      : undefined;
    items.push({ key: `diagnostic:${index}`, title: diagnostic.code.replaceAll("_", " "), detail: diagnostic.message, file: diagnostic.file_path, line: diagnostic.span?.start_line, state: "DIAGNOSTIC", selection: matchingEvidence ? { type: "edge", id: matchingEvidence.edge_id } : matchingSite ? { type: "site", id: matchingSite.site_id } : undefined });
  }
  const rank: Record<string, number> = { RESOLVER_DISAGREEMENT: 0, REPLAY_FAILED: 1, RUNTIME_MISMATCH: 2, SCHEMA_WARNING: 3, HUMAN_REVIEW_REQUIRED: 4, UNRESOLVED: 5, DIAGNOSTIC: 6 };
  return items.sort((a, b) => (rank[a.state] ?? 7) - (rank[b.state] ?? 7) || a.file?.localeCompare(b.file ?? "") || a.key.localeCompare(b.key));
}
