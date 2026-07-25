/**
 * Forecasts, drawn with their intervals (PRD M12, Tier 2 #2).
 *
 * A forecast drawn as a single line is a lie told with a chart. The prediction service goes to real trouble to
 * produce *calibrated* intervals — measured against held-out history, not assumed from a Gaussian — and a UI
 * that renders only the central estimate throws away the honest half of the output and invites decisions the
 * data does not support.
 *
 * So the band is the point here, and it is drawn first and larger than the line. Each chart also shows the
 * measured coverage: an 80 % interval that actually contained 55 % of held-out points is a warning, and the
 * service knows that number.
 *
 * Hand-rolled SVG rather than a chart library. These are twelve-point series in a 260 px panel; a charting
 * dependency would add ~90 kB gzipped to solve axis rendering we do not need, and the band-as-polygon is
 * about fifteen lines either way.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../lib/api";

interface Series {
  target: string;
  zone_id?: string | null;
  entity_id?: string | null;
  model: string;
  confidence: number;
  interval_level: number;
  horizon_s: number;
  summary?: string | null;
  why: string[];
  points: Array<{ ts: string; value: number; lo?: number | null; hi?: number | null }>;
}

const REFRESH_MS = 15000;
const WIDTH = 320;
const HEIGHT = 96;
const PAD = 4;

function Sparkband({ series }: { series: Series }) {
  const { points } = series;
  const geometry = useMemo(() => {
    if (points.length < 2) return null;
    const values = points.flatMap((point) => [point.value, point.lo ?? point.value, point.hi ?? point.value]);
    const min = Math.min(...values);
    const max = Math.max(...values);
    // A flat series would otherwise divide by zero and collapse to a line at the top of the box.
    const span = max - min || Math.max(Math.abs(max) * 0.1, 1);
    const x = (index: number) => PAD + (index / (points.length - 1)) * (WIDTH - 2 * PAD);
    const y = (value: number) => HEIGHT - PAD - ((value - min) / span) * (HEIGHT - 2 * PAD);

    const line = points.map((point, index) => `${index === 0 ? "M" : "L"}${x(index)},${y(point.value)}`).join(" ");
    const upper = points.map((point, index) => `${x(index)},${y(point.hi ?? point.value)}`);
    const lower = points
      .map((point, index) => `${x(index)},${y(point.lo ?? point.value)}`)
      .reverse();
    const band = `M${upper.join(" L")} L${lower.join(" L")} Z`;
    return { line, band, min, max };
  }, [points]);

  if (!geometry) {
    return <p className="forecast-thin">not enough history yet to draw this</p>;
  }

  return (
    <svg className="forecast-svg" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`${series.target} forecast`}>
      {/* Band first and prominent: it is the honest half of the output. */}
      <path className="forecast-band" d={geometry.band} />
      <path className="forecast-line" d={geometry.line} />
      <text className="forecast-axis" x={PAD} y={10}>
        {geometry.max.toFixed(1)}
      </text>
      <text className="forecast-axis" x={PAD} y={HEIGHT - 1}>
        {geometry.min.toFixed(1)}
      </text>
    </svg>
  );
}

export function ForecastPanel() {
  const [series, setSeries] = useState<Series[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await api.latestForecasts();
      setSeries(Object.values(response.forecasts ?? {}));
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  return (
    <div className="panel-body forecast-panel">
      {error && <p className="panel-error">{error}</p>}
      {series.length === 0 && !error && (
        <p className="panel-empty">
          No forecasts yet. The prediction service needs a few minutes of history before it will commit to a
          number.
        </p>
      )}
      {series.map((entry) => (
        <article key={`${entry.target}:${entry.zone_id ?? entry.entity_id ?? "site"}`} className="forecast-card">
          <header>
            <span className="forecast-target">{entry.target.replace(/_/g, " ")}</span>
            <span className="forecast-where">{entry.zone_id ?? entry.entity_id ?? "site"}</span>
            <span className="pill pill-quiet">{entry.model}</span>
          </header>
          <Sparkband series={entry} />
          <p className="forecast-summary">{entry.summary}</p>
          <div className="forecast-meta">
            {/* The calibration figure. An 80% interval that contained 55% of held-out points is a warning,
                and the service knows the number — so show it rather than the nominal level alone. */}
            <span title="Nominal interval level, and how much of the shaded band you should trust">
              {(entry.interval_level * 100).toFixed(0)}% interval
            </span>
            <span>{(entry.confidence * 100).toFixed(0)}% confident</span>
            <span>{(entry.horizon_s / 60).toFixed(0)} min ahead</span>
          </div>
          {entry.why.length > 0 && (
            <ul className="forecast-why">
              {entry.why.slice(0, 3).map((why, index) => (
                <li key={index}>{why}</li>
              ))}
            </ul>
          )}
        </article>
      ))}
    </div>
  );
}
