import { useEffect, useState } from "react";
import { api, explainError } from "../lib/api";
import { protectedBlob } from "../lib/review-api";
import type { NormalizedPoint, ReviewZone } from "../lib/review-types";
import * as session from "../lib/session";
import "./site.css";

type Pose = { lat: number; lon: number; bearing_deg: number; height_m: number; tilt_deg: number; fov_deg: number; vfov_deg: number; frame_width: number; frame_height: number };
type Camera = { camera_id: string; name: string; source_id: string; x: number; y: number; pose: Pose | null };
type Site = { site_id: string; revision: number; name: string; notes: string; zones: ReviewZone[]; cameras: Camera[]; floorplan_url?: string; calibration_note?: string };
const poseFields: [keyof Pose, string][] = [["lat", "Latitude"], ["lon", "Longitude"], ["bearing_deg", "Bearing °"], ["height_m", "Height m"], ["tilt_deg", "Downward tilt °"], ["fov_deg", "Horizontal FOV °"], ["vfov_deg", "Vertical FOV °"], ["frame_width", "Frame width px"], ["frame_height", "Frame height px"]];

export function SitePanel({initialSiteId}: {initialSiteId?: string}) {
  const [sites, setSites] = useState<Site[]>([]);
  const [site, setSite] = useState<Site | null>(null);
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [mode, setMode] = useState<"inspect" | "zone" | "camera">("inspect");
  const [points, setPoints] = useState<NormalizedPoint[]>([]);
  const [label, setLabel] = useState("");
  const [selectedCamera, setSelectedCamera] = useState<string | null>(null);
  const [pose, setPose] = useState<Record<string, string>>({});
  const [plan, setPlan] = useState<string | null>(null);
  const [ratio, setRatio] = useState(0.65);
  const canEdit = session.can("site.write");
  useEffect(() => {
    const controller = new AbortController();
    void api.request<{sites: Site[]}>("/sites", { signal: controller.signal }).then(result => { if (controller.signal.aborted) return; setSites(result.sites); setSite(result.sites.find(row => row.site_id === initialSiteId) ?? result.sites[0] ?? null); }).catch(cause => { if (!controller.signal.aborted) setError(explainError(cause)); });
    return () => controller.abort();
  }, [initialSiteId]);
  useEffect(() => {
    const controller = new AbortController(); let url: string | null = null; setPlan(null);
    if (site?.floorplan_url) void protectedBlob(site.floorplan_url, controller.signal, 40 * 1024 * 1024).then(blob => { if (controller.signal.aborted) return; url = URL.createObjectURL(blob); setPlan(url); const image = new Image(); image.onload = () => { if (!controller.signal.aborted) setRatio(image.height / image.width); }; image.src = url; }).catch(cause => { if (!controller.signal.aborted) setError(explainError(cause)); });
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [site?.site_id, site?.floorplan_url]);
  function accept(value: Site) { setSite(value); setSites(rows => [value, ...rows.filter(row => row.site_id !== value.site_id)]); setDirty(false); setPoints([]); setMode("inspect"); }
  function edit(changes: Partial<Site>) { if (!site) return; setSite({...site, ...changes}); setDirty(true); setNotice(""); }
  async function run(action: () => Promise<void>) { setBusy(true); setError(null); setNotice(""); try { await action(); } catch (cause) { setError(explainError(cause)); } finally { setBusy(false); } }
  function choose(value: Site) { setSite(value); setDirty(false); setSelectedCamera(null); setPoints([]); setMode("inspect"); setNotice(""); setError(null); }
  async function save() { if (!site) return; const result = await api.request<Site>(`/sites/${encodeURIComponent(site.site_id)}`, { method: "PUT", body: JSON.stringify({name: site.name, notes: site.notes, zones: site.zones, cameras: site.cameras, expected_revision: site.revision}) }); accept(result); setNotice(`Saved revision ${result.revision}.`); }
  function addZone() { if (!site || points.length < 3 || !label.trim()) return; edit({zones: [...site.zones, { zone_id: `zone_${crypto.randomUUID().replaceAll("-", "")}`, name: label.trim(), points }]}); setPoints([]); setMode("inspect"); setLabel(""); }
  const camera = site?.cameras.find(row => row.camera_id === selectedCamera);
  return <div className="site-editor">
    <header className="section-heading"><div><span className="eyebrow">Administration / site editor</span><h2>Put the site in context.</h2><p className="section-intro">Save a floorplan, draw operating zones, and place cameras. Positions on the plan are relative to the image.</p></div></header>
    {error && <p className="panel-error" role="alert">{error}</p>}{notice && <p role="status" className="site-notice">{notice}</p>}
    <div className="site-select-row"><label>Site<select aria-label="Site" value={site?.site_id ?? ""} disabled={busy || dirty || points.length > 0} onChange={event => { const value = sites.find(row => row.site_id === event.target.value); if (value) choose(value); }}><option value="" disabled>Choose a site</option>{sites.map(row => <option key={row.site_id} value={row.site_id}>{row.name}</option>)}</select></label>
      {canEdit && <form onSubmit={event => { event.preventDefault(); void run(async () => { accept(await api.request<Site>("/sites", {method: "POST", body: JSON.stringify({name: newName.trim()})})); setNewName(""); }); }}><label>New site name<input value={newName} maxLength={120} onChange={event => setNewName(event.target.value)} placeholder="Warehouse or campus" /></label><button disabled={busy || dirty || points.length > 0 || !newName.trim()}>Create site</button></form>}
    </div>
    {!site ? <p className="panel-empty">Create a site to start planning its zones and cameras.</p> : <div className="site-columns"><section className="site-canvas-section">
      <div className="site-toolbar"><strong>{site.name}</strong><span>Revision {site.revision}{dirty ? " · Unsaved changes" : ""}</span>{canEdit && <><label className="site-file">Upload floorplan<input type="file" accept="image/png,image/jpeg" disabled={busy || dirty || points.length > 0} onChange={event => { const file = event.target.files?.[0]; event.target.value = ""; if (!file) return; void run(async () => { if (!file.size || file.size > 8 * 1024 * 1024) throw new Error("Choose a PNG or JPEG smaller than 8 MiB."); accept(await api.request<Site>(`/sites/${site.site_id}/floorplan`, {method: "POST", headers: {"Content-Type": file.type, "X-Revision": String(site.revision)}, body: file})); }); }} /></label><button disabled={busy || !dirty || points.length > 0} onClick={() => void run(save)}>Save layout</button><button disabled={busy || (!dirty && !points.length)} onClick={() => { const original = sites.find(row => row.site_id === site.site_id); if (original) choose(original); }}>Discard edits</button></>}</div>
      <svg className={`site-canvas mode-${mode}`} viewBox={`0 0 1000 ${1000 * ratio}`} role="img" aria-label="Site floorplan editor" onClick={event => { if (!canEdit || busy || mode === "inspect") return; const rect = event.currentTarget.getBoundingClientRect(); const point: NormalizedPoint = [Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))]; if (mode === "zone") { if (points.length < 20) setPoints([...points, point]); } else { const id = `cam_${crypto.randomUUID().replaceAll("-", "")}`; edit({cameras: [...site.cameras, {camera_id: id, name: label.trim() || `Camera ${site.cameras.length + 1}`, source_id: "", x: point[0], y: point[1], pose: null}]}); setSelectedCamera(id); setPose({}); setMode("inspect"); setLabel(""); } }}>
        <defs><pattern id="site-grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" stroke="#2b383c" strokeWidth="1" /></pattern></defs><rect width="1000" height={1000 * ratio} fill="url(#site-grid)" />{plan && <image href={plan} width="1000" height={1000 * ratio} preserveAspectRatio="none" />}
        {site.zones.map(zone => <g key={zone.zone_id}><polygon points={zone.points.map(([x,y]) => `${x * 1000},${y * 1000 * ratio}`).join(" ")} fill="#8be5be33" stroke="#8be5be" strokeWidth="3" /><text x={(zone.points[0]?.[0] ?? 0) * 1000 + 6} y={(zone.points[0]?.[1] ?? 0) * 1000 * ratio + 24}>{zone.name}</text></g>)}
        {points.length > 0 && <polyline points={points.map(([x,y]) => `${x * 1000},${y * 1000 * ratio}`).join(" ")} fill="none" stroke="#ffcb78" strokeWidth="3" />}{points.map(([x,y], index) => <circle key={index} cx={x * 1000} cy={y * 1000 * ratio} r="5" fill="#ffcb78" />)}
        {site.cameras.map(row => <g key={row.camera_id} onClick={event => { if (mode !== "inspect") return; event.stopPropagation(); setSelectedCamera(row.camera_id); setPose(Object.fromEntries(Object.entries(row.pose ?? {}).map(([key,value]) => [key, String(value)]))); }}><circle cx={row.x * 1000} cy={row.y * 1000 * ratio} r="12" fill={row.camera_id === selectedCamera ? "#ffcb78" : "#99caff"} stroke="#132027" strokeWidth="3" /><text x={row.x * 1000 + 16} y={row.y * 1000 * ratio + 6}>{row.name}</text></g>)}
      </svg>
      <p className="feed-note">{mode === "zone" ? "Click at least three corners, then finish the zone. Use Undo to correct a corner." : mode === "camera" ? "Click the plan to place this camera." : "A floorplan is optional. Camera markers and polygons are layout records; they do not geolocate video detections."}</p>
      {canEdit && <div className="site-drawing-controls"><label>Zone / camera label<input value={label} maxLength={100} onChange={event => setLabel(event.target.value)} placeholder="Loading bay" /></label><button disabled={busy || points.length > 0 || site.zones.length >= 30} onClick={() => setMode("zone")}>Draw zone</button><button disabled={busy || points.length > 0 || site.cameras.length >= 50} onClick={() => setMode("camera")}>Place camera</button>{mode !== "inspect" && <><button disabled={points.length === 0} onClick={() => setPoints(points.slice(0,-1))}>Undo corner</button><button disabled={points.length < 3 || !label.trim()} onClick={addZone}>Finish zone</button><button onClick={() => {setPoints([]); setMode("inspect");}}>Cancel drawing</button></>}</div>}
    </section><aside className="site-properties"><label>Site name<input value={site.name} readOnly={!canEdit} maxLength={120} onChange={event => edit({name: event.target.value})} /></label><label>Planning notes<textarea value={site.notes} readOnly={!canEdit} maxLength={4000} onChange={event => edit({notes: event.target.value})} /></label>
      <h3>Zones · {site.zones.length}</h3>{site.zones.map(zone => <div className="site-list-row" key={zone.zone_id}><span>{zone.name}</span>{canEdit && <button onClick={() => edit({zones: site.zones.filter(row => row.zone_id !== zone.zone_id)})}>Remove</button>}</div>)}
      <h3>Cameras · {site.cameras.length}</h3>{site.cameras.map(row => <button className="site-camera-choice" key={row.camera_id} aria-pressed={row.camera_id === selectedCamera} onClick={() => { setSelectedCamera(row.camera_id); setPose(Object.fromEntries(Object.entries(row.pose ?? {}).map(([key,value]) => [key, String(value)]))); }}>{row.name} <small>{row.source_id || "Source not linked"}</small></button>)}
      {camera && <fieldset disabled={!canEdit || busy}><legend>{camera.name}</legend><label>Camera name<input value={camera.name} maxLength={120} onChange={event => edit({cameras: site.cameras.map(row => row.camera_id === camera.camera_id ? {...row, name: event.target.value} : row)})} /></label><label>Source ID<input value={camera.source_id} maxLength={120} onChange={event => edit({cameras: site.cameras.map(row => row.camera_id === camera.camera_id ? {...row, source_id: event.target.value} : row)})} placeholder="Existing source identifier" /></label><details><summary>Calibration draft {camera.pose ? "· recorded" : "· optional"}</summary><p className="feed-note">Enter measured values. Saving this draft does not commission the camera or validate geographic accuracy.</p><div className="site-pose-fields">{poseFields.map(([key,name]) => <label key={key}>{name}<input type="number" step={key.startsWith("frame_") ? "1" : "any"} value={pose[key] ?? ""} onChange={event => setPose({...pose, [key]: event.target.value})} /></label>)}</div><button disabled={poseFields.some(([key]) => !pose[key]?.trim() || !Number.isFinite(Number(pose[key])))} onClick={() => { const value = Object.fromEntries(poseFields.map(([key]) => [key, Number(pose[key])])) as Pose; edit({cameras: site.cameras.map(row => row.camera_id === camera.camera_id ? {...row, pose: value} : row)}); }}>Apply pose to draft</button></details><button onClick={() => { edit({cameras: site.cameras.filter(row => row.camera_id !== camera.camera_id)}); setSelectedCamera(null); }}>Remove camera</button></fieldset>}
    </aside></div>}
  </div>;
}
