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
import { useEffect, useMemo, useRef } from "react";
import { basemapStyle, entityColour, SEVERITY_COLOURS } from "../lib/basemap";
import { selectPositionedEntities, useSioStore } from "../store";
import type { Entity, SioEvent, Zone } from "../types";

import "maplibre-gl/dist/maplibre-gl.css";

const DEFAULT_VIEW = { longitude: -122.4194, latitude: 37.7749, zoom: 16.5, pitch: 0, bearing: 0 };

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

function entityLayers(entities: Entity[], selectedId: string | null, onSelect: (id: string) => void) {
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
      data: movers.filter((entity) => entity.label),
      getPosition: (entity) => [entity.state.geo!.lon, entity.state.geo!.lat],
      getText: (entity) => entity.label ?? "",
      getSize: 11,
      getColor: [220, 228, 236, 200],
      getPixelOffset: [0, -14],
      // No font atlas is bundled, so use the browser's default sans stack.
      fontFamily: "system-ui, sans-serif",
      characterSet: "auto",
      pickable: false,
    }),
  ];
}

function eventLayer(events: SioEvent[]) {
  const positioned = events.filter((event) => event.geo != null).slice(0, 60);
  return new ScatterplotLayer<SioEvent>({
    id: "events",
    data: positioned,
    getPosition: (event) => [event.geo!.lon, event.geo!.lat],
    getFillColor: (event) => SEVERITY_COLOURS[event.severity] ?? SEVERITY_COLOURS["info"]!,
    getRadius: 8,
    radiusMinPixels: 6,
    radiusMaxPixels: 24,
    stroked: true,
    getLineColor: [255, 255, 255, 120],
    lineWidthMinPixels: 1,
    pickable: true,
  });
}

export function LiveMap() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);

  const entities = useSioStore(selectPositionedEntities);
  const events = useSioStore((state) => state.events);
  const zones = useSioStore((state) => state.zones);
  const selectedId = useSioStore((state) => state.selectedEntityId);
  const selectEntity = useSioStore((state) => state.selectEntity);

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

  const layers = useMemo(
    () => [
      zoneLayer(zones),
      ...entityLayers(entities, selectedId, selectEntity),
      eventLayer(events),
    ],
    [zones, entities, events, selectedId, selectEntity],
  );

  useEffect(() => {
    overlayRef.current?.setProps({ layers });
  }, [layers]);

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
