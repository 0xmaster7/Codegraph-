import { useEffect, useId, useRef, useState } from "react";
import { ArrowUpRight, X } from "lucide-react";
import { API_BASE_URL } from "../apiConfig";
import { requestJson } from "./request.js";

export type PinnedSource = { context?: { start_line: number; lines: string[] }; file_path: string; start_line: number; end_line: number; text: string };
export function SourceCode({ source }: { source: PinnedSource }) {
  return <pre tabIndex={0} className="source-code" aria-label={`Source from ${source.file_path}`}><code>{(source.context?.lines ?? source.text.split("\n")).map((line,index) => { const number = (source.context?.start_line ?? source.start_line)+index; return <span className={`source-line ${number>=source.start_line&&number<=source.end_line?"evidence-line":""}`} key={index}><span className="source-line-number" aria-hidden="true">{number}</span><span className="source-line-text">{line||" "}</span></span>; })}</code></pre>;
}

export function HighlightedSourceViewer({ source, version }: { source: PinnedSource; version: string }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => { dialogRef.current?.close(); }, [source]);

  function open() {
    const dialog = dialogRef.current;
    if (!dialog) return;
    dialog.showModal();
    dialog.querySelector<HTMLElement>(".source-code")?.focus({ preventScroll: true });
    dialog.querySelector(".evidence-line")?.scrollIntoView({ block: "center", inline: "nearest" });
  }

  return <>
    <button className="action-secondary full" onClick={open}>View highlighted source<ArrowUpRight size={14}/></button>
    <dialog ref={dialogRef} className="source-viewer" aria-labelledby={titleId} onKeyDown={(event) => { if (event.key === "Escape") event.stopPropagation(); }}>
      <div className="source-viewer-heading"><div><h2 id={titleId}>Source evidence</h2><div className="source-location">{source.file_path} · version {version}</div></div><button type="button" className="icon-button" aria-label="Close source viewer" onClick={() => dialogRef.current?.close()}><X size={18}/></button></div>
      <p>Highlighted lines {source.start_line}–{source.end_line} from the saved snapshot. This is a source excerpt, not the full file.</p>
      <SourceCode source={source}/>
    </dialog>
  </>;
}

export function EvidenceSource({ snapshotId, entityId }: { snapshotId: string; entityId: string }) {
  const [source,setSource] = useState<PinnedSource|null>(null);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState("");
  const flight = useRef(false);
  const controller = useRef<AbortController|null>(null);
  useEffect(() => () => controller.current?.abort(),[]);
  async function open() {
    if(flight.current)return;
    flight.current=true; setBusy(true);setError("");controller.current=new AbortController();
    const signal=controller.current.signal;
    try {
      const resolved=await requestJson<{results:Array<{entity_id:string;file_path:string;span:{start_byte:number;end_byte:number}}>}>(`${API_BASE_URL}/api/snapshots/${snapshotId}/symbols/resolve?query=${encodeURIComponent(entityId)}`,{signal});
      const entity=resolved.results.find(item=>item.entity_id===entityId);
      if(!entity)throw new Error("This code element is not present in the evidence snapshot.");
      const params=new URLSearchParams({file_path:entity.file_path,start_byte:String(entity.span.start_byte),end_byte:String(entity.span.end_byte),context_lines:"3"});
      const result=await requestJson<PinnedSource>(`${API_BASE_URL}/api/snapshots/${snapshotId}/source?${params}`,{signal});
      if(!signal.aborted)setSource(result);
    }catch(reason){if(!signal.aborted)setError(reason instanceof Error?reason.message:"Source retrieval failed");}
    finally{flight.current=false;if(!signal.aborted)setBusy(false);}
  }
  return <div className="diff-source-preview">{!source&&<button className="text-button" disabled={busy} onClick={()=>void open()}>{busy?"Loading source…":error?"Retry source":"View source"}</button>}{error&&<p role="alert">{error}</p>}{source&&<><div className="source-location">{source.file_path} · lines {source.start_line}–{source.end_line}</div><SourceCode source={source}/><button className="text-button" onClick={()=>setSource(null)}>Close source</button></>}</div>;
}
