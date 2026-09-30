import { useEffect, useRef, useState } from "react";
import { explainError } from "../lib/api";
import { reviewApi, type RetainedAnalysis } from "../lib/review-api";

export function RetainedAnalyses({videoId, selectedId, disabled, refreshKey, onSelect}: {videoId: string; selectedId?: string; disabled: boolean; refreshKey?: string; onSelect: (id: string) => void}) {
  const [rows,setRows] = useState<RetainedAnalysis[]>([]), [cursor,setCursor] = useState<string | null>(null);
  const [reload,setReload] = useState(0);
  const [loading,setLoading] = useState(false), [error,setError] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();request.current=controller;setRows([]);setCursor(null);setLoading(true);setError("");
    void reviewApi.analyses(videoId,undefined,controller.signal).then(page=>{if(!controller.signal.aborted){setRows(page.analyses);setCursor(page.next_cursor);}}).catch(cause=>{if(!controller.signal.aborted)setError(explainError(cause));}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[videoId,refreshKey,reload]);
  async function older() {
    const signal=request.current?.signal;if(!cursor||!signal||signal.aborted||loading)return;setLoading(true);setError("");
    try{const page=await reviewApi.analyses(videoId,cursor,signal);if(!signal.aborted){setRows(current=>[...current,...page.analyses.filter(row=>!current.some(item=>item.analysis_id===row.analysis_id))]);setCursor(page.next_cursor);}}
    catch(cause){if(!signal.aborted)setError(explainError(cause));}finally{if(!signal.aborted)setLoading(false);}
  }
  return <section className="rv-editor-card" aria-label="Retained analysis history"><div className="rv-row"><label>Retained analysis<select aria-label="Retained analysis" value={selectedId ?? ""} disabled={disabled || loading} onChange={event=>{if(event.target.value)onSelect(event.target.value);}}><option value="">Choose a saved run</option>{selectedId&&!rows.some(row=>row.analysis_id===selectedId)&&<option value={selectedId}>{selectedId} · current selection</option>}{rows.map(row=><option key={row.analysis_id} value={row.analysis_id} disabled={row.status!=="completed"}>{row.created_at ? new Date(row.created_at).toLocaleString() : row.analysis_id} · {row.model?.name ?? row.model?.mode ?? "Analysis"} · {row.status}</option>)}</select></label><button className="rv-button" disabled={disabled || loading} onClick={()=>setReload(value=>value+1)}>Refresh analyses</button>{cursor&&<button className="rv-button" disabled={disabled || loading} onClick={()=>void older()}>Load older analyses</button>}</div>{loading&&<p className="rv-small" role="status">Loading retained analyses…</p>}{error&&<p className="rv-error" role="alert">{error}</p>}<p className="rv-small">Open a completed retained run to review its exact zones, rules and findings. {rows.length} saved run{rows.length===1?"":"s"} loaded.</p></section>;
}
