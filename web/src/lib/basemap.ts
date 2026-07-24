/**
 * Basemap style.
 *
 * Deliberately self-contained: a flat dark background plus the site's own geometry, with no
 * external tile provider. Three reasons — no API key to obtain or leak, the demo works with the
 * network unplugged (PRD principle 6, offline tolerance), and for a facility twin the site
 * outline *is* the map; OSM tiles of a warehouse roof add nothing.
 *
 * Set `SIO_BASEMAP_TILES=osm` to overlay real raster tiles when a wider geographic context is
 * wanted (city-scale deployments).
 */

import type { StyleSpecification } from "maplibre-gl";

const BACKGROUND = "#0b0f14";
const GRID = "#141b24";

export function basemapStyle(withTiles = false): StyleSpecification {
  const style: StyleSpecification = {
    version: 8,
    name: "SIO dark",
    // No `glyphs` key at all: MapLibre validates the style and rejects an explicitly-undefined
    // value ("glyphs: string expected, undefined found"). Nothing needs it — labels are drawn by
    // deck.gl's TextLayer, not MapLibre's symbol layer, so there is no font atlas to fetch and no
    // network dependency for the basemap.
    sources: {},
    layers: [
      {
        id: "background",
        type: "background",
        paint: { "background-color": BACKGROUND },
      },
    ],
  };

  if (withTiles) {
    style.sources["osm"] = {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
      maxzoom: 19,
    };
    style.layers.push({
      id: "osm",
      type: "raster",
      source: "osm",
      paint: { "raster-opacity": 0.45, "raster-saturation": -0.7, "raster-brightness-max": 0.8 },
    });
  }

  return style;
}

export const GRID_COLOUR = GRID;

/** Entity colours, chosen so class is readable at a glance on a dark map. */
export const ENTITY_COLOURS: Record<string, [number, number, number]> = {
  truck: [86, 156, 214],
  vehicle: [86, 156, 214],
  forklift: [255, 196, 87],
  person: [122, 212, 137],
  drone: [199, 146, 234],
  camera: [120, 130, 145],
  sensor: [120, 130, 145],
  gate: [160, 160, 170],
  dock: [160, 160, 170],
  zone: [70, 80, 95],
  container: [200, 160, 120],
  machine: [180, 180, 120],
  hazard: [240, 90, 80],
  unknown: [150, 150, 150],
};

export function entityColour(type: string, alpha = 220): [number, number, number, number] {
  const rgb = ENTITY_COLOURS[type] ?? ENTITY_COLOURS["unknown"]!;
  return [rgb[0], rgb[1], rgb[2], alpha];
}

export const SEVERITY_COLOURS: Record<string, [number, number, number, number]> = {
  info: [110, 160, 210, 200],
  low: [110, 190, 160, 200],
  medium: [240, 200, 90, 220],
  high: [245, 150, 70, 235],
  critical: [240, 80, 70, 255],
};
