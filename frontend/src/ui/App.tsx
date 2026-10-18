import { exactCallAliases } from "./callEvidence.js";
import { HighlightedSourceViewer, SourceCode } from "./SourceEvidence";
import { connectedRelations, expandConnections, preserveGraphPositions } from "./graphFocus.js";
import { IngestionProgress } from "./IngestionProgress";
import { requestJson, RequestScope } from "./request.js";
import { API_BASE_URL } from "../apiConfig";
import { resolveRepositoryPath } from "./repositoryPath.js";
import { selectEvidencePathEdge } from "./graphPathSelection.js";
import { graphViewportHeight, relationshipNodeY, unresolvedSitePosition } from "./graphLayout.js";
import { useCallback, useEffect, useMemo, useRef, useState, useId } from "react";
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
import { evidenceStateLabel, humanBindingLabel, humanDispatchState, humanExternalName, humanRelationshipLabel, humanRepositoryName, humanSymbolName, replayStateLabel } from "./evidencePresentation.js";
import { isSnapshotInProgress, snapshotProgressMessage, withSnapshotStatus } from "./snapshotStatus.js";

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
type SourcePreview = { context?: { start_line: number; lines: string[] }; file_path: string; file_content_hash: string; start_byte: number; end_byte: number; start_line: number; start_column: number; end_line: number; end_column: number; text: string };
type Selection = { type: "entity" | "site" | "edge"; id: string };
type GraphEndpoint = { id: string; label: string; detail: string; type: "entity" | "site" | "external" };
type GraphRelation = { id: string; source: GraphEndpoint; target: GraphEndpoint; label: string; state: string; visualClass: string; file: string; selection: Selection };
type GraphLooseSite = { id: string; label: string; detail: string; file: string; selection: Selection };
type ReviewItem = { key: string; title: string; detail: string; file?: string; line?: number; selection?: Selection; state: string };

const API = API_BASE_URL;
const HOSTED = import.meta.env.PROD && !API;

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  return requestJson<T>(`${API}${path}`, init);
}

