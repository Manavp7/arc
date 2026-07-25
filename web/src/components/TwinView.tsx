/**
 * The 3D digital twin (PRD M18, Phase 7 P7.2).
 *
 * **Lazy-loaded, and that is not an optimisation — it is the reason this can exist at all.** Cesium is ~3MB of
 * JavaScript plus its own Workers and asset directory. Loading that on every page view to serve a toggle most
 * operators never touch would make the console slower for everyone in exchange for a feature for a few. The
 * whole module is behind a `React.lazy` boundary, so the bytes arrive when somebody asks for 3D and never
 * otherwise. `TwinPanel` is the boundary; this file is what it loads.
 *
 * **What 3D is actually for here.** A 3D view of a logistics yard is mostly a worse 2D map: flat ground, and
 * pan/zoom that now has two more degrees of freedom to get lost in. The thing it genuinely adds is
 * **camera coverage as a volume**. A camera's field of view is a cone in space, and the 2D map can only draw its
 * shadow on the ground — which silently misrepresents what a mast-mounted camera at 8m actually sees, and cannot
 * show that two cameras overlap at head height but not at ground level. So the frustums are the feature and the
 * entities are context, not the other way round.
 *
 * **It degrades honestly without an ion token.** Cesium World Terrain needs a Cesium ion account. Rather than
 * demanding one, this falls back to the WGS84 ellipsoid and *says so on screen* — a 3D view that silently shows
 * flat ground where there are hills is worse than one that admits it, because somebody will make a
 * line-of-sight judgement from it.
 */

import { Cartesian3, Color, Ion, Terrain } from "cesium";
import { useEffect, useMemo, useState } from "react";
import {
  CameraLookAt,
  Entity as CesiumEntity,
  PolygonGraphics,
  Viewer,
} from "resium";

import { api } from "../lib/api";
import { ENTITY_COLOURS } from "../lib/basemap";
import type { Entity, Zone } from "../types";
import { positionedEntities, useSioStore } from "../store";

interface Camera {
  source_id: string;
  label: string | null;
  lat: number;
  lon: number;
  fov: { type: string; coordinates: number[][][] } | null;
}

/**
 * How high above the ground a camera is assumed to sit, in metres.
 *
 * A guess, and labelled as one on screen. The `sources` table records a camera's position and its ground-level
 * field of view but not its mast height, so the frustum's apex is an assumption. Stating the number beats
 * drawing a confident cone from an invisible one — somebody judging whether a camera clears a stack of
 * containers needs to know which part of the picture is data.
 */
const ASSUMED_CAMERA_HEIGHT_M = 8;

/** How tall to draw an entity's marker, so it is visible against the ground. */
const ENTITY_HEIGHT_M = 3;

const SITE_CENTRE = { lon: -122.4175, lat: 37.7756 };

/**
 * The box every entity is drawn as, built ONCE.
 *
 * This was `new Cartesian3(6, 3, ENTITY_HEIGHT_M)` inside the render, and that single line was the difference
 * between a usable view and an unusable one. A new object identity per render means resium's shallow diff never
 * short-circuits, so Cesium tears down and rebuilds the geometry batch for all ~56 entities — and with the live
 * stream delivering ~50 messages a second, it restarted the batch long before it could ever finish one.
 *
 * Measured before: 9,067 property writes per second, 1.05 rendered frames per second, `dataSourceDisplay.ready`
 * stuck false, and the yard not visible for 277 seconds. A GPU would have made each rebuild faster without
 * stopping the treadmill.
 */
const ENTITY_BOX_DIMENSIONS = new Cartesian3(6, 3, ENTITY_HEIGHT_M);

/** The opening view, built once for the same identity reasons as the box dimensions. */
const SITE_TARGET = Cartesian3.fromDegrees(SITE_CENTRE.lon, SITE_CENTRE.lat, 0);
const SITE_OFFSET = new Cartesian3(0, -420, 320);

/** Colours built once per type rather than per entity per frame. */
const COLOUR_CACHE = new Map<string, Color>();

/**
 * How often the twin re-reads the world.
 *
 * The 2D map can absorb the full stream because deck.gl re-uploads a typed array; Cesium maintains a retained
 * scene graph and has to reconcile every entity. 1Hz is plenty for a yard — a truck at 20km/h moves 5.5m
 * between frames — and it is the difference between a scene that settles and one that never does.
 */
const REFRESH_MS = 1000;

