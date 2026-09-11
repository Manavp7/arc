import type { DetectionFrame, DetectionObject, NormalizedPoint, ReviewZone } from "./review-types";
export function normalizedPoint(x: number, y: number, bounds: { left: number; top: number; width: number; height: number }): NormalizedPoint {
  return [Math.min(1, Math.max(0, (x - bounds.left) / Math.max(1, bounds.width))), Math.min(1, Math.max(0, (y - bounds.top) / Math.max(1, bounds.height)))];
}
function cross(a: NormalizedPoint, b: NormalizedPoint, c: NormalizedPoint): number { return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]); }
function intersects(a: NormalizedPoint, b: NormalizedPoint, c: NormalizedPoint, d: NormalizedPoint): boolean {
  const abC = cross(a, b, c), abD = cross(a, b, d), cdA = cross(c, d, a), cdB = cross(c, d, b);
  const overlaps = (a0: number, a1: number, b0: number, b1: number) => Math.max(Math.min(a0, a1), Math.min(b0, b1)) <= Math.min(Math.max(a0, a1), Math.max(b0, b1)) + 1e-9;
  return abC * abD <= 0 && cdA * cdB <= 0 && overlaps(a[0], b[0], c[0], d[0]) && overlaps(a[1], b[1], c[1], d[1]);
}
export function polygonProblem(points: NormalizedPoint[]): string | null {
  if (points.length < 3) return "Add at least three points.";
  if (points.length > 20) return "A zone can have at most 20 points.";
  if (points.some(point => point.some(value => !Number.isFinite(value) || value < 0 || value > 1))) return "Zone points must stay inside the image.";
  if (new Set(points.map(point => point.join(","))).size !== points.length) return "Remove repeated points before closing this polygon.";
  let twiceArea = 0;
  for (let i = 0; i < points.length; i++) {
    const a = points[i]!, b = points[(i + 1) % points.length]!;
    twiceArea += a[0] * b[1] - b[0] * a[1];
    for (let j = i + 1; j < points.length; j++) {
      if (j === i + 1 || (i === 0 && j === points.length - 1)) continue;
      if (intersects(a, b, points[j]!, points[(j + 1) % points.length]!)) return "Polygon edges cross. Undo a point and redraw the boundary.";
    }
  }
  if (Math.abs(twiceArea) < 0.0002) return "The zone is too small or its points lie on a line.";
  return null;
}
export function polygonPoints(zone: ReviewZone): string { return zone.points.map(([x, y]) => `${x * 1000},${y * 1000}`).join(" "); }
/** Sampled detections are shown only near their timestamp; never carry a detection indefinitely. */
export function detectionAt(frames: DetectionFrame[], atS: number, maxAgeS = 1.5): DetectionFrame | null {
  let selected: DetectionFrame | null = null;
  for (const frame of frames) if (frame.at_s <= atS + 0.05 && (!selected || frame.at_s > selected.at_s)) selected = frame;
  return selected && atS - selected.at_s <= maxAgeS ? selected : null;
}
export function trackTrails(frames: DetectionFrame[], atS: number, windowS = 4): { id: string; points: string }[] {
  const byTrack = new Map<string, NormalizedPoint[]>();
  for (const frame of frames) {
    if (frame.at_s > atS + 0.05 || frame.at_s < atS - windowS) continue;
    for (const object of frame.objects) {
      const points = byTrack.get(object.track_id) ?? [];
      points.push(boxCenter(object)); byTrack.set(object.track_id, points);
    }
  }
  return [...byTrack].filter(([, points]) => points.length > 1).map(([id, points]) => ({ id, points: points.map(([x, y]) => `${x * 1000},${y * 1000}`).join(" ") }));
}
function boxCenter(object: DetectionObject): NormalizedPoint { return [(object.bbox[0] + object.bbox[2]) / 2, (object.bbox[1] + object.bbox[3]) / 2]; }
export function formatVideoTime(seconds: number): string { const safe = Math.max(0, Number.isFinite(seconds) ? seconds : 0); return `${Math.floor(safe / 60)}:${Math.floor(safe % 60).toString().padStart(2, "0")}`; }