export default function App() {
  const [path, setPath] = useState("");
  const [snapshots, setSnapshots] = useState<SnapshotRow[]>([]);
  const [snapshotId, setSnapshotId] = useState("");
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadingMessage, setLoadingMessage] = useState("");
  const [replayingEdgeId, setReplayingEdgeId] = useState<string | null>(null);
  const [sourceAttempt, setSourceAttempt] = useState(0);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState("");
  const [connectionsSelection, setConnectionsSelection] = useState<Selection | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [page, setCurrentPage] = useState<WorkspacePage>("investigate");
  function setPage(next: WorkspacePage) {
    setNotice("");
    setCurrentPage(next);
    if (location.hash !== `#${next}`) history.pushState(null, "", `#${next}`);
  }
  useEffect(() => {
    const onBack = () => {
      const next = location.hash.slice(1) as WorkspacePage;
      setCurrentPage(workspaceNavigation.some((item) => item.id === next) ? next : "investigate");
    };
    window.addEventListener("popstate", onBack);
    return () => window.removeEventListener("popstate", onBack);
  }, []);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [source, setSource] = useState<SourcePreview | null>(null);
  const [showPremises, setShowPremises] = useState(false);
  const [copied, setCopied] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [queueFilter, setQueueFilter] = useState("ALL");
  const [focusedEvidence, setFocusedEvidence] = useState<EvidenceItem | null>(null);
  const [runtimeRefreshRunId, setRuntimeRefreshRunId] = useState<string | null>(null);
  const [runtimeCalls, setRuntimeCalls] = useState<RuntimeCall[]>([]);
  const [highlightedPathEdgeIds, setHighlightedPathEdgeIds] = useState<string[]>([]);
  const [savedInvestigations, setSavedInvestigations] = useState<InvestigationSummary[]>([]);
  const [historyRequestId, setHistoryRequestId] = useState<string | null>(null);
  const snapshotScope = useRef(new RequestScope());
  const replayLock = useRef(false);
  const ingestionLock = useRef(false);
  const [snapshotsLoading, setSnapshotsLoading] = useState(true);
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
    setSnapshotsLoading(true);
    try {
      const rows = (await api<{ snapshots: SnapshotRow[] }>("/api/snapshots?limit=50")).snapshots;
      setSnapshots(rows.filter((item) => /^snap_[a-f0-9]{64}$/.test(item.snapshot_id)));
      if (!startupSnapshotChecked.current) {
        startupSnapshotChecked.current = true;
        const pendingId = localStorage.getItem(`codegraph:ingestion:${API}`);
        if (pendingId) { void resumeIngestion(pendingId); return; }
        const demo = import.meta.env.VITE_CODEGRAPH_DEMO_SNAPSHOT_ID;
        const ready = rows.find((item) => item.snapshot_id === demo && item.status === "READY") ?? rows.find((item) => item.status === "READY" && /^snap_[a-f0-9]{64}$/.test(item.snapshot_id));
        if (ready) void openSnapshot(ready.snapshot_id);
      }
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not load repositories"); }
    finally { setSnapshotsLoading(false); }
  }

  useEffect(() => { void refreshSnapshots(); }, []);

  useEffect(() => {
    if (!analysis || !isSnapshotInProgress(analysis.status)) return;
    const activeSnapshotId = analysis.snapshot_id;
    let stopped = false;
    let timer = 0;
    const refreshStatus = async () => {
      try {
        const result = await api<{ snapshot: Record<string, unknown> }>(`/api/snapshots/${encodeURIComponent(activeSnapshotId)}`);
        if (stopped) return;
        const status = String(result.snapshot.status ?? analysis.status);
        setAnalysis((current) => current?.snapshot_id === activeSnapshotId ? { ...current, status } : current);
        setSnapshots((current) => current.map((item) => item.snapshot_id === activeSnapshotId ? { ...item, status } : item));
        if (["FAILED", "CANCELLED", "INTERRUPTED"].includes(status)) {
          setError(`Snapshot preparation ended with status ${status.toLowerCase()}. Cached analysis is available in History.`);
          return;
        }
        if (status !== "READY") timer = window.setTimeout(() => void refreshStatus(), 2500);
      } catch {
        if (!stopped) timer = window.setTimeout(() => void refreshStatus(), 5000);
      }
    };
    timer = window.setTimeout(() => void refreshStatus(), 1500);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [analysis?.snapshot_id, analysis?.status]);

  async function followIngestion(started: Job, request: ReturnType<RequestScope["begin"]>) {
    let current = started;
    setJob(current); setSnapshotId(current.snapshot_id);
    localStorage.setItem(`codegraph:ingestion:${API}`, current.snapshot_id);
    while (!["COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"].includes(current.status)) {
      const status = await api<{ snapshot: Record<string, unknown> }>(`/api/snapshots/${encodeURIComponent(current.snapshot_id)}`, { signal: request.signal });
      if (!request.current()) return;
      const metadata = status.snapshot;
      current = { ...current, status: metadata.status === "READY" ? "COMPLETED" : String(metadata.job_status ?? metadata.status ?? current.status), error: metadata.error ? String(metadata.error) : null };
      setJob(current);
      if (!["COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"].includes(current.status)) await delay(1500);
      if (!request.current()) return;
    }
    localStorage.removeItem(`codegraph:ingestion:${API}`);
    if (current.status !== "COMPLETED") throw new Error(current.error ?? `Analysis ${current.status.toLowerCase()}. Retry the repository to start a new job.`);
    if (request.current()) { await openSnapshot(current.snapshot_id); await refreshSnapshots(); }
  }

  async function resumeIngestion(id: string) {
    if (ingestionLock.current) return;
    ingestionLock.current = true;
    const request = snapshotScope.current.begin();
    setBusy(true); setLoadingMessage("Reconnecting to repository analysis…");
    try { await followIngestion({ job_id: "", snapshot_id: id, status: "QUEUED" }, request); }
    catch (reason) { if (request.current()) setError(reason instanceof Error ? reason.message : "Could not reconnect. Refresh to retry."); }
    finally { ingestionLock.current = false; if (request.current()) { setBusy(false); setLoadingMessage(""); } }
  }

  async function startIngestion() {
    if (!path.trim() || busy || ingestionLock.current) return;
    ingestionLock.current = true;
    const request = snapshotScope.current.begin();
    setBusy(true); setLoadingMessage("Preparing repository analysis…"); setError(""); setNotice(""); setAnalysis(null); setSelection(null); setSource(null); setJob(null);
    try {
      const started = await api<Job>("/api/snapshots/ingest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: path.trim(), include_untracked: false }), signal: request.signal });
      if (!request.current()) return;
      await followIngestion(started, request);
    } catch (reason) { if (request.current()) setError(reason instanceof Error ? reason.message : "Could not start repository ingestion"); }
    finally { ingestionLock.current = false; if (request.current()) { setBusy(false); setLoadingMessage(""); } }
  }

  async function cancelIngestion() {
    if (!job || !snapshotId) return;
    setJob({ ...job, cancel_requested: true });
    try { setJob(await api<Job>(`/api/snapshots/${encodeURIComponent(snapshotId)}/cancel`, { method: "POST" })); }
    catch (reason) { setJob((current) => current ? { ...current, cancel_requested: false } : current); setError(reason instanceof Error ? reason.message : "Could not cancel ingestion"); }
  }

  async function openSnapshot(id: string) {
    if (!id) return;
    const request = snapshotScope.current.begin();
    setError(""); setNotice(""); setAnalysis(null); setSnapshotId(id); setSelection(null); setSource(null); setRuntimeRefreshRunId(null); setJob(null); setReplayingEdgeId(null); setRuntimeCalls([]); setHighlightedPathEdgeIds([]); setFocusedEvidence(null); setSavedInvestigations([]); setBusy(true); setLoadingMessage("Opening saved snapshot…");
    try {
      const status = await api<{ snapshot: Record<string, unknown> }>(`/api/snapshots/${encodeURIComponent(id)}`, { signal: request.signal });
      if (!request.current()) return;
      const stage = String(status.snapshot.job_status ?? status.snapshot.status);
      if (stage === "QUEUED" || isSnapshotInProgress(stage)) {
        await followIngestion({ job_id: "", snapshot_id: id, status: stage }, request);
        return;
      }
      const loaded = await api<Analysis>(`/api/snapshots/${encodeURIComponent(id)}/workspace`, { signal: request.signal });
      if (!request.current()) return;
      const snapshotStatus = String(status.snapshot.status ?? loaded.status);
      setAnalysis(withSnapshotStatus(loaded, status.snapshot));
      if (snapshotStatus !== "READY" && !isSnapshotInProgress(snapshotStatus)) setError(`This snapshot is ${snapshotStatus.toLowerCase()} and is kept out of normal graph queries.`);
      setPage("investigate");
    } catch (reason) { if (request.current()) setError(reason instanceof Error ? reason.message : "Could not load this snapshot"); }
    finally { if (request.current()) { setBusy(false); setLoadingMessage(""); } }
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
    setSource(null); setSourceError(""); setConnectionsSelection(null); setShowPremises(false);
    if (!analysis || !selection || !selectedPath || !selectedSpan) { setSourceLoading(false); return; }
    let active = true;
    const controller = new AbortController();
    setSourceLoading(true);
    const params = new URLSearchParams({ file_path: selectedPath, start_byte: String(selectedSpan.start_byte), end_byte: String(selectedSpan.end_byte), context_lines: "3" });
    void api<SourcePreview>(`/api/snapshots/${encodeURIComponent(analysis.snapshot_id)}/source?${params.toString()}`, { signal: controller.signal }).then((value) => { if (active) setSource(value); }).catch((reason) => { if (active) setSourceError(reason instanceof Error ? reason.message : "Could not retrieve source. Try again."); }).finally(() => { if (active) setSourceLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [analysis?.snapshot_id, selection?.id, selectedPath, selectedSpan, sourceAttempt]);

  const reviewItems = useMemo(() => buildReviewQueue(analysis), [analysis]);
  const exampleQuestions = useMemo(() => {
    if (!analysis) return [];
    const callable = analysis.entities.find((item) => item.file_path.startsWith("src/") && ["FUNCTION", "METHOD"].includes(item.kind.toUpperCase()) && item.name === "parse_args") ?? analysis.entities.find((item) => item.file_path.startsWith("src/") && ["FUNCTION", "METHOD"].includes(item.kind.toUpperCase())) ?? analysis.entities.find((item) => ["FUNCTION", "METHOD"].includes(item.kind.toUpperCase()));
    if (!callable) return [];
    return [
      `Where is ${callable.qualified_name} defined?`,
      `Who calls ${callable.qualified_name}?`,
      `What does ${callable.qualified_name} call?`,
    ];
  }, [analysis]);

  function startNewRepository() {
    snapshotScope.current.cancel();
    setBusy(false); setLoadingMessage(""); setJob(null);
    startupSnapshotChecked.current = true;
    setAnalysis(null); setSnapshotId(""); setSelection(null); setSource(null); setFocusedEvidence(null); setPage("investigate");
  }

  async function replayEdge(edgeId: string) {
    if (!analysis || replayingEdgeId || replayLock.current) return;
    replayLock.current = true;
    setError(""); setNotice("");
    const contextId = analysis.snapshot_id;
    const epoch = snapshotScope.current;
    const context = epoch.begin();
    setReplayingEdgeId(edgeId);
    try {
      const result = await api<{ message: string; outcome: string }>(`/api/evidence/${encodeURIComponent(edgeId)}/replay`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ snapshot_id: analysis.snapshot_id }) });
      if (!context.current()) return;
      setNotice(result.outcome === "PASS" ? "Code evidence reproduced from the saved source. This does not establish runtime execution." : `${replayStateLabel(result.outcome)}. ${result.message}`);
      const [status, updated] = await Promise.all([
        api<{ snapshot: Record<string, unknown> }>(`/api/snapshots/${encodeURIComponent(analysis.snapshot_id)}`),
        api<Analysis>(`/api/snapshots/${encodeURIComponent(analysis.snapshot_id)}/workspace`),
      ]);
      if (!context.current() || updated.snapshot_id !== contextId) return;
      const refreshed = withSnapshotStatus(updated, status.snapshot);
      setAnalysis(refreshed);
      setSnapshots((current) => current.map((item) => item.snapshot_id === refreshed.snapshot_id ? { ...item, status: refreshed.status } : item));
    } catch (reason) { if (context.current()) setError(reason instanceof Error ? reason.message : "Replay failed"); }
    finally { replayLock.current = false; setReplayingEdgeId(null); }
  }

  function loadNeighbors() {
    if (!analysis || !selection) return;
    setConnectionsSelection(selection);
  }

  async function copyDebugId() {
    if (!selection) return;
    try { await navigator.clipboard.writeText(selection.id); setCopied(true); window.setTimeout(() => setCopied(false), 1400); }
    catch { setError("Could not copy the identifier. It is available under Technical details."); }
  }

  const { displayNames, displayContexts, pathEvidenceInfo } = useMemo(() => {
    const entities = new Map(analysis?.entities.map((item) => [item.entity_id, item]));
    const bindings = new Map(analysis?.bindings.map((item) => [item.edge_id, item]));
    const derivedIndex = new Map(analysis?.derived_edges.map((item) => [item.edge_id, item]));
  const displayNames = Object.fromEntries([
    ...((analysis?.entities ?? []).flatMap((entity) => {
      const parent = entities.get(entity.parent_id ?? "");
      const name = humanSymbolName(entity.name, entity.kind);
      const contextName = parent?.kind === "Class" ? `${parent.name}.${name}` : `${entity.qualified_name.split(".").slice(-2, -1)[0] ?? ""}.${name}`;
      return [[entity.entity_id, name], [entity.qualified_name, contextName]] as const;
    })),
    ...((analysis?.external_symbols ?? []).map((symbol) => {
      return [symbol.external_symbol_id, humanExternalName(symbol.semantic_symbol, symbol.distribution_name, symbol.module_name)] as const;
    })),
  ]);
  const displayContexts = Object.fromEntries((analysis?.entities ?? []).map((entity) => {
    const parent = entities.get(entity.parent_id ?? "");
    return [entity.entity_id, `${parent?.kind === "Class" ? parent.name : entity.qualified_name.split(".").slice(-2, -1)[0] ?? "Code"} · ${entity.file_path}:${entity.span.start_line}`];
  }));
  const pathEvidenceInfo = Object.fromEntries((analysis?.provenance ?? []).map((record) => {
    const binding = bindings.get(record.edge_id);
    const derived = derivedIndex.get(record.edge_id);
    const modality = binding?.binding_state ?? (derived ? derived.relation === "MAY_DISPATCH_TO" ? "MAY" : "DERIVED" : "EXACT");
    return [record.edge_id, { location: `${record.file_path}:${record.span.start_line}`, state: evidenceStateLabel({ modality, replayState: record.replay_state, reviewState: record.review_state }) }];
  }));
    return { displayNames, displayContexts, pathEvidenceInfo };
  }, [analysis]);
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
      <div className="sidebar-footer"><span>{analysis ? repositoryName : "Your workspace"}</span></div>
    </aside>
    <section className="product-main">
      <header className="product-header">
        <div className="header-page"><span>CODEGRAPH</span><h1>{workspaceTitle(page)}</h1></div>
        <div className="header-context">
          {analysis ? <>
            <label className="sr-only" htmlFor="active-snapshot">Repository and snapshot</label>
            <select id="active-snapshot" value={snapshotId} onChange={(event) => void openSnapshot(event.target.value)} aria-label="Switch repository snapshot">
              {snapshots.some((item) => item.snapshot_id === analysis.snapshot_id) ? null : <option value={analysis.snapshot_id}>{repositoryName} · {analysis.repository.head_commit?.slice(0, 7) ?? "uncommitted"}</option>}
              {snapshots.map((item) => <option key={item.snapshot_id} value={item.snapshot_id}>{snapshotOptionLabel(item)}</option>)}
            </select>
            <button className="header-secondary-action" onClick={startNewRepository}>Add repository</button>
          </> : <><span className="context-placeholder">{HOSTED ? "Private Python workspace" : "Local Python workspace"}</span>{snapshots.length > 0 && <select value={snapshotId} onChange={(event) => void openSnapshot(event.target.value)} aria-label="Open repository snapshot"><option value="">Choose a recent snapshot</option>{snapshots.map((item) => <option key={item.snapshot_id} value={item.snapshot_id}>{snapshotOptionLabel(item)}</option>)}</select>}</>}
        </div>
      </header>
      <div className={`product-content ${analysis && selection && (page === "investigate" || page === "graph") ? "has-inspector" : ""}`}>
        {(busy && !job || snapshotsLoading && !analysis) && <div className="loading-banner" role="status" aria-live="polite"><LoaderCircle className="spin" size={17}/><span>{loadingMessage || "Finding saved repositories…"}</span></div>}
        {!analysis && busy && !job ? <div className="workspace-skeleton" aria-hidden="true"><div/><div/><div/></div> : !analysis ? page === "history" ? <div className="workspace-view history-view">
          <div className="view-introduction"><div><span className="section-label">SAVED WORK · LOCAL SNAPSHOTS</span><h2>History</h2><p>Open a previously analyzed repository snapshot or start a new analysis.</p></div><button className="header-secondary-action" onClick={startNewRepository}>Analyze a repository</button></div>
          <section className="history-section"><div className="history-section-heading"><h3>Repository snapshots</h3><button className="text-button" onClick={() => void refreshSnapshots()}><RefreshCw size={14}/> Refresh</button></div>{snapshots.length ? <SnapshotList snapshots={snapshots} selectedId={snapshotId} onSelect={(id) => void openSnapshot(id)} onRefresh={() => void refreshSnapshots()}/> : <p className="history-empty">No analyzed snapshots are available yet. Start with a local Python repository to create one.</p>}</section>
          <section className="history-section"><div className="history-section-heading"><h3>Saved investigations</h3><span>0</span></div><p className="history-empty">Saved questions appear here after you open a repository and run an investigation.</p></section>
        </div> : <section className="repository-start">
          <div className="start-intro"><span className="section-label">START WITH YOUR CODE</span><h2>Get to know your code. Follow the evidence.</h2><p>Ask questions about a Python repository, see how its functions connect, and open the source behind each answer.</p>
            {snapshots.some((item) => item.status === "READY") && <button className="demo-entry" onClick={() => { const demo = snapshots.find((item) => item.snapshot_id === import.meta.env.VITE_CODEGRAPH_DEMO_SNAPSHOT_ID && item.status === "READY") ?? snapshots.find((item) => item.status === "READY"); if (demo) void openSnapshot(demo.snapshot_id); }}><Waypoints size={18}/><span><b>Explore the ready-to-use demo</b><small>Ask a question and inspect real source. No setup needed.</small></span><ArrowUpRight size={16}/></button>}<ol className="workflow-summary"><li><span>1</span><div><b>Analyze a repository</b><small>{HOSTED ? "Analyze a checkout in your private workspace." : "Create a source-bound snapshot on this machine."}</small></div></li><li><span>2</span><div><b>Investigate a question</b><small>Get an answer with a traceable path through code.</small></div></li><li><span>3</span><div><b>Review changes and tests</b><small>See impact and approve any sandboxed execution.</small></div></li></ol>
          </div>
          <section className="repository-form-panel" aria-labelledby="repository-form-title">
            <div><span className="section-label">{HOSTED ? "SERVER REPOSITORY" : "LOCAL REPOSITORY"}</span><h3 id="repository-form-title">Analyze a Python repository</h3><p>{HOSTED ? "Enter the path to an allowlisted Git checkout on the analysis server." : "Enter the absolute path to a Git checkout available to this machine."}</p></div>
            <form className="repository-form" onSubmit={(event) => { event.preventDefault(); void startIngestion(); }}>
              <label htmlFor="repository-path">Repository path</label><div className="repository-path-input"><Search size={16}/><input id="repository-path" value={path} onChange={(event) => setPath(event.target.value)} placeholder="/path/to/python-project" autoComplete="off" spellCheck={false}/></div>
              <button type="submit" disabled={!path.trim() || busy}>{busy ? <><LoaderCircle className="spin" size={15}/>{job?.status?.replaceAll("_", " ") ?? "Starting analysis"}</> : <>Analyze repository <ArrowUpRight size={16}/></>}</button>
            </form>
            <div className="repository-form-foot"><span><ShieldCheck size={14}/> {HOSTED ? "Source stays in your private workspace" : "Source stays on this machine"}</span><span>Python · Git history · manifests</span></div>
            {job && <IngestionProgress status={job.status} cancelling={job.cancel_requested} onCancel={() => void cancelIngestion()}/>}
            {snapshots.length > 0 && <div className="available-snapshots"><b>Or open a recent snapshot</b><SnapshotList snapshots={snapshots.slice(0, 4)} selectedId={snapshotId} onSelect={(id) => void openSnapshot(id)} onRefresh={() => void refreshSnapshots()} compact/> </div>}
          </section>
        </section> : <>
          <div className="workspace-view" hidden={page !== "investigate"}>
            <div className="view-introduction"><div><span className="section-label">{repositoryName} · {analysis.repository.head_commit?.slice(0, 7) ?? "working tree"}</span><h2>Understand your code, visually.</h2><p>{canUseValidatedSnapshot(analysis.status) ? "Ask a question. Explore connections. See the source behind the answer." : "This snapshot has not passed publication validation."}</p></div></div>
            {canUseValidatedSnapshot(analysis.status) ? <InvestigationWorkspace key={analysis.snapshot_id} snapshotId={analysis.snapshot_id} selected={selection} onInspectPathEdges={inspectEvidencePathEdges} onFocusEvidence={setFocusedEvidence} focusedEvidence={focusedEvidence} runtimeRefreshRunId={runtimeRefreshRunId} onRuntimeCalls={handleRuntimeCalls} onEvidencePathChange={handleEvidencePathChange} displayNames={displayNames} displayContexts={displayContexts} pathEvidenceInfo={pathEvidenceInfo} exampleQuestions={exampleQuestions} onSavedInvestigationsChange={handleSavedInvestigations} requestedInvestigationId={historyRequestId} onHistoryRequestHandled={handleHistoryRequestOpened} onInspect={(item) => { setSelection(item); setPage("graph"); }} graph={null} /> : <UnreadySnapshotNotice status={analysis.status}/>}
          </div>
          <div className="workspace-view" hidden={page !== "graph"}>
            <div className="view-introduction"><div><span className="section-label">SNAPSHOT GRAPH · {analysis.repository.head_commit?.slice(0, 7) ?? "working tree"}</span><h2>Follow the connections.</h2><p>{canUseValidatedSnapshot(analysis.status) ? "Boxes are code elements. Arrows show how they connect. Select either to see the evidence." : "Graph relationships are available after publication validation passes."}</p></div></div>

          </div>
          <div className="workspace-view shared-graph-view" hidden={page !== "investigate" && page !== "graph"}>
            {canUseValidatedSnapshot(analysis.status) ? <GraphExplorer key={analysis.snapshot_id} analysis={analysis} selection={selection} runtimeCalls={runtimeCalls} highlightedPathEdgeIds={highlightedPathEdgeIds} onSelect={setSelection}/> : <UnreadySnapshotNotice status={analysis.status}/>}
          </div>
          <div className="workspace-view" hidden={page !== "changes"}>
            <div className="view-introduction"><div><span className="section-label">REVIEW YOUR CHANGES</span><h2>Changes &amp; tests</h2><p>See what changed, trace what might be affected, and review tests before running them.</p></div></div>
            {canUseValidatedSnapshot(analysis.status) ? <DiffPlanningWorkspace key={analysis.snapshot_id} repositoryPath={resolveRepositoryPath(repositorySnapshot?.repository_root, analysis.repository.root)} targetSha={analysis.repository.head_commit} workingTreeDirty={analysis.repository.is_dirty} onRuntimeCompleted={setRuntimeRefreshRunId}/> : <UnreadySnapshotNotice status={analysis.status}/>}
          </div>
          <div className="workspace-view history-view" hidden={page !== "history"}>
            <div className="view-introduction"><div><span className="section-label">SAVED WORK · SNAPSHOT BOUND</span><h2>History</h2><p>Return to a repository snapshot or a saved investigation.</p></div><button className="header-secondary-action" onClick={startNewRepository}>Analyze another repository</button></div>
            <section className="history-section"><div className="history-section-heading"><h3>Saved investigations</h3><span>{savedInvestigations.length}</span></div>{savedInvestigations.length ? <div className="saved-investigations">{savedInvestigations.map((item) => <button key={item.investigation_id} className="saved-investigation-row" onClick={() => { setSelection(null); setFocusedEvidence(null); setHistoryRequestId(item.investigation_id); setPage("investigate"); }}><MessageSquareText size={16}/><span><b>{Object.entries(displayNames).filter(([id]) => item.last_question.includes(id)).sort((a,b) => b[0].length-a[0].length).reduce((text,[id,label]) => text.replaceAll(id,label),item.last_question)}</b><small>{new Date(item.updated_at * 1000).toLocaleString()} · {item.note_count} notes</small></span><ChevronRight size={16}/></button>)}</div> : <p className="history-empty">Saved questions appear here after an investigation.</p>}</section>
            <section className="history-section"><div className="history-section-heading"><h3>Repository snapshots</h3><button className="text-button" onClick={() => void refreshSnapshots()}><RefreshCw size={14}/> Refresh</button></div><SnapshotList snapshots={snapshots} selectedId={snapshotId} onSelect={(id) => void openSnapshot(id)} onRefresh={() => void refreshSnapshots()}/></section>
            <details className="advanced-history" onToggle={(event) => setReviewOpen(event.currentTarget.open)}><summary>Review queue and diagnostics</summary><div className="history-advanced-content">{reviewOpen && <ReviewQueue items={reviewItems} filter={queueFilter} setFilter={setQueueFilter} onSelect={(item) => { if (item.selection) { setSelection(item.selection); setPage("graph"); } }}/>} {analysis.diagnostics.length > 0 && <Diagnostics diagnostics={analysis.diagnostics}/>}<details className="snapshot-details"><summary><GitCommitHorizontal size={14}/> Snapshot details</summary><div className="snapshot-meta"><span>{analysis.repository.is_shallow ? "Shallow clone" : "Full history"}</span><span>{analysis.repository.is_dirty ? "Working tree dirty" : "Working tree clean"}</span><span>Python {analysis.analysis_environment?.python_version ?? "unknown"}</span><span>scip-python {analysis.analysis_environment?.scip_python_version ?? analysis.semantic_indexing}</span></div><div className="metrics"><Metric label="Python files" value={analysis.counts.analyzed_python_files ?? 0}/><Metric label="Definitions" value={analysis.entities.length}/><Metric label="Sites" value={analysis.sites.length}/><Metric label="Review items" value={reviewItems.length}/></div><details className="technical-details"><summary>Technical details</summary><code>{analysis.snapshot_id}</code></details></details></div></details>
          </div>
        </>}
        {analysis && canUseValidatedSnapshot(analysis.status) && selection && selected && (page === "investigate" || page === "graph") && <Inspector analysis={analysis} selection={selection} selected={selected} provenance={selectedProvenance} relatedProvenance={relatedProvenance} binding={selectedBinding} derived={selectedDerived} linkedSite={selectedSiteForEdge} source={source} sourceError={sourceError} onRetrySource={() => { setError(""); setSourceAttempt((current) => current + 1); }} sourceLoading={sourceLoading} replaying={replayingEdgeId === (selection.type === "edge" ? selection.id : "")} replayInProgress={replayingEdgeId !== null || replayLock.current} showPremises={showPremises} setShowPremises={setShowPremises} onReplay={replayEdge} onNeighbors={loadNeighbors} onCopy={() => void copyDebugId()} onClose={() => setSelection(null)} copied={copied} onSelect={setSelection}/>}
        {analysis && connectionsSelection && <ConnectionsDialog key={analysis.snapshot_id} analysis={analysis} selection={connectionsSelection} runtimeCalls={runtimeCalls} onClose={() => setConnectionsSelection(null)} onSelect={(item) => { setSelection(item); setConnectionsSelection(null); }}/>}
        {error && <div className="error-banner" role="alert"><AlertTriangle size={15}/><span>{error}</span>{!analysis && !busy && <button className="text-button" onClick={() => { if (job) void resumeIngestion(job.snapshot_id); else if (snapshotId) void openSnapshot(snapshotId); else void refreshSnapshots(); }}>Retry</button>}<button aria-label="Dismiss" onClick={() => setError("")}><X size={14}/></button></div>}
        {notice && <div className="notice-banner" role="status"><Check size={15}/><span>{notice}</span><button aria-label="Dismiss" onClick={() => setNotice("")}><X size={14}/></button></div>}
      </div>
    </section>
  </main>;
}

