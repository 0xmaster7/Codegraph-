const stages = [
  ["QUEUED", "Waiting for analysis", "Your repository is queued for the analysis worker."],
  ["MATERIALIZING", "Preparing source snapshot", "Capturing an immutable copy of the selected repository."],
  ["PARSING", "Reading Python source", "Extracting definitions and call sites from Python files."],
  ["SEMANTIC_INDEXING", "Resolving code relationships", "Indexing symbols and references with SCIP."],
  ["VALIDATING", "Validating evidence", "Checking graph structure and source provenance."],
  ["PUBLISHING", "Publishing snapshot", "Writing the validated graph so it can be investigated."],
  ["COMPLETED", "Ready to explore", "The snapshot has been published."],
];
export function IngestionProgress({status, cancelling, onCancel}: {status: string; cancelling?: boolean; onCancel: () => void}) {
  const index = stages.findIndex(([key]) => key === status);
  const terminal = ["FAILED", "CANCELLED", "INTERRUPTED"].includes(status);
  const current = stages[index] ?? [status, terminal ? `Analysis ${status.toLowerCase()}` : "Checking repository analysis", terminal ? "This snapshot is unavailable for investigation. Retry when you are ready." : "Retrieving the latest server status."];
  const active = index >= 0 && index < stages.length - 1;
  return <section className="ingestion-progress" aria-label="Repository analysis progress">
    <div role="status" aria-live="polite"><b>{current[1]}</b><p>{cancelling ? "Cancellation requested. Waiting for the worker to stop safely." : current[2]}</p></div>
    {active && <div className="activity-track" aria-hidden="true"><span/></div>}
    <ol>{stages.slice(1).map(([key, label], i) => <li key={key} className={i + 1 < index ? "complete" : i + 1 === index ? "current" : ""} aria-current={i + 1 === index ? "step" : undefined}><span>{i + 1 < index ? "✓" : i + 1}</span>{label}</li>)}</ol>
    <small>Status comes from the analysis worker. You can refresh this page to reconnect.</small>
    {active && <button className="header-secondary-action" disabled={cancelling || status === "PUBLISHING"} onClick={onCancel}>{cancelling ? "Stopping safely…" : status === "PUBLISHING" ? "Publication in progress" : "Cancel analysis"}</button>}
  </section>;
}