/**
 * A snapshot of the entities, sampled rather than subscribed.
 *
 * Deliberately NOT `useSioStore(positionedEntities)`: that re-renders on every store change, which is every
 * stream message. This reads the store on a timer, so the twin's render rate is bounded by the clock instead of
 * by how busy the site is — and a busier site is exactly when the operator needs the view to work.
 */
function useSampledEntities(): Entity[] {
  const [snapshot, setSnapshot] = useState<Entity[]>([]);
  useEffect(() => {
    const read = () => {
      const state = useSioStore.getState();
      setSnapshot(
        positionedEntities(
          state.replayAt ? state.historyEntities : state.entities,
        ),
      );
    };
    read();
    const timer = setInterval(read, REFRESH_MS);
    return () => clearInterval(timer);
  }, []);
  return snapshot;
}

export default function TwinView() {
  const selectedId = useSioStore((state) => state.selectedEntityId);
  const select = useSioStore((state) => state.selectEntity);

  const [zones, setZones] = useState<Zone[]>([]);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [showFrustums, setShowFrustums] = useState(true);
  const [showZones, setShowZones] = useState(true);
  const [terrain, setTerrain] = useState(false);
  const [note, setNote] = useState("");
  // Bumping this remounts `CameraLookAt`, which re-runs its `once` fly-to. Cheaper and more predictable than
  // holding a viewer ref and driving the camera imperatively, which fights resium's own lifecycle.
  const [recentre, setRecentre] = useState(0);

  // The SAME source as the 2D map, including during replay — if the twin read live entities while the map
  // replayed, the console would describe two different moments at once, which is worse than not replaying at
  // all because it looks correct. Sampled at 1Hz rather than subscribed; see `useSampledEntities`.
  const entities = useSampledEntities();

  useEffect(() => {
    void (async () => {
      try {
        setZones((await api.zones()) as Zone[]);
      } catch {
        /* the twin is still useful without zone outlines */
      }
      try {
        setCameras((await api.cameras()) as Camera[]);
      } catch {
        setNote("Camera coverage is unavailable, so no frustums are drawn.");
      }
    })();
  }, []);

  // An ion token unlocks world terrain and the photorealistic tilesets. Read from the environment so a
  // deployment can supply one; absent, the ellipsoid is used and the caption says so.
  const ionToken = import.meta.env.VITE_CESIUM_ION_TOKEN as string | undefined;
  useEffect(() => {
    if (ionToken) Ion.defaultAccessToken = ionToken;
  }, [ionToken]);

  const terrainProvider = useMemo(
    () => (terrain && ionToken ? Terrain.fromWorldTerrain() : undefined),
    [terrain, ionToken],
  );

  return (
    <div className="twin-view">
      <div className="twin-controls">
        <label className="checkbox">
          <input
            type="checkbox"
            checked={showFrustums}
            onChange={(event) => setShowFrustums(event.target.checked)}
          />
          camera coverage
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={showZones}
            onChange={(event) => setShowZones(event.target.checked)}
          />
          zones
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={terrain}
            disabled={!ionToken}
            onChange={(event) => setTerrain(event.target.checked)}
          />
          terrain
        </label>
        <span className="twin-note">
          {!ionToken
            ? "Flat ellipsoid — set VITE_CESIUM_ION_TOKEN for world terrain."
            : terrain
              ? "Cesium World Terrain."
              : "Flat ellipsoid."}{" "}
          {/* Said on screen, not buried in a docstring. A frustum drawn from an assumed height is a drawing,
              not a measurement, and somebody judging whether a camera clears a container stack has to know
              which part of the picture is data. */}
          Frustum apex assumes a {ASSUMED_CAMERA_HEIGHT_M} m mast — the source
          table records position and ground coverage, not height.
        </span>
        {note && <span className="twin-note twin-warn">{note}</span>}
        {/* Without this an operator who drags off target has no way back: `homeButton` is disabled and the
            initial fly-to happens once. A yard is not findable by eye against a whole city of imagery. */}
        <button
          className="ghost"
          onClick={() => setRecentre((count) => count + 1)}
        >
          recentre
        </button>
      </div>

      <Viewer
        full={false}
        className="twin-canvas"
        terrain={terrainProvider}
        // Every widget off. They are built for a globe browser — a search box, a clock, a base-layer picker,
        // a link to Cesium's own help — and none of them belongs in an operations console that already has its
        // own timeline, its own search and its own basemap.
        animation={false}
        timeline={false}
        baseLayerPicker={false}
        fullscreenButton={false}
        geocoder={false}
        homeButton={false}
        infoBox={false}
        navigationHelpButton={false}
        sceneModePicker={false}
        selectionIndicator={false}
      >
        <CameraLookAt
          key={recentre}
          target={SITE_TARGET}
          offset={SITE_OFFSET}
          once
        />

        {showZones &&
          zones.map((zone) => (
            <CesiumEntity key={zone.zone_id} name={zone.name}>
              <PolygonGraphics
                hierarchy={Cartesian3.fromDegreesArray(
                  (zone.geometry.coordinates[0] ?? []).flat() as number[],
                )}
                material={
                  zone.restricted
                    ? Color.fromCssColorString("#f05a4b").withAlpha(0.18)
                    : Color.fromCssColorString("#4ea1ff").withAlpha(0.1)
                }
                outline
                outlineColor={
                  zone.restricted
                    ? Color.fromCssColorString("#f05a4b")
                    : Color.fromCssColorString("#4ea1ff")
                }
                // `height={0}`, and the reason is a warning Cesium logged 28 times that I had not read:
                //
                //   "Entity geometry outlines are unsupported on terrain. Outlines will be disabled."
                //   "...with heightReference must also have a defined height. heightReference will be ignored"
                //
                // So `heightReference` alone achieved nothing AND silently threw away the outlines — leaving
                // restricted zones distinguished only by a 10%-alpha fill over aerial photography, which is far
                // weaker than intended. Setting an explicit height is what the warning asks for.
                height={0}
              />
            </CesiumEntity>
          ))}

        {/* The frustums: the one thing this view shows that the 2D map cannot. Each is the camera's ground
            coverage lifted into a volume with its apex at the assumed mast height, which is what makes
            overlap-at-head-height visible. */}
        {showFrustums &&
          cameras
            .filter((camera) => camera.fov)
            .map((camera) => (
              <CesiumEntity
                key={camera.source_id}
                name={camera.label ?? camera.source_id}
              >
                <PolygonGraphics
                  hierarchy={Cartesian3.fromDegreesArray(
                    (camera.fov?.coordinates[0] ?? []).flat() as number[],
                  )}
                  material={Color.fromCssColorString("#ffc457").withAlpha(0.14)}
                  outline
                  outlineColor={Color.fromCssColorString("#ffc457").withAlpha(
                    0.7,
                  )}
                  // Extruded from the ground to the mast: the volume, not its shadow.
                  extrudedHeight={ASSUMED_CAMERA_HEIGHT_M}
                  perPositionHeight={false}
                />
              </CesiumEntity>
            ))}

        {entities.map((entity) => (
          <CesiumEntity
            key={entity.entity_id}
            name={entity.label ?? entity.entity_id}
            position={Cartesian3.fromDegrees(
              entity.state.geo?.lon ?? 0,
              entity.state.geo?.lat ?? 0,
              ENTITY_HEIGHT_M / 2,
            )}
            // Selection is SHARED with the 2D map through the store, so clicking a truck here highlights it
            // there. Two views of one site that disagree about what is selected are two applications.
            onClick={() => select(entity.entity_id)}
            box={{
              dimensions: ENTITY_BOX_DIMENSIONS,
              material: colourFor(entity),
              // The type colour is KEPT when selected, with a white outline on top — the 2D map's behaviour.
              // The first version returned solid white, so a selected truck stopped being blue and the white
              // outline was invisible against it.
              outline: entity.entity_id === selectedId,
              outlineColor: Color.WHITE,
              outlineWidth: 3,
            }}
          />
        ))}
      </Viewer>
    </div>
  );
}

function colourFor(entity: Entity): Color {
  // Cached by type, so a busy site does not allocate a Color per entity per frame — and, more importantly, so
  // the same object identity comes back each time and resium's diff can short-circuit.
  const cached = COLOUR_CACHE.get(entity.type);
  if (cached) return cached;
  // The same palette as the 2D map. A truck that is orange on the map and blue in the twin is two objects as
  // far as an operator's memory is concerned.
  const rgb = ENTITY_COLOURS[entity.type] ?? [150, 150, 150];
  const colour = Color.fromBytes(rgb[0], rgb[1], rgb[2], 235);
  COLOUR_CACHE.set(entity.type, colour);
  return colour;
}