function delay(ms: number) { return new Promise((resolve) => window.setTimeout(resolve, ms)); }
function UnreadySnapshotNotice({ status }: { status: string }) {
  const processing = isSnapshotInProgress(status);
  return <section className={`panel-card snapshot-unavailable ${processing ? "snapshot-progress" : ""}`} role={processing ? "status" : "alert"} aria-live="polite">
    {processing ? <LoaderCircle className="spin" size={20}/> : <AlertTriangle size={20}/>}
    <div><h3>{processing ? "Preparing this snapshot" : `Snapshot ${status.toLowerCase()}`}</h3><p>{processing ? `${snapshotProgressMessage(status)} This page will update when the check is complete.` : "Cached analysis is retained in History for diagnostics. This snapshot is not available for investigation."}</p>{processing && <div className="snapshot-progress-track" aria-label="Snapshot preparation in progress"><span/></div>}</div>
  </section>;
}
function Metric({ label, value }: { label: string; value: number }) { return <div className="metric"><span>{label}</span><strong>{value.toLocaleString()}</strong></div>; }

function snapshotOptionLabel(item: SnapshotRow) {
  const date = item.updated_at ? new Date(item.updated_at * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "Saved version";
  return `${humanRepositoryName(item.repository_root)} · ${item.snapshot_id === import.meta.env.VITE_CODEGRAPH_DEMO_SNAPSHOT_ID ? "Demo · " : ""}${date} · ${item.head_commit?.slice(0, 7) ?? "working tree"}`;
}

function SnapshotList({ snapshots, selectedId, onSelect, onRefresh, compact = false }: { snapshots: SnapshotRow[]; selectedId: string; onSelect: (id: string) => void; onRefresh: () => void; compact?: boolean }) {
  if (!snapshots.length) return null;
  return <section className={`snapshot-list ${compact ? "compact" : ""}`}><div className="list-heading"><div><span className="section-label">RECENT SNAPSHOTS</span><span className="subtle">{snapshots.length} stored</span></div><button className="text-button" onClick={onRefresh}>Refresh list</button></div><div className="snapshot-list-items">{snapshots.slice(0, compact ? 4 : 8).map((item) => <button key={item.snapshot_id} className={`snapshot-list-row ${item.snapshot_id === selectedId ? "current" : ""}`} onClick={() => onSelect(item.snapshot_id)}><span className={`status-dot ${item.status.toLowerCase()}`}/><span className="snapshot-list-name">{humanRepositoryName(item.repository_root)}<small title={item.head_commit}>{item.head_commit?.slice(0, 7) ?? "Working tree"}{item.updated_at ? ` · ${new Date(item.updated_at * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}` : ""}{item.snapshot_id === import.meta.env.VITE_CODEGRAPH_DEMO_SNAPSHOT_ID ? " · Demo" : ""}</small></span><span className="snapshot-list-status">{item.snapshot_id === selectedId ? "Open" : item.status === "READY" ? "Ready to explore" : item.status.toLowerCase().replaceAll("_", " ")}</span><ChevronRight size={14}/></button>)}</div></section>;
}

function ConnectionsDialog({ analysis, selection, runtimeCalls, onClose, onSelect }: { analysis: Analysis; selection: Selection; runtimeCalls: RuntimeCall[]; onClose: () => void; onSelect: (item: Selection) => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const entity = analysis.entities.find((item) => item.entity_id === selection.id);
  const site = analysis.sites.find((item) => item.site_id === selection.id);
  const title = entity ? humanSymbolName(entity.name, entity.kind) : site?.expression ?? "this connection";
  useEffect(() => { const dialog = dialogRef.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog ref={dialogRef} className="connections-viewer source-viewer" aria-labelledby={titleId} onCancel={onClose}>
    <div className="source-viewer-heading"><div><h2 id={titleId}>Connections to {title}</h2><p>Only code directly connected to this selection. Your main graph stays unchanged.</p></div><button type="button" className="icon-button" aria-label="Close connections" onClick={onClose}><X size={18}/></button></div>
    <GraphExplorer analysis={analysis} selection={selection} runtimeCalls={runtimeCalls} highlightedPathEdgeIds={[]} onSelect={onSelect} connectionFocus={selection}/>
    <p>Select a box or arrow to close this view and inspect its source.</p>
  </dialog>;
}

function GraphExplorer({ analysis, selection, runtimeCalls, highlightedPathEdgeIds, onSelect, connectionFocus }: { analysis: Analysis; selection: Selection | null; runtimeCalls: RuntimeCall[]; highlightedPathEdgeIds: string[]; onSelect: (item: Selection) => void; connectionFocus?: Selection }) {
  const [filter, setFilter] = useState("");
  const [browserOpen, setBrowserOpen] = useState(false);
  const [focusIds, setFocusIds] = useState<string[] | null>(null);
  const [focusHistory, setFocusHistory] = useState<Array<string[] | null>>([]);
  const [expansionMessage, setExpansionMessage] = useState("");
  const [kind, setKind] = useState<"all" | "nodes" | "sites" | "edges">("all");
  const [pathOnly, setPathOnly] = useState(false);
  const [visibleRowCount, setVisibleRowCount] = useState(100);
  const [visibleEdgeCount, setVisibleEdgeCount] = useState(12);
  const bindingsByEdge = useMemo(() => new Map(analysis.bindings.map((item) => [item.edge_id, item])), [analysis.bindings]);
  const derivedByEdge = useMemo(() => new Map(analysis.derived_edges.map((item) => [item.edge_id, item])), [analysis.derived_edges]);
  const entitiesById = useMemo(() => new Map(analysis.entities.map((item) => [item.entity_id, item])), [analysis.entities]);
  const sitesById = useMemo(() => new Map(analysis.sites.map((item) => [item.site_id, item])), [analysis.sites]);
  const externalSymbolsById = useMemo(() => new Map(analysis.external_symbols.map((item) => [item.external_symbol_id, item])), [analysis.external_symbols]);
  const callAliases = useMemo(() => exactCallAliases(analysis), [analysis]);
  const highlightedPath = useMemo(() => new Set(highlightedPathEdgeIds.map(id => callAliases.get(id) ?? id)), [highlightedPathEdgeIds, callAliases]);
  useEffect(() => { setPathOnly(highlightedPath.size > 0); setFocusIds(null); setFocusHistory([]); setFilter(""); setExpansionMessage(""); }, [highlightedPath]);
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
    const relationMatches = (item: GraphRelation) => !normalized || `${item.source.label} ${item.source.detail} ${item.target.label} ${item.target.detail} ${item.label} ${item.state} ${item.file}`.toLowerCase().includes(normalized);
    const staticEdges: GraphRelation[] = analysis.bindings.flatMap((binding) => {
      const record = provenanceByEdge.get(binding.edge_id);
      const site = sitesById.get(binding.site_id);
      if (!record || !site) return [];
      const targetId = binding.target_entity_id ?? binding.external_symbol_id;
      if (!targetId) return [];
      const source = endpoint(site.context_id, site.expression);
      const target = endpoint(targetId, "Bound target");
      const state = edgeStateLabel(record, binding);
      const item: GraphRelation = { id: binding.edge_id, source, target, label: humanBindingLabel(site.kind), state, visualClass: edgeVisualClass(state, record, binding), file: `${site.file_path}:${site.span.start_line}`, selection: { type: "edge", id: binding.edge_id } };
      return relationMatches(item) ? [item] : [];
    });
    const derivedEdges: GraphRelation[] = analysis.derived_edges.flatMap((derived) => {
      if (callAliases.has(derived.edge_id)) return [];
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
    const relations = [...staticEdges, ...derivedEdges, ...observed];
    const focused = connectionFocus ? connectedRelations(relations, callAliases.get(connectionFocus.id) ?? connectionFocus.id, sitesById.get(connectionFocus.id)?.context_id) : relations;
    return focused.sort((a, b) => Number(!highlightedPath.has(a.id)) - Number(!highlightedPath.has(b.id)) || Number(!a.source.detail.startsWith("src/")) - Number(!b.source.detail.startsWith("src/")) || a.source.label.localeCompare(b.source.label) || a.target.label.localeCompare(b.target.label) || a.label.localeCompare(b.label));
  }, [analysis, callAliases, bindingsByEdge, derivedByEdge, entitiesById, externalSymbolsById, highlightedPath, normalized, runtimeCalls, sitesById, pathOnly, connectionFocus]);
  const initialEdges = useMemo(() => {
    const seed = matchingEdges.find((edge) => edge.source.type === "entity" && edge.target.type === "entity" && edge.source.detail.startsWith("src/")) ?? matchingEdges[0];
    return seed ? matchingEdges.filter((edge) => edge.source.id === seed.source.id || edge.target.id === seed.source.id).slice(0, 6) : [];
  }, [matchingEdges]);
  const visibleEdges = useMemo(() => (connectionFocus ? matchingEdges : focusIds ? matchingEdges.filter((edge) => focusIds.includes(edge.id)) : pathOnly ? matchingEdges.filter((edge) => highlightedPath.has(edge.id)) : normalized ? matchingEdges : initialEdges).slice(0, Math.max(visibleEdgeCount, Math.min(72, highlightedPath.size))), [matchingEdges, focusIds, pathOnly, normalized, initialEdges, visibleEdgeCount, highlightedPath, connectionFocus]);
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
  const updateFilter = (value: string) => { setFilter(value); setExpansionMessage(""); setFocusIds(null); setPathOnly(false); setVisibleRowCount(100); setVisibleEdgeCount(12); };
  const updateKind = (value: typeof kind) => { setKind(value); setVisibleRowCount(100); };
  const statusCount = (status: string) => matchingEdges.filter((edge) => edge.visualClass === status).length;
  if (connectionFocus) return <section className="focused-connections">
    <p className="connections-count">{visibleEdges.length} of {matchingEdges.length} direct connections{matchingEdges.length > 72 ? " · showing up to 72" : ""}</p>
    {matchingEdges.length > 0 ? <GraphCanvas edges={visibleEdges} unresolved={[]} selection={selection} highlightedPathEdgeIds={highlightedPath} fitRequest={visibleEdges.length} counts={{ review: statusCount("review") }} totalEdges={matchingEdges.length} provenanceCount={analysis.provenance.length} runtimeCount={0} onSelect={onSelect} onShowMore={() => setVisibleEdgeCount((count) => Math.min(count + 12, 72))}/> : <p>No direct connections were identified for this selection in the saved code. This does not prove that no connections exist.</p>}
  </section>;
  return <section className="explorer-shell">
    <div className="explorer-toolbar"><div><h3>How the code connects</h3><span className="subtle">{pathOnly ? "The path supporting your answer" : normalized ? "Connections matching your search" : focusIds ? "Your expanded connections" : "A connected starting point in this repository"}</span></div><div className="explorer-toolbar-actions">{focusHistory.length > 0 && <button className="header-secondary-action" onClick={() => { setFocusIds(focusHistory.at(-1) ?? null); setFocusHistory((history) => history.slice(0, -1)); setExpansionMessage("Returned to the previous focus."); }}>Back to focus</button>}{highlightedPath.size > 0 && <button className="header-secondary-action" aria-pressed={pathOnly} onClick={() => { setFocusIds(null); setPathOnly((current) => !current); }}>{pathOnly ? "Browse repository" : "Focus answer path"}</button>}<button className="header-secondary-action" aria-expanded={browserOpen} onClick={() => setBrowserOpen((value) => !value)}>{browserOpen ? "Hide code browser" : "Browse code"}</button></div></div>
    <div className="graph-search-row"><div className="explorer-search"><Search size={14}/><input value={filter} onChange={(event) => updateFilter(event.target.value)} aria-label="Filter graph by symbol, file, or relationship" placeholder="Find a function or file…" /></div><span>Click a box or arrow to inspect its source.</span>{normalized && <button className="text-button" onClick={() => updateFilter("")}>Clear search</button>}</div>
    {expansionMessage && <p className="graph-feedback" role="status">{expansionMessage}</p>}
    <div className={`explorer-body ${browserOpen ? "browser-open" : "browser-closed"}`}><div className="explorer-browser"><div className="filter-tabs">{(["all", "nodes", "sites", "edges"] as const).map((value) => <button className={kind === value ? "active" : ""} key={value} onClick={() => updateKind(value)}>{value === "all" ? "All" : value === "nodes" ? "Code" : value === "sites" ? "Call sites" : "Relationships"}</button>)}</div><div className="explorer-rows">{rows.map((row) => <button aria-pressed={selection?.id === row.id} className={`explorer-row ${selection?.id === row.id ? "selected" : ""}`} key={`${row.type}-${row.id}`} onClick={() => { onSelect({ type: row.type, id: row.id }); const connected = expandConnections(matchingEdges, [], row.id, sitesById.get(row.id)?.context_id); if (connected.length) { setFocusHistory((history) => [...history, visibleEdges.map((edge) => edge.id)]); setFocusIds(connected); setVisibleEdgeCount(connected.length); setPathOnly(false); } }}><span className={`object-icon ${row.type}`}>{row.type === "entity" ? <Boxes size={13}/> : row.type === "site" ? <FileCode2 size={13}/> : <Link2 size={13}/>}</span><span className="explorer-row-label"><b>{row.label}</b><small>{row.file}:{row.line}</small></span><span className="row-type">{row.type === "entity" ? "Code" : row.type === "site" ? "Call site" : "Relationship"}</span></button>)}{rows.length === 0 && <div className="empty-inline">No graph records match this filter.</div>}{rows.length === visibleRowCount && visibleRowCount < 400 && <button className="text-button" onClick={() => setVisibleRowCount((count) => Math.min(count + 100, 400))}>Show next {Math.min(100, 400 - visibleRowCount)} records</button>}</div></div>
    <GraphCanvas edges={visibleEdges} unresolved={unresolvedSites} selection={selection} highlightedPathEdgeIds={highlightedPath} fitRequest={0} counts={{verified:statusCount("verified"), exact:statusCount("exact"), derived:statusCount("derived"), possible:statusCount("possible"), runtime:statusCount("runtime"), review:statusCount("review"), unresolved:unresolvedCount}} totalEdges={matchingEdges.length} provenanceCount={analysis.provenance.length} runtimeCount={runtimeCalls.filter((call) => call.snapshot_id === analysis.snapshot_id).length} onSelect={onSelect} onShowMore={() => { setFocusIds(matchingEdges.slice(0, Math.min(visibleEdges.length + 12, 72)).map((edge) => edge.id)); setVisibleEdgeCount((count) => Math.min(count + 18, 72)); setPathOnly(false); }}/></div>
  </section>;
}

function GraphCanvas({ edges, unresolved, selection, highlightedPathEdgeIds, fitRequest, counts, totalEdges, provenanceCount, runtimeCount, onSelect, onShowMore }: { edges: GraphRelation[]; unresolved: GraphLooseSite[]; selection: Selection | null; highlightedPathEdgeIds: Set<string>; fitRequest: number; counts: Record<string, number>; totalEdges: number; provenanceCount: number; runtimeCount: number; onSelect: (item: Selection) => void; onShowMore: () => void }) {
  const graphId = useId().replaceAll(":", "");
  const svgRef = useRef<SVGSVGElement>(null);
  const nodes = useMemo(() => {
    const byId = new Map<string, GraphEndpoint>();
    for (const edge of edges) { byId.set(edge.source.id, edge.source); byId.set(edge.target.id, edge.target); }
    return [...byId.values()];
  }, [edges, unresolved]);
  const previousLayout = useRef<{ points: Map<string, { x: number; y: number }>; width: number; height: number } | null>(null);
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
    const stable = preserveGraphPositions(points, previousLayout.current, width, height);
    previousLayout.current = stable;
    return { ...stable, relationshipHeight };

  }, [edges, nodes, unresolved.length]);
  const [view, setView] = useState({ x: 0, y: 0, width: 960, height: 520 });
  const [drag, setDrag] = useState<{ x: number; y: number; viewX: number; viewY: number } | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const previousFitRequest = useRef(fitRequest);
  useEffect(() => {
    const requested = previousFitRequest.current !== fitRequest;
    previousFitRequest.current = fitRequest;
    if (requested || !("preserveView" in layout && layout.preserveView)) setView({ x: 0, y: 0, width: layout.width, height: layout.height });
  }, [layout, fitRequest]);
  const zoom = useCallback((factor: number) => setView((current) => {
    const ratio = Math.max(0.15, Math.min(2, current.width / layout.width * factor));
    const width = layout.width * ratio;
    const height = layout.height * ratio;
    return { x: current.x + (current.width - width) / 2, y: current.y + (current.height - height) / 2, width, height };
  }), [layout.width, layout.height]);
  useEffect(() => {
    const element = svgRef.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => { event.preventDefault(); zoom(event.deltaY > 0 ? 1.12 : 0.89); };
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
  }, [zoom]);
  const edgePairs = new Map<string, number[]>();
  for (const edge of edges) { const key = `${edge.source.id}\u0000${edge.target.id}`; edgePairs.set(key, [...(edgePairs.get(key) ?? []), edges.indexOf(edge)]); }
  const pairOffset = (edge: GraphRelation) => { const indices = edgePairs.get(`${edge.source.id}\u0000${edge.target.id}`) ?? []; return (indices.indexOf(edges.indexOf(edge)) - (indices.length - 1) / 2) * 22; };
  const relationColor = (visualClass: string) => visualClass === "possible" ? "#b68532" : ["unresolved", "review"].includes(visualClass) ? "#7f8b84" : "#548c72";
  const nodeSelection = (node: GraphEndpoint): Selection | undefined => node.type === "entity" || node.type === "site" ? { type: node.type, id: node.id } : undefined;
  const panStart = (event: React.PointerEvent<SVGSVGElement>) => { if (event.target !== event.currentTarget) return; const rect = event.currentTarget.getBoundingClientRect(); setDrag({ x: event.clientX / rect.width, y: event.clientY / rect.height, viewX: view.x, viewY: view.y }); event.currentTarget.setPointerCapture(event.pointerId); };
  const panMove = (event: React.PointerEvent<SVGSVGElement>) => { if (!drag) return; const rect = event.currentTarget.getBoundingClientRect(); setView((current) => ({ ...current, x: drag.viewX - (event.clientX / rect.width - drag.x) * current.width, y: drag.viewY - (event.clientY / rect.height - drag.y) * current.height })); };
  const svgText = (value: string, limit = 27) => value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
  // Pan and zoom change only the SVG viewport. Reuse the graph element tree so
  // every pointer movement does not re-render all labels, edges and hit targets.
  const graphContent = useMemo(() => <>
      <defs>{["verified", "exact", "possible", "runtime", "unresolved", "derived", "review"].map((state) => <marker key={state} id={`${graphId}-arrow-${state}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill={relationColor(state)}/></marker>)}</defs>
      <g className="graph-links">{edges.map((edge) => { const start = layout.points.get(edge.source.id); const end = layout.points.get(edge.target.id); if (!start || !end) return null; const offset = pairOffset(edge) || 8; const middle = (start.x + end.x) / 2; const selected = selection?.id === edge.selection.id; const inPath = highlightedPathEdgeIds.has(edge.id); const active = selected || hovered === edge.id; return <g key={edge.id} className={`graph-link ${edge.visualClass} ${selected ? "selected" : ""} ${inPath ? "path-highlight" : ""} ${hovered === edge.id ? "hovered" : ""}`} onMouseEnter={() => setHovered(edge.id)} onMouseLeave={() => setHovered(null)}><title>{`${inPath ? "Validated Evidence Path · " : ""}${edge.source.label} → ${edge.target.label} · ${edge.state} · ${edge.file}`}</title><path className="graph-edge-visible" d={`M ${start.x + 96} ${start.y} C ${middle + offset} ${start.y + offset}, ${middle + offset} ${end.y + offset}, ${end.x - 96} ${end.y}`} markerEnd={`url(#${graphId}-arrow-${edge.visualClass})`}/><path className="graph-edge-hit" d={`M ${start.x + 96} ${start.y} C ${middle + offset} ${start.y + offset}, ${middle + offset} ${end.y + offset}, ${end.x - 96} ${end.y}`} onClick={() => onSelect(edge.selection)} role="button" tabIndex={0} aria-label={`${inPath ? "Validated Evidence Path: " : ""}Inspect relationship: ${edge.source.label} to ${edge.target.label}, ${edge.state}`} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(edge.selection); } }}/>{(edges.length <= 6 || active) && <text x={middle} y={(start.y + end.y) / 2 + offset - 4} textAnchor="middle">{svgText(edge.visualClass === "derived" ? `${edge.label} · inferred` : edge.visualClass === "review" ? `${edge.label} · needs review` : edge.label, 25)}</text>}</g>; })}</g>
      <g className="graph-nodes">{nodes.map((node) => { const point = layout.points.get(node.id); if (!point) return null; const selected = selection?.id === node.id; const inPath = edges.some((edge) => highlightedPathEdgeIds.has(edge.id) && (edge.source.id === node.id || edge.target.id === node.id)); const onClick = () => { const target = nodeSelection(node); if (target) onSelect(target); else { const edge = edges.find((candidate) => candidate.source.id === node.id || candidate.target.id === node.id); if (edge) onSelect(edge.selection); } }; return <g key={node.id} className={`graph-node ${node.type} ${selected ? "selected" : ""} ${inPath ? "path-highlight" : ""} ${hovered && edges.some((edge) => edge.id === hovered && (edge.source.id === node.id || edge.target.id === node.id)) ? "related" : ""}`} transform={`translate(${point.x - 90} ${point.y - 29})`} role="button" aria-pressed={selected} tabIndex={0} aria-label={`Inspect ${node.type}: ${node.label} · ${node.detail}${inPath ? " · on validated Evidence Path" : ""}`} onClick={onClick} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onClick(); } }}><title>{`${node.label} · ${node.detail}${inPath ? " · validated Evidence Path" : ""}`}</title><rect width="180" height="58" rx="10"/><text x="12" y="24">{svgText(node.label.split(".").slice(-2).join("."), 26)}</text><text className="graph-node-detail" x="12" y="43">{svgText(node.detail, 29)}</text></g>; })}</g>
      <g className="graph-unresolved-nodes">{unresolved.map((site, index) => { const { x, y } = unresolvedSitePosition(index, layout.relationshipHeight ?? layout.height); return <g key={site.id} className={`graph-node site unresolved-node ${selection?.id === site.id ? "selected" : ""}`} transform={`translate(${x - 90} ${y - 24})`} role="button" tabIndex={0} aria-label={`Inspect unresolved site: ${site.label}, no target inferred, ${site.file}`} onClick={() => onSelect(site.selection)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(site.selection); } }}><title>{`${site.label} · ${site.detail}`}</title><rect width="180" height="48" rx="9"/><text x="12" y="21">{svgText(site.label, 25)}</text><text className="graph-node-detail" x="12" y="38">No target · {svgText(site.file, 17)}</text></g>; })}</g>
  </>, [edges, unresolved, layout, selection?.id, highlightedPathEdgeIds, hovered, onSelect, graphId]);
  return <div className="relation-canvas">
    <div className="graph-legend" aria-label="Relationship evidence legend"><span className="exact"><i/>Identified connection</span><span className="possible"><i/>Possible connection</span><span className="unresolved"><i/>Target unresolved</span>{counts.review > 0 && <span className="review"><i/>Evidence needs review</span>}<details><summary>How to read this graph</summary><p>Boxes represent code. Arrows represent relationships, not execution order. Dashed amber arrows are possible connections; grey boxes mark references with no identified target. Select an arrow to distinguish direct, inferred, and runtime evidence.</p><p>{edges.length} connections shown. {totalEdges} match your search. {provenanceCount} evidence records in this snapshot; {runtimeCount} runtime observations.</p></details></div>
    <div className="graph-controls"><span>Drag to pan · scroll to zoom · click a box or arrow to inspect</span><div><button type="button" aria-label="Zoom out" onClick={() => zoom(1.2)}>−</button><button type="button" aria-label="Zoom in" onClick={() => zoom(0.82)}>+</button><button type="button" onClick={() => setView({ x: 0, y: 0, width: layout.width, height: layout.height })}>Fit graph</button></div></div>
    {nodes.length === 0 && unresolved.length === 0 ? <div className="canvas-empty"><Waypoints size={22}/><p>{totalEdges === 0 ? "No evidence-backed relationships match this search." : "The current filter has no relationships to display."}</p></div> : <div className="graph-viewport" style={{height: graphViewportHeight(layout.height)}}><svg ref={svgRef} className="graph-svg" style={{height: graphViewportHeight(layout.height)}} role="group" aria-label="Interactive snapshot relationship graph" viewBox={`${view.x} ${view.y} ${view.width} ${view.height}`} onPointerDown={panStart} onPointerMove={panMove} onPointerUp={() => setDrag(null)} onPointerCancel={() => setDrag(null)}>
      {graphContent}
    </svg></div>}
    {edges.length < totalEdges && edges.length < 72 && <button className="text-button canvas-more" onClick={onShowMore}>Browse more connections</button>}
  </div>;
}

function Inspector({ analysis, selection, selected, provenance, relatedProvenance, binding, derived, linkedSite, source, sourceError, onRetrySource, sourceLoading, replaying, replayInProgress, showPremises, setShowPremises, onReplay, onNeighbors, onCopy, onClose, copied, onSelect }: { analysis: Analysis; selection: Selection | null; selected?: Entity | Site | Provenance; provenance?: Provenance; relatedProvenance: Provenance[]; binding?: Binding; derived?: DerivedEdge; linkedSite?: Site; source: SourcePreview | null; sourceError: string; onRetrySource: () => void; sourceLoading: boolean; replaying: boolean; replayInProgress: boolean; showPremises: boolean; setShowPremises: (value: boolean) => void; onReplay: (id: string) => void; onNeighbors: () => void; onCopy: () => void; onClose: () => void; copied: boolean; onSelect: (item: Selection) => void }) {
  const entity = selection?.type === "entity" ? selected as Entity | undefined : undefined;
  const site = selection?.type === "site" ? selected as Site | undefined : linkedSite;
  const edge = selection?.type === "edge" ? selected as Provenance | undefined : undefined;
  const parentEntity = entity?.parent_id ? analysis.entities.find((item) => item.entity_id === entity.parent_id) : undefined;
  const title = entity ? humanSymbolName(entity.name, entity.kind) : edge ? edgeLabel(edge, binding, derived, analysis) : site?.expression ?? "Choose code or a connection";
  const file = entity?.file_path ?? site?.file_path ?? edge?.file_path;
  const span = entity?.span ?? site?.span ?? edge?.span;
  const target = binding?.target_entity_id ? analysis.entities.find((item) => item.entity_id === binding.target_entity_id) : derived ? analysis.entities.find((item) => item.entity_id === derived.target_id) : undefined;
  const recordId = entity?.entity_id ?? site?.site_id ?? edge?.edge_id;
  const relation = binding ? "CALL_SITE_BINDING" : derived?.relation ?? edge?.rule_id ?? "";
  const replayState = edge?.replay_state ?? (linkedSite ? analysis.provenance.find((item) => item.edge_id === binding?.edge_id)?.replay_state : undefined);
  const evidenceModality = binding?.binding_state ?? (derived ? derived.relation === "MAY_DISPATCH_TO" ? "MAY" : "DERIVED" : edge ? "EXACT" : "UNRESOLVED");

  const dispatch = site ? analysis.dispatch.find((item) => item.site_id === site.site_id) : undefined;
  const external = binding?.external_symbol_id ? analysis.external_symbols.find((item) => item.external_symbol_id === binding.external_symbol_id) : undefined;
  const supportingCalls = useMemo(() => {
    const aliases = exactCallAliases(analysis);
    const canonicalId = aliases.get(selection?.id ?? "") ?? selection?.id;
    return [...aliases].filter(([, id]) => id === canonicalId).map(([id]) => id).concat(aliases.has(selection?.id ?? "") ? [canonicalId!] : []).filter(id => id !== selection?.id);
  }, [analysis, selection?.id]);
  const derivedExplanation = derived ? derived.relation === "MAY_DISPATCH_TO" ? "This is a possible target based on the available type and inheritance information. Analysis cannot confirm which implementation is called." : `This ${humanRelationshipLabel(derived.relation).toLowerCase()} relationship is inferred from other code relationships. Its supporting chain is available below; it is not a direct source-level call.` : "";
  const description = entity ? `A ${entity.kind.toLowerCase()} in ${parentEntity?.name ?? "this repository"}. Select an arrow to see why two code elements connect.` : derived ? derivedExplanation : binding ? `Code analysis identifies the target of this reference${target ? ` as ${humanSymbolName(target.name, target.kind)}` : external ? " in external code" : ""}. This does not establish that it ran.` : edge ? "This record describes source structure found during analysis. It does not establish a call or runtime execution." : "A reference was found in the source, but analysis could not identify its target. This does not mean the code is broken.";
  return <aside className="inspector-panel" aria-label="Graph inspector" onKeyDown={(event) => { if (event.key === "Escape") onClose(); }}>
    <div className="inspector-head"><div><span className="section-label">{edge ? "CONNECTION DETAILS" : "CODE DETAILS"}</span><h2>{title}</h2></div><button className="icon-button" title="Close inspector" aria-label="Close inspector" onClick={onClose}><X size={16}/></button></div>
    <div className="inspector-scroll">
      <p className="inspector-explanation">{description}</p>
      <div className="identity-block">
        <span className={`type-tag ${derived?.relation === "MAY_DISPATCH_TO" ? "possible" : "entity"}`}>{entity ? entity.kind : evidenceStateLabel({ modality: evidenceModality, replayState: edge?.replay_state, reviewState: edge?.review_state })}</span>
        {edge && <InfoRow label="Connection" value={binding && site ? humanBindingLabel(site.kind) : derived ? humanRelationshipLabel(derived.relation) : "Code structure"}/>}<InfoRow label="Source file" value={file ?? "No repository source"}/>
        {span && <InfoRow label="Lines" value={span.start_line === span.end_line ? String(span.start_line) : `${span.start_line}–${span.end_line}`}/>}
        {edge && <InfoRow label="Execution" value="Not established by static analysis"/>}
        {external && <p className="inspector-explanation">The target belongs to {external.distribution_name ?? external.module_name ?? "external code"}. The code below shows the reference in your repository; the external implementation is not part of this snapshot.</p>}
      </div>
      <section className="source-section">
        <div className="source-title"><span><FileCode2 size={15}/> Source evidence</span></div>
        {source ? <><div className="source-location" title={analysis.repository.head_commit ?? analysis.snapshot_id}>{source.file_path} · version {analysis.repository.head_commit?.slice(0, 7) ?? "working tree"}</div><SourceCode source={source}/><HighlightedSourceViewer source={source} version={analysis.repository.head_commit?.slice(0, 7) ?? "working tree"}/></> : <div className="source-missing" role="status">{sourceLoading ? "Loading pinned source…" : sourceError ? /not found|not present/i.test(sourceError) ? "This file is not available in the selected snapshot." : `Source retrieval failed: ${sourceError}` : derived ? "This relationship is inferred. Inspect its supporting connections below for source evidence." : external ? "The external implementation is not included in this snapshot." : "No direct source span is attached to this selection."}{sourceError && !sourceLoading && <button className="text-button" onClick={onRetrySource}>Retry source</button>}</div>}
      </section>
      <section className="inspector-section"><h3>Keep exploring</h3><button className="action-secondary full" onClick={onNeighbors}>Explore connections<Waypoints size={14}/></button><p className="inspector-help">Open a focused view of the code directly connected to this selection.</p></section>
      {edge && <ReplayControls key={edge.edge_id} edge={edge} snapshotId={analysis.snapshot_id} sourceReady={!!source} replaying={replaying} replayInProgress={replayInProgress} onReplay={onReplay}/>}

      {supportingCalls.length > 0 && <details className="inspector-section"><summary>Evidence for the same call · {supportingCalls.length + 1} records</summary><p>The exact binding and its derived dispatch record describe the same call site and target. The graph shows one Calls arrow.</p><div className="premise-list">{supportingCalls.map(id => <button key={id} onClick={() => onSelect({ type: "edge", id })}><Link2 size={13}/>Inspect supporting evidence<ChevronRight size={13}/></button>)}</div></details>}
      {derived && derived.premise_edge_ids.length > 0 && <section className="inspector-section"><h3>Why this connection was inferred</h3><div className="premise-list">{derived.premise_edge_ids.map((id, i) => <button key={id} onClick={() => onSelect({ type: "edge", id })}><Link2 size={13}/>Supporting connection {i + 1}<ChevronRight size={13}/></button>)}</div></section>}
      {!edge && relatedProvenance.length > 0 && <details className="inspector-section"><summary>Related source evidence · {relatedProvenance.length}</summary><div className="premise-list">{relatedProvenance.map((record) => <button key={record.edge_id} onClick={() => onSelect({ type: "edge", id: record.edge_id })}><Link2 size={13}/><span>{record.file_path}:{record.span.start_line}</span><ChevronRight size={13}/></button>)}</div></details>}
      {site && binding && !edge && <button className="subtle-action" onClick={() => onSelect({ type: "edge", id: binding.edge_id })}>Inspect this connection<ChevronRight size={13}/></button>}
      <details className="technical-details inspector-advanced"><summary>Advanced analysis details</summary><button className="text-button" onClick={onCopy}><Copy size={13}/>{copied ? "Copied" : "Copy record ID"}</button><code>{recordId}</code><InfoRow label="Snapshot" value={analysis.snapshot_id}/><InfoRow label="Commit" value={analysis.repository.head_commit ?? "Working tree"}/>{source && <InfoRow label="Source hash" value={source.file_content_hash}/ >}{entity && <InfoRow label="Full name" value={entity.qualified_name}/ >}{site && <><InfoRow label="Site kind" value={site.kind}/><InfoRow label="Resolver" value={binding?.resolver ?? "No resolved binding"}/><InfoRow label="SCIP symbol" value={binding?.scip_symbol ?? "Unavailable"}/></>}{dispatch && <InfoRow label="Dispatch" value={humanDispatchState(dispatch.state, dispatch.candidate_entity_ids.length)}/ >}{edge && <><InfoRow label="Provenance" value={edge.provenance_id}/><InfoRow label="Replay scope" value={edge.replay_scope}/><InfoRow label="Extractor" value={`${edge.extractor_name} ${edge.extractor_version}`}/><InfoRow label="Rule" value={edge.rule_id}/><InfoRow label="Review state" value={edge.review_state}/><InfoRow label="Resolution" value={binding?.binding_state ?? derived?.relation ?? "Structural evidence"}/></>}</details>
    </div>
  </aside>;
}

function ReplayControls({ edge, snapshotId, sourceReady, replaying, replayInProgress, onReplay }: { edge: Provenance; snapshotId: string; sourceReady: boolean; replaying: boolean; replayInProgress: boolean; onReplay: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [availability, setAvailability] = useState<{ available: boolean; reason: string } | null>(null);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setAvailability(null);
    void api<{ available: boolean; reason: string }>(`/api/evidence/${encodeURIComponent(edge.edge_id)}/replay/availability?snapshot_id=${encodeURIComponent(snapshotId)}`, { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) setAvailability(value); }).catch((reason) => { if (!controller.signal.aborted) setAvailability({ available: false, reason: reason instanceof Error ? reason.message : "Could not check availability. Close and reopen to retry." }); });
    return () => controller.abort();
  }, [edge.edge_id, snapshotId, open]);
  return <details className="inspector-section verification-section" onToggle={(event) => setOpen(event.currentTarget.open)}><summary>Recheck code evidence</summary><p className="inspector-help">Re-run code analysis on the saved source and check whether it reproduces this connection. This does not execute your application or tests.</p><span className="verification-state">{replayStateLabel(edge.replay_state)}</span><button className="action-secondary full" disabled={!sourceReady || !availability?.available || replayInProgress} aria-busy={replaying} onClick={() => onReplay(edge.edge_id)}>{replaying ? "Checking code evidence…" : "Recheck code evidence"}<ShieldCheck size={14}/></button><p className="inspector-help" role="status">{availability?.reason ?? "Checking server prerequisites…"}</p></details>;
}

