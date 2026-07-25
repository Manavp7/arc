/**
 * The live map: MapLibre for the basemap and camera, deck.gl for the data layers.
 *
 * MapboxOverlay (interleaved: false) puts deck.gl in its own canvas above MapLibre's. Interleaved
 * mode shares one WebGL context and is prettier for 3D extrusions, but it makes every deck.gl
 * update a MapLibre repaint — at 20 entity updates a second on a laptop CPU that is the
 * difference between smooth and juddering.
 */

import { MapboxOverlay } from "@deck.gl/mapbox";
import { GeoJsonLayer, IconLayer, PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import type { Feature, FeatureCollection, Geometry } from "geojson";
import maplibregl from "maplibre-gl";
import { useEffect, useMemo, useRef, useState } from "react";
import { basemapStyle, entityColour, SEVERITY_COLOURS } from "../lib/basemap";
import { positionedEntities, useSioStore } from "../store";
import type { Entity, SioEvent, Zone } from "../types";

import "maplibre-gl/dist/maplibre-gl.css";

const DEFAULT_VIEW = { longitude: -122.4194, latitude: 37.7749, zoom: 16.5, pitch: 0, bearing: 0 };

/** Bounding box of the site geometry, as [[west, south], [east, north]]. */
function zoneBounds(zones: Zone[]): [[number, number], [number, number]] | null {
  let west = Infinity;
  let south = Infinity;
  let east = -Infinity;
  let north = -Infinity;
  for (const zone of zones) {
    for (const ring of zone.geometry?.coordinates ?? []) {
      for (const position of ring as number[][]) {
        const lon = position[0];
        const lat = position[1];
        if (lon === undefined || lat === undefined) continue;
        west = Math.min(west, lon);
        east = Math.max(east, lon);
        south = Math.min(south, lat);
        north = Math.max(north, lat);
      }
    }
  }
  return Number.isFinite(west) ? [[west, south], [east, north]] : null;
}

/** Printable ASCII: entity labels are names, plates and ids. */
const LABEL_CHARSET = Array.from({ length: 95 }, (_, index) => String.fromCharCode(32 + index));

type ZoneFeature = Feature<Geometry, Zone>;

function zoneLayer(zones: Zone[]) {
  const collection: FeatureCollection<Geometry, Zone> = {
    type: "FeatureCollection",
    features: zones.map((zone) => ({
      type: "Feature",
      geometry: zone.geometry,
      properties: zone,
    })),
  };
  return new GeoJsonLayer<Zone>({
    id: "zones",
    data: collection,
    stroked: true,
    filled: true,
    // Restricted areas read red so an intrusion is obvious without reading a label.
    getFillColor: (feature: ZoneFeature) =>
      feature.properties.restricted ? [200, 70, 60, 26] : [60, 90, 120, 22],
    getLineColor: (feature: ZoneFeature) =>
      feature.properties.restricted ? [220, 90, 80, 180] : [90, 130, 170, 150],
    lineWidthMinPixels: 1.5,
    pickable: true,
  });
}

const LABELLED_TYPES = new Set(["truck", "vehicle", "drone", "forklift"]);

const LABEL_SIZE = 11;
const LABEL_OFFSET_Y = -14;
const LABEL_FONT = "system-ui, sans-serif";
/** deck.gl rasterises its font atlas at 64 px and scales down, so measure there and scale too. */
const ATLAS_FONT_SIZE = 64;
/** Breathing room around each label box, so survivors are visibly separated, not merely disjoint. */
const LABEL_PADDING = 2;

const advanceCache = new Map<string, number>();
let measureContext: CanvasRenderingContext2D | null | undefined;

/** Width of one character at atlas size, cached — the label set is ~30 strings over ASCII. */
function charAdvance(char: string): number {
  const cached = advanceCache.get(char);
  if (cached !== undefined) return cached;
  if (measureContext === undefined) {
    measureContext = document.createElement("canvas").getContext("2d");
    if (measureContext) measureContext.font = `${ATLAS_FONT_SIZE}px ${LABEL_FONT}`;
  }
  // Half an em is a serviceable mean advance where 2D canvas is unavailable (jsdom).
  const advance = measureContext
    ? measureContext.measureText(char).width
    : ATLAS_FONT_SIZE * 0.5;
  advanceCache.set(char, advance);
  return advance;
}

/** Rendered width in pixels. Summed per character, which is how TextLayer lays glyphs out. */
function labelWidth(text: string): number {
  let atlasWidth = 0;
  for (const char of text) atlasWidth += charAdvance(char);
  return (atlasWidth * LABEL_SIZE) / ATLAS_FONT_SIZE;
}

/** Which label an operator would rather keep: the selection, then trucks, then the rest. */
function labelPriority(entity: Entity, selectedId: string | null): number {
  if (entity.entity_id === selectedId) return 2;
  return entity.type === "truck" || entity.type === "vehicle" ? 1 : 0;
}

interface LabelBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/**
 * Choose which labels to draw, dropping any that would collide with a higher-priority one.
 *
 * Restricting *which* entities get labels does not stop two of them standing in the same place:
 * two forklifts parked 0.75 m apart put two labels inside the same 51 px of screen and rendered
 * them glyph-on-glyph — visibly bold and unreadable.
 *
 * deck.gl ships a CollisionFilterExtension for exactly this, and it is the obvious thing to reach
 * for, but on 9.3.7 attaching it makes the layer draw *nothing at all*: the collision map itself
 * rasterises correctly, yet every instance is discarded, and deck throws
 * `accessor "getCollisionPriority" is not a function` from its own attribute updater once the
 * extension's `draw()` has swapped `layer.props` for a clone. It reproduces on a bare
 * ScatterplotLayer too, so it is not a TextLayer quirk. Every entity label vanished. Doing the
 * test here costs one pass over ~30 boxes and cannot silently erase the layer.
 */
function deconflictLabels(
  entities: Entity[],
  selectedId: string | null,
  project: (position: [number, number]) => { x: number; y: number },
): Entity[] {
  const ranked = entities
    .filter(
      (entity) =>
        !entity.is_static &&
        entity.label &&
        (LABELLED_TYPES.has(entity.type) || entity.entity_id === selectedId),
    )
    // Ties break on id rather than on array order: co-located entities arrive in a different
    // order on each stream message, and without a stable key the surviving label of a tied pair
    // would flip between them twice a second.
    .sort(
      (a, b) =>
        labelPriority(b, selectedId) - labelPriority(a, selectedId) ||
        (a.entity_id < b.entity_id ? -1 : 1),
    );

  const kept: Entity[] = [];
  const taken: LabelBox[] = [];
  for (const entity of ranked) {
    const { x, y } = project([entity.state.geo!.lon, entity.state.geo!.lat]);
    const halfWidth = labelWidth(entity.label!) / 2 + LABEL_PADDING;
    const halfHeight = LABEL_SIZE / 2 + LABEL_PADDING;
    const box: LabelBox = {
      x0: x - halfWidth,
      x1: x + halfWidth,
      y0: y + LABEL_OFFSET_Y - halfHeight,
      y1: y + LABEL_OFFSET_Y + halfHeight,
    };
    const collides = taken.some(
      (other) => other.x0 < box.x1 && box.x0 < other.x1 && other.y0 < box.y1 && box.y0 < other.y1,
    );
    if (collides) continue;
    taken.push(box);
    kept.push(entity);
  }
  return kept;
}

function entityLayers(
  entities: Entity[],
  labelled: Entity[],
  selectedId: string | null,
  onSelect: (id: string) => void,
) {
  // Returns [dots, labels]. The caller draws the labels LAST, above the event rings, because a ring
  // crossing a label is the same readability failure the rings were introduced to fix.
  const movers = entities.filter((entity) => !entity.is_static);
  const fixtures = entities.filter((entity) => entity.is_static);

  return [
    new ScatterplotLayer<Entity>({
      id: "fixtures",
      data: fixtures,
      getPosition: (entity) => [entity.state.geo!.lon, entity.state.geo!.lat],
      getFillColor: (entity) => entityColour(entity.type, 160),
      getRadius: 3,
      radiusMinPixels: 3,
      radiusMaxPixels: 8,
      pickable: true,
      onClick: (info) => info.object && onSelect(info.object.entity_id),
    }),
    new ScatterplotLayer<Entity>({
      id: "entities",
      data: movers,
      getPosition: (entity) => [entity.state.geo!.lon, entity.state.geo!.lat],
      getFillColor: (entity) => entityColour(entity.type),
      getLineColor: (entity) =>
        entity.entity_id === selectedId ? [255, 255, 255, 255] : [10, 15, 20, 180],
      getLineWidth: (entity) => (entity.entity_id === selectedId ? 2.5 : 1),
      lineWidthUnits: "pixels",
      stroked: true,
      getRadius: (entity) => (entity.type === "person" ? 2.2 : 4),
      radiusMinPixels: 4,
      radiusMaxPixels: 14,
      pickable: true,
      onClick: (info) => info.object && onSelect(info.object.entity_id),
      // Transitions interpolate between position updates, so 2 Hz fusion output looks like
      // continuous motion rather than teleporting.
      transitions: { getPosition: 400 },
      updateTriggers: { getLineColor: [selectedId], getLineWidth: [selectedId] },
    }),
    new TextLayer<Entity>({
      id: "entity-labels",
      // Already de-conflicted by the caller: whatever is in here is drawn.
      data: labelled,
      getPosition: (entity) => [entity.state.geo!.lon, entity.state.geo!.lat],
      getText: (entity) => entity.label ?? "",
      getSize: LABEL_SIZE,
      getColor: [220, 228, 236, 200],
      getPixelOffset: [0, LABEL_OFFSET_Y],
      // No font atlas is bundled, so use the browser's default sans stack.
      fontFamily: LABEL_FONT,
      // An explicit character set instead of "auto": auto builds the atlas lazily and uploads an
      // empty canvas on the first frame, which WebGL reports as
      // "INVALID_VALUE: texSubImage2D: no canvas". Entity labels are ASCII (names, plates, ids).
      characterSet: LABEL_CHARSET,
      pickable: false,
      updateTriggers: { getText: [selectedId] },
    }),
  ];
}

/** Severities that earn a mark on the map. Everything else belongs in the feed and the timeline. */
const MAP_WORTHY: ReadonlySet<string> = new Set(["medium", "high", "critical"]);
/** How long an event keeps its marker. Older than this and it is history, not a live situation. */
const EVENT_MARKER_TTL_MS = 120_000;

function eventLayer(events: SioEvent[]) {
  // Phase 3 turned this layer into a problem worth solving properly.
  //
  // Zone entries and exits fire constantly, they carry the position of the entity that caused them,
  // and they were drawn as filled discs up to 24 px across. The result was a pale blob sitting on top
  // of every truck on the site, hiding the entity AND its label — a map that reported activity by
  // obscuring the thing the activity happened to.
  //
  // Three changes, in order of how much they mattered:
  //   1. Only medium-and-above severity is drawn. An informational zone crossing is timeline material;
  //      putting every one on the map is how a map stops being readable.
  //   2. Rings, not discs, so whatever is underneath stays visible. The event is an annotation on an
  //      entity, and an annotation that covers its subject has failed.
  //   3. Markers fade out over two minutes, because a mark that looks identical at five minutes old
  //      teaches an operator to ignore all of them.
  const now = Date.now();
  const positioned = events
    .filter((event) => event.geo != null && MAP_WORTHY.has(event.severity))
    .filter((event) => now - new Date(event.ts).getTime() < EVENT_MARKER_TTL_MS)
    .slice(0, 40);

  const age = (event: SioEvent) =>
    Math.min(1, Math.max(0, (now - new Date(event.ts).getTime()) / EVENT_MARKER_TTL_MS));

  return new ScatterplotLayer<SioEvent>({
    id: "events",
    data: positioned,
    getPosition: (event) => [event.geo!.lon, event.geo!.lat],
    filled: false,
    stroked: true,
    getLineColor: (event) => {
      const colour = SEVERITY_COLOURS[event.severity] ?? SEVERITY_COLOURS["info"]!;
      return [colour[0]!, colour[1]!, colour[2]!, Math.round(230 * (1 - age(event)))];
    },
    getLineWidth: 2,
    lineWidthMinPixels: 2,
    lineWidthMaxPixels: 3,
    getRadius: 9,
    radiusMinPixels: 9,
    radiusMaxPixels: 18,
    updateTriggers: { getLineColor: [now] },
    pickable: true,
  });
}

export function LiveMap() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);
  const fittedRef = useRef(false);

  // Subscribe to the stored Map, then derive. Subscribing to a *filtered array* would hand React a
  // new snapshot on every read and loop forever (see the note in store.ts).
  const entityMap = useSioStore((state) => state.entities);
  const entities = useMemo(() => positionedEntities(entityMap), [entityMap]);
  const events = useSioStore((state) => state.events);
  const zones = useSioStore((state) => state.zones);
  const selectedId = useSioStore((state) => state.selectedEntityId);
  const selectEntity = useSioStore((state) => state.selectEntity);
  /** Bumped whenever the camera moves, to re-run the screen-space label de-confliction. */
  const [cameraVersion, setCameraVersion] = useState(0);

  // Create the map once. React 19's strict-mode double-invoke makes a guard essential here:
  // two MapLibre instances on one container leak a WebGL context each.
  useEffect(() => {
    if (mapRef.current || !containerRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: basemapStyle(false),
      center: [DEFAULT_VIEW.longitude, DEFAULT_VIEW.latitude],
      zoom: DEFAULT_VIEW.zoom,
      pitch: DEFAULT_VIEW.pitch,
      attributionControl: false,
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: true }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

    const overlay = new MapboxOverlay({ interleaved: false, layers: [] });
    map.addControl(overlay);

    mapRef.current = map;
    overlayRef.current = overlay;

    return () => {
      overlayRef.current = null;
      mapRef.current = null;
      map.remove();
    };
  }, []);

  // Runs after the effect above, so the map exists by the time this subscribes. MapLibre fires
  // `move` on every frame of a pan or zoom, so coalesce to at most one recompute per frame.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    let queued = false;
    const bump = () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        setCameraVersion((version) => version + 1);
      });
    };
    map.on("move", bump);
    return () => {
      map.off("move", bump);
    };
  }, []);

  // Which labels survive depends on where the camera puts them, so this has to be recomputed on
  // camera moves as well as on data changes. `cameraVersion` is the signal, not an input.
  const labelled = useMemo(() => {
    const map = mapRef.current;
    if (!map) return [];
    return deconflictLabels(entities, selectedId, (position) => map.project(position));
  }, [entities, selectedId, cameraVersion]);

  const layers = useMemo(() => {
    const entityStack = entityLayers(entities, labelled, selectedId, selectEntity);
    const labels = entityStack.pop();
    // Zones, then entities, then event rings annotating them, then text on top of everything. Text
    // last is not cosmetic: it is the only layer a human reads rather than glances at.
    return [zoneLayer(zones), ...entityStack, eventLayer(events), labels];
  }, [zones, entities, labelled, events, selectedId, selectEntity]);

  useEffect(() => {
    overlayRef.current?.setProps({ layers });
  }, [layers]);

  // Frame the site the first time its geometry arrives. Guarded so it does not fight the operator
  // for control of the camera on every subsequent zone update.
  useEffect(() => {
    if (fittedRef.current || zones.length === 0 || !mapRef.current) return;
    const bounds = zoneBounds(zones);
    if (!bounds) return;
    mapRef.current.fitBounds(bounds, { padding: 60, duration: 0 });
    fittedRef.current = true;
  }, [zones]);

  return (
    <div className="map-root">
      <div ref={containerRef} className="map-canvas" />
      <div className="map-legend">
        <span className="legend-item">
          <i style={{ background: "rgb(86,156,214)" }} /> truck
        </span>
        <span className="legend-item">
          <i style={{ background: "rgb(255,196,87)" }} /> forklift
        </span>
        <span className="legend-item">
          <i style={{ background: "rgb(122,212,137)" }} /> person
        </span>
        <span className="legend-item">
          <i style={{ background: "rgb(199,146,234)" }} /> drone
        </span>
        <span className="legend-count">{entities.length} entities</span>
      </div>
    </div>
  );
}

export { PathLayer, IconLayer };