function ReviewQueue({ items, filter, setFilter, onSelect }: { items: ReviewItem[]; filter: string; setFilter: (value: string) => void; onSelect: (item: ReviewItem) => void }) {
  const [limit, setLimit] = useState(100);
  const categories = ["ALL", ...new Set(items.map((item) => item.state))];
  const visible = filter === "ALL" ? items : items.filter((item) => item.state === filter);
  return <section className="review-queue"><div className="review-queue-head"><div><h2>Evidence requiring review</h2><p>Resolver disagreements, replay failures, unresolved sites, and diagnostics stay visible until examined.</p></div><span className="queue-total"><AlertTriangle size={14}/>{items.length}</span></div><div className="queue-filters">{categories.map((item) => <button key={item} className={filter === item ? "active" : ""} onClick={() => setFilter(item)}>{item.replaceAll("_", " ")}{item === "ALL" && <span>{items.length}</span>}</button>)}</div>{visible.length ? <div className="queue-list">{visible.slice(0, limit).map((item) => <button className="queue-item" key={item.key} onClick={() => onSelect(item)}><span className="queue-item-icon"><AlertTriangle size={15}/></span><span className="queue-item-main"><b>{item.title}</b><small>{item.detail}</small><code>{item.file ? `${item.file}${item.line ? `:${item.line}` : ""}` : item.state}</code></span><span className="queue-state">{item.state.replaceAll("_", " ")}</span><ChevronRight size={15}/></button>)}</div> : <div className="empty-queue"><div><Check size={18}/></div><b>No items in this review filter.</b><p>Current resolver, replay, and diagnostic signals are accounted for.</p></div>}{visible.length > limit && <button className="text-button" onClick={() => setLimit((current) => current + 100)}>Show next review items ({visible.length - limit} remaining)</button>}</section>;
}

function Diagnostics({ diagnostics }: { diagnostics: Diagnostic[] }) { return <section className="diagnostics"><div className="panel-title"><div><h2>Analysis diagnostics</h2><p>Parser and resolver information for this snapshot.</p></div><span className="subtle">{diagnostics.length}</span></div>{diagnostics.slice(0, 20).map((item, index) => <div className="diagnostic-row" key={`${item.code}-${index}`}><AlertTriangle size={13}/><span><b>{item.code}</b><small>{item.message}{item.file_path ? ` · ${item.file_path}` : ""}</small></span></div>)}</section>; }
function InfoRow({ label, value }: { label: string; value: string }) { return <div className="info-row"><span>{label}</span><b title={value}>{value}</b></div>; }
function edgeStateLabel(record: Provenance, binding?: Binding, derived?: DerivedEdge) {
  return evidenceStateLabel({ modality: binding?.binding_state ?? (derived ? derived.relation === "MAY_DISPATCH_TO" ? "MAY" : "DERIVED" : "EXACT"), replayState: record.replay_state, reviewState: record.review_state });
}
function edgeVisualClass(_state: string, record?: Provenance, binding?: Binding, derived?: DerivedEdge) {
  if (derived?.relation === "MAY_DISPATCH_TO") return "possible";
  if (record?.replay_state === "EDGE_NOT_REPRODUCED" || record?.replay_state === "SOURCE_HASH_MISMATCH" || record?.replay_state === "ERROR" || record?.replay_state === "ENVIRONMENT_MISMATCH" || record?.replay_state === "EXTRACTOR_VERSION_MISMATCH" || record?.replay_state === "PREMISE_MISSING") return "review";
  if (binding?.binding_state === "EXACT" && record?.replay_state === "PASS" && record.review_state === "NONE") return "verified";
  if (derived) return "derived";
  return binding?.binding_state === "EXACT" ? "exact" : "unresolved";
}
function edgeLabel(record: Provenance, binding: Binding | undefined, derived: DerivedEdge | undefined, analysis: Analysis) {
  if (!binding && !derived) {
    const names: Record<string, string> = { "snapshot.contains-file.v1": "Source file", "file.declares-module.v1": "Module definition", "syntax.site-context.v1": "Source reference", "syntax.defines.v1": "Code definition" };
    return `${names[record.rule_id] ?? "Code structure"} · ${record.file_path}:${record.span.start_line}`;
  }
  const site = analysis.sites.find((item) => item.site_id === (binding?.site_id ?? derived?.source_id));
  const source = analysis.entities.find((item) => item.entity_id === (site?.context_id ?? derived?.source_id));
  const target = analysis.entities.find((item) => item.entity_id === (binding?.target_entity_id ?? derived?.target_id));
  const external = analysis.external_symbols.find((item) => item.external_symbol_id === binding?.external_symbol_id);
  const from = source ? humanSymbolName(source.name, source.kind) : site?.expression ?? "Code";
  const to = target ? humanSymbolName(target.name, target.kind) : external ? humanExternalName(external.semantic_symbol, external.distribution_name, external.module_name) : "Target unavailable";
  return `${from} → ${to}`;
}

function buildReviewQueue(analysis: Analysis | null): ReviewItem[] {
  if (!analysis) return [];
  const items: ReviewItem[] = [];
  const spanKey = (item: { file_path?: string; span?: Span }) => `${item.file_path}:${item.span?.start_byte}:${item.span?.end_byte}`;
  const provenanceBySpan = new Map(analysis.provenance.slice().reverse().map((item) => [spanKey(item), item]));
  const sitesBySpan = new Map(analysis.sites.slice().reverse().map((item) => [spanKey(item), item]));
  for (const record of analysis.provenance) {
    if (record.review_state !== "NONE" || (record.replay_state !== "NOT_RUN" && record.replay_state !== "PASS")) items.push({ key: `edge:${record.edge_id}`, title: record.review_state !== "NONE" ? record.review_state.replaceAll("_", " ") : `Replay ${record.replay_state.toLowerCase().replaceAll("_", " ")}`, detail: `${record.extractor_name} · ${record.rule_id}`, file: record.file_path, line: record.span.start_line, selection: { type: "edge", id: record.edge_id }, state: record.review_state !== "NONE" ? record.review_state : record.replay_state });
  }
  const boundSites = new Set(analysis.bindings.map((item) => item.site_id));
  for (const site of analysis.sites) {
    if (!boundSites.has(site.site_id) && ["CALL_CALLEE", "VALUE_REFERENCE", "IMPORT_REFERENCE", "CALLBACK_REFERENCE"].includes(site.kind)) items.push({ key: `site:${site.site_id}`, title: "Unresolved site", detail: `${site.kind} · ${site.expression}`, file: site.file_path, line: site.span.start_line, selection: { type: "site", id: site.site_id }, state: "UNRESOLVED" });
  }
  for (const [index, diagnostic] of analysis.diagnostics.entries()) {
    const matchingEvidence = diagnostic.file_path && diagnostic.span
      ? provenanceBySpan.get(spanKey(diagnostic))
      : undefined;
    const matchingSite = diagnostic.file_path && diagnostic.span
      ? sitesBySpan.get(spanKey(diagnostic))
      : undefined;
    items.push({ key: `diagnostic:${index}`, title: diagnostic.code.replaceAll("_", " "), detail: diagnostic.message, file: diagnostic.file_path, line: diagnostic.span?.start_line, state: "DIAGNOSTIC", selection: matchingEvidence ? { type: "edge", id: matchingEvidence.edge_id } : matchingSite ? { type: "site", id: matchingSite.site_id } : undefined });
  }
  const rank: Record<string, number> = { RESOLVER_DISAGREEMENT: 0, REPLAY_FAILED: 1, RUNTIME_MISMATCH: 2, SCHEMA_WARNING: 3, HUMAN_REVIEW_REQUIRED: 4, UNRESOLVED: 5, DIAGNOSTIC: 6 };
  return items.sort((a, b) => (rank[a.state] ?? 7) - (rank[b.state] ?? 7) || a.file?.localeCompare(b.file ?? "") || a.key.localeCompare(b.key));
}
