"""Phase 7 connectors, against fakes (PRD M1).

Every one of these talks to something this test suite must not depend on — a camera, a broker, a drone, somebody
else's Oracle instance, a public STAC API. So each is driven against a fake, and the fake is chosen to exercise
*this repository's* logic rather than the third party's.

That distinction is the whole design of this file. A test that boots ArduPilot SITL proves ArduPilot works; a test
that drives a fake MAVLink connection proves that `GLOBAL_POSITION_INT` at 1e7-scaled integers becomes the right
latitude — which is the part that can be wrong here, and the part that would put a drone in the Gulf of Guinea.

The recurring risks these cover:

* **unit conversions**, where a factor of 1e7 or a centimetre/metre mix-up is silent and catastrophic;
* **the "not reported" sentinel**, where -1 or 65535 or 0,0 must not become a value;
* **read-only guarantees**, which have to hold at configuration time rather than at execution time;
* **credentials in URLs**, which end up in logs and health payloads unless redacted at the boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sio_ingest.connectors import (
    CsvEnterpriseConnector,
    MavlinkDroneConnector,
    MqttConnector,
    RtspCameraConnector,
    SqlEnterpriseConnector,
    StacSatelliteConnector,
    TrafficConnector,
    build_connector,
    connector_kinds,
)
from sio_ingest.connectors.drone import CM_PER_M, DEGREES_SCALE, INTERESTING, MM_PER_M
from sio_ingest.connectors.enterprise import _refuse_writes
from sio_ingest.connectors.rtsp import _redact
from sio_ingest.connectors.traffic import _first_point, _haversine_km

from sio_core.connector import ConnectorConfig
from sio_schemas import Modality

ALL_P7 = (
    "camera_rtsp",
    "csv_enterprise",
    "drone_mavlink",
    "mqtt_iot",
    "satellite_stac",
    "sql_enterprise",
    "traffic_incidents",
)


def a_config(kind: str, **options: Any) -> ConnectorConfig:
    return ConnectorConfig(
        source_id=f"test-{kind}", kind=kind, modality=Modality.MANUAL, options=options
    )


# --- registration and optional dependencies -------------------------------------------------------
@pytest.mark.parametrize("kind", ALL_P7)
def test_every_connector_is_registered(kind: str) -> None:
    """A connector that is never imported is never registered.

    `build_connector` would then report it as unknown while the file sat there looking complete — which is a
    failure mode with no error message anywhere.
    """
    assert kind in connector_kinds()


@pytest.mark.parametrize("kind", ALL_P7)
def test_every_connector_constructs_without_its_optional_dependency(kind: str) -> None:
    """The dependency lookup must be inside `start()`, not at module import.

    If any of these imported `cv2` or `pymavlink` at module level, the whole ingest service would fail to boot
    on a default install — and `just check` would fail on a laptop that has never seen a camera.
    """
    connector = build_connector(a_config(kind))
    assert connector.source_id == f"test-{kind}"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("camera_rtsp", Modality.VIDEO),
        ("csv_enterprise", Modality.ENTERPRISE),
        ("drone_mavlink", Modality.GPS),
        ("mqtt_iot", Modality.IOT),
        ("satellite_stac", Modality.SATELLITE),
        ("traffic_incidents", Modality.TRAFFIC),
    ],
)
def test_each_connector_declares_a_real_modality(kind: str, expected: Modality) -> None:
    """Written because I got three of these wrong.

    The first version used `Modality.DRONE` and `Modality.CAMERA`, neither of which exists — the enum has GPS
    and VIDEO. It failed at import, loudly, which was lucky: `Modality.MANUAL` on an enterprise connector would
    have been accepted silently and made every downstream modality filter wrong.
    """
    assert build_connector(a_config(kind)).modality is expected


# --- the enterprise connectors --------------------------------------------------------------------
def test_a_select_is_permitted() -> None:
    _refuse_writes("SELECT * FROM bookings")
    _refuse_writes("  with recent as (select 1) select * from recent  ")


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM bookings",
        "UPDATE bookings SET dock = 3",
        "DROP TABLE bookings",
        "INSERT INTO bookings VALUES (1)",
        "TRUNCATE bookings",
        "CREATE TABLE x (a int)",
        "GRANT ALL ON bookings TO public",
        "call some_procedure()",
    ],
)
def test_anything_that_writes_is_refused(statement: str) -> None:
    """A whitelist, not a blacklist.

    A blacklist has to anticipate every way a specific dialect can write, and it only takes one gap. A connector
    pointed at a customer's system of record must not be able to change it, and a deployment that discovers
    otherwise at 2am has already run the statement.
    """
    with pytest.raises(ValueError, match="read-only"):
        _refuse_writes(statement)


def test_a_second_statement_is_refused() -> None:
    """The classic way past a prefix check.

    `SELECT 1; DROP TABLE bookings` starts with SELECT, and a prefix check cannot vouch for what follows a
    semicolon.
    """
    with pytest.raises(ValueError, match="more than one statement"):
        _refuse_writes("SELECT 1; DROP TABLE bookings")


def test_a_trailing_semicolon_is_still_fine() -> None:
    """Refusing this would reject the way most people write SQL."""
    _refuse_writes("SELECT * FROM bookings;")


@pytest.mark.asyncio
async def test_the_sql_connector_refuses_a_write_before_it_connects() -> None:
    """At configuration time, not execution time.

    A read-only guarantee checked after the connection is open is a guarantee checked after the statement could
    have run. This also means the refusal does not need SQLAlchemy installed to happen.
    """
    connector = SqlEnterpriseConnector(
        a_config("sql_enterprise", dsn="sqlite://", query="DELETE FROM x")
    )
    with pytest.raises(ValueError, match="read-only"):
        await connector.start()


@pytest.mark.asyncio
async def test_the_csv_connector_reads_rows_and_maps_columns(tmp_path: Path) -> None:
    """A mapping rather than convention, because nobody's warehouse names a column `lat`."""
    path = tmp_path / "bookings.csv"
    path.write_text(
        "trailer,latitude,longitude,dock\nTRL-9,37.7749,-122.4194,dock_3\nTRL-4,37.775,-122.42,dock_1\n"
    )
    connector = CsvEnterpriseConnector(
        a_config(
            "csv_enterprise",
            path=str(path),
            once=True,
            mapping={"lat": "latitude", "lon": "longitude", "label": "trailer"},
        )
    )
    await connector.start()
    observations = [item async for item in connector.observations()]

    assert len(observations) == 2
    first = observations[0]
    assert first.payload["label"] == "TRL-9"
    assert first.geo is not None
    assert first.geo.lat == pytest.approx(37.7749)
    # Unmapped columns are KEPT. The platform does not know which of a customer's forty columns matters, and
    # discarding them means a later question cannot be answered without re-ingesting history.
    assert first.payload["dock"] == "dock_3"
    assert first.payload["trailer"] == "TRL-9"


@pytest.mark.asyncio
async def test_the_csv_connector_does_not_assert_identity(tmp_path: Path) -> None:
    """A WMS trailer number must not become an `entity_id`.

    And it structurally cannot: `Observation` has no `entity_id` field, which is the schema enforcing the right
    thing rather than trusting every connector author to remember it. Fusion decides identity, and a connector
    able to assert it would let a spreadsheet silently overrule a track the perception stack has been following
    for ten minutes. Asserted here so a future schema change that ADDS the field is caught.
    """
    path = tmp_path / "x.csv"
    path.write_text("entity_id,lat,lon\nTRL-9,1,2\n")
    connector = CsvEnterpriseConnector(a_config("csv_enterprise", path=str(path), once=True))
    await connector.start()
    observation = await anext(connector.observations())
    # It is in the payload, where fusion can use it as a hint...
    assert observation.payload["entity_id"] == "TRL-9"
    # ...and not on the observation, where it would be a claim.
    assert not hasattr(observation, "entity_id")


@pytest.mark.asyncio
async def test_the_csv_connector_refuses_a_missing_path_at_startup(tmp_path: Path) -> None:
    """Refused at startup rather than logged every minute.

    A path that does not exist is a deployment mistake, and finding it in a log an hour later wastes the hour.
    """
    connector = CsvEnterpriseConnector(a_config("csv_enterprise", path=str(tmp_path / "nope.csv")))
    with pytest.raises(FileNotFoundError, match="no such file"):
        await connector.start()


@pytest.mark.asyncio
async def test_the_csv_connector_only_rereads_when_the_file_changes(tmp_path: Path) -> None:
    """A 200MB nightly export costs one `stat` per interval, not a full parse."""
    path = tmp_path / "x.csv"
    path.write_text("a\n1\n")
    connector = CsvEnterpriseConnector(a_config("csv_enterprise", path=str(path)))
    await connector.start()
    assert len(connector._read_if_changed()) == 1
    assert connector._read_if_changed() == [], "an unchanged file must not be re-read"


# --- MQTT -----------------------------------------------------------------------------------------
def test_a_json_payload_becomes_a_payload() -> None:
    connector = MqttConnector(a_config("mqtt_iot"))
    observation = connector._to_observation(
        "site/dock_3/temp", b'{"value": 21.5, "name": "dock 3 probe"}'
    )
    assert observation is not None
    assert observation.payload["value"] == 21.5
    # The topic travels, because it is frequently the only place the sensor's location appears.
    assert observation.payload["topic"] == "site/dock_3/temp"
    assert observation.payload["label"] == "dock 3 probe"


def test_a_bare_value_is_wrapped_rather_than_rejected() -> None:
    """Plenty of sensors publish `21.5` on `site/temp/3`.

    Refusing that would rule out a large share of real MQTT deployments.
    """
    connector = MqttConnector(a_config("mqtt_iot"))
    observation = connector._to_observation("site/temp/3", b"21.5")
    assert observation is not None
    # A NUMBER, not the string "21.5". Leaving it a string makes every downstream comparison a string
    # comparison, where "9" > "10".
    assert observation.payload["value"] == 21.5
    assert isinstance(observation.payload["value"], float)


def test_a_bare_boolean_is_coerced() -> None:
    connector = MqttConnector(a_config("mqtt_iot"))
    observation = connector._to_observation("site/door/1", b"true")
    assert observation is not None
    assert observation.payload["value"] is True


def test_an_undecodable_payload_is_counted_not_crashed() -> None:
    """A sensor publishing protobuf onto a topic we thought was JSON produces silence otherwise.

    "The bridge is up and no data is arriving" is the hardest thing to debug from outside, so it is counted and
    surfaced in the health line.
    """
    connector = MqttConnector(a_config("mqtt_iot"))
    assert connector._to_observation("site/x", b"\xff\xfe\x00binary") is None
    assert connector._undecodable == 1


def test_mqtt_geo_is_only_set_when_both_coordinates_are_present() -> None:
    """Half a coordinate is not a location, and defaulting the other half puts the sensor in the Atlantic."""
    connector = MqttConnector(a_config("mqtt_iot"))
    assert connector._to_observation("t", b'{"lat": 1.0}') is not None
    assert connector._to_observation("t", b'{"lat": 1.0}').geo is None  # type: ignore[union-attr]
    both = connector._to_observation("t", b'{"lat": 1.0, "lon": 2.0}')
    assert both is not None and both.geo is not None


@pytest.mark.asyncio
async def test_mqtt_names_its_extra_when_the_client_is_missing(monkeypatch) -> None:
    """`ModuleNotFoundError: aiomqtt` tells an operator nothing about what to do."""
    import builtins

    real_import = builtins.__import__

    def fail(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "aiomqtt":
            raise ImportError("no aiomqtt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail)
    with pytest.raises(RuntimeError, match=r"sio-ingest\[mqtt\]"):
        await MqttConnector(a_config("mqtt_iot")).start()


# --- MAVLink --------------------------------------------------------------------------------------
class FakeMavlinkMessage:
    """A MAVLink message, as pymavlink presents one.

    A fake rather than SITL. SITL is the right thing for a pre-flight check and the wrong thing for a test suite:
    a 200MB dependency, 30 seconds to boot, and what it exercises is ArduPilot's flight logic rather than this
    repository's unit conversions — which are the part that can put a drone in the Gulf of Guinea.
    """

    def __init__(self, kind: str, **fields: Any) -> None:
        self._kind = kind
        self._fields = fields
        for key, value in fields.items():
            setattr(self, key, value)

    def get_type(self) -> str:
        return self._kind

    def get_srcSystem(self) -> int:
        return int(self._fields.get("src_system", 1))


def a_drone(**options: Any) -> MavlinkDroneConnector:
    connector = MavlinkDroneConnector(a_config("drone_mavlink", min_interval_s=0, **options))
    return connector


def test_a_position_converts_degrees_correctly() -> None:
    """MAVLink sends degrees as int * 1e7. A missing factor of ten is 500km."""
    connector = a_drone()
    observation = connector._handle(
        FakeMavlinkMessage(
            "GLOBAL_POSITION_INT",
            lat=int(37.7749 * DEGREES_SCALE),
            lon=int(-122.4194 * DEGREES_SCALE),
            alt=int(120.5 * MM_PER_M),
            relative_alt=int(50.0 * MM_PER_M),
            vx=int(5.0 * CM_PER_M),
            vy=int(-2.0 * CM_PER_M),
            # MAVLink vz is positive DOWN: +150 cm/s means DESCENDING at 1.5 m/s.
            vz=150,
            hdg=9000,
        )
    )
    assert observation is not None
    assert observation.geo is not None
    assert observation.geo.lat == pytest.approx(37.7749, abs=1e-6)
    assert observation.geo.lon == pytest.approx(-122.4194, abs=1e-6)
    assert observation.geo.alt == pytest.approx(120.5)
    assert observation.payload["relative_alt_m"] == pytest.approx(50.0)
    velocity = observation.payload["velocity_ms"]
    assert velocity is not None
    assert velocity["north"] == pytest.approx(5.0)
    assert velocity["east"] == pytest.approx(-2.0)
    assert observation.payload["heading_deg"] == pytest.approx(90.0)
    # THE SIGN FLIP. MAVLink NED is positive down; this platform's Velocity is positive up. Without the
    # negation a descending drone reads as climbing — an error that survives every test checking only
    # magnitudes, and one a reviewer would not spot in a stream of unit conversions.
    assert velocity["up"] == pytest.approx(-1.5), "a descending drone must not read as climbing"


def test_a_position_at_null_island_is_rejected() -> None:
    """0,0 is what an autopilot reports before it has a fix.

    Letting it through would put an entity in the Atlantic and skew every spatial query that averages positions.
    """
    connector = a_drone()
    assert connector._handle(FakeMavlinkMessage("GLOBAL_POSITION_INT", lat=0, lon=0, alt=0)) is None


def test_battery_not_reported_is_none_not_zero() -> None:
    """-1 means "not reported", and treating it as 0 produces a fleet that all appears about to fall."""
    connector = a_drone()
    connector._handle(FakeMavlinkMessage("SYS_STATUS", battery_remaining=-1))
    assert connector._battery_percent is None
    connector._handle(FakeMavlinkMessage("SYS_STATUS", battery_remaining=64))
    assert connector._battery_percent == 64.0


def test_an_unknown_heading_is_none_not_65535() -> None:
    connector = a_drone()
    observation = connector._handle(
        FakeMavlinkMessage("GLOBAL_POSITION_INT", lat=1, lon=1, alt=0, hdg=65535)
    )
    assert observation is not None
    assert observation.payload["heading_deg"] is None


def test_state_messages_do_not_produce_observations() -> None:
    """Battery and mode are STATE, not events.

    Emitting one per message would triple the volume and tell nobody anything new; they ride along on the next
    position instead.
    """
    connector = a_drone()
    assert connector._handle(FakeMavlinkMessage("SYS_STATUS", battery_remaining=50)) is None
    assert (
        connector._handle(FakeMavlinkMessage("STATUSTEXT", text="EKF variance", severity=4)) is None
    )
    observation = connector._handle(FakeMavlinkMessage("GLOBAL_POSITION_INT", lat=1, lon=1, alt=0))
    assert observation is not None
    assert observation.payload["battery_percent"] == 50.0


def test_gps_raw_has_no_velocity_rather_than_a_fabricated_zero() -> None:
    """A stationary reading and an absent one must not look the same to a tracker."""
    connector = a_drone()
    observation = connector._handle(
        FakeMavlinkMessage(
            "GPS_RAW_INT", lat=int(1 * DEGREES_SCALE), lon=int(2 * DEGREES_SCALE), alt=0
        )
    )
    assert observation is not None
    # None rather than a fabricated zero: a stationary reading and an absent one must not look the same.
    assert observation.payload["velocity_ms"] is None


def test_the_drone_does_not_assert_an_entity_id() -> None:
    """The MAVLink system id is a hint for fusion, not a claim about identity.

    A telemetry link asserting identity would let it overrule a camera track that fusion is better placed to
    reconcile.
    """
    connector = a_drone()
    observation = connector._handle(
        FakeMavlinkMessage("GLOBAL_POSITION_INT", lat=1, lon=1, alt=0, src_system=7)
    )
    assert observation is not None
    assert not hasattr(observation, "entity_id")
    assert observation.payload["mavlink_system"] == 7


def test_the_message_whitelist_is_small() -> None:
    """A stream carries forty types at different rates.

    Turning all of them into observations would produce a hundred a second of which two matter.
    """
    assert len(INTERESTING) <= 8
    assert "GLOBAL_POSITION_INT" in INTERESTING


def test_positions_are_throttled() -> None:
    """A yard drone at 10m/s moves 5m between 2Hz reports, well inside what anything downstream cares about."""
    connector = MavlinkDroneConnector(a_config("drone_mavlink", min_interval_s=999))
    first = connector._handle(FakeMavlinkMessage("GLOBAL_POSITION_INT", lat=1, lon=1, alt=0))
    second = connector._handle(FakeMavlinkMessage("GLOBAL_POSITION_INT", lat=2, lon=2, alt=0))
    assert first is not None
    assert second is None, "a second position inside the interval must be dropped"


# --- RTSP -----------------------------------------------------------------------------------------
def test_credentials_are_stripped_from_camera_urls() -> None:
    """`rtsp://admin:hunter2@10.0.0.5/stream` is the NORMAL form for a camera.

    That string ends up in logs, health payloads and error messages, and `describe()` is served on `/health` —
    the least private endpoint the service has.
    """
    assert _redact("rtsp://admin:hunter2@10.0.0.5/stream") == "rtsp://***@10.0.0.5/stream"
    assert "hunter2" not in _redact("rtsp://admin:hunter2@10.0.0.5/stream")
    # No credentials, unchanged.
    assert _redact("rtsp://10.0.0.5/stream") == "rtsp://10.0.0.5/stream"


def test_the_camera_describe_never_leaks_a_password() -> None:
    connector = RtspCameraConnector(
        a_config("camera_rtsp", url="rtsp://admin:hunter2@10.0.0.5/stream")
    )
    described = json.dumps(connector.describe())
    assert "hunter2" not in described
    assert "***" in described


def test_the_camera_frame_key_goes_in_raw_ref() -> None:
    """The most consequential thing this file caught.

    `services/perception` reads `observation.raw_ref` and SKIPS any observation without one. My first version put
    the key in `payload["frame_key"]`, so perception would never have processed a single real camera frame —
    silently, while every counter in the connector said it was healthy. A schema field existing for exactly this
    purpose and going unused is the kind of mistake that survives code review.
    """
    from sio_ingest.connectors import rtsp

    source = Path(rtsp.__file__).read_text()
    assert "raw_ref=key" in source
    assert '"frame_key": key' not in source


def test_the_camera_publish_rate_is_independent_of_the_read_rate() -> None:
    """A camera at 25fps is 25 chances a second to run a detector that takes 80ms.

    The naive version falls behind and then reports the world as it was thirty seconds ago — the worst failure
    for a live platform, because the map looks fine.
    """
    connector = RtspCameraConnector(a_config("camera_rtsp", url="rtsp://x/y", publish_fps=2))
    assert connector.min_interval_s == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_the_camera_refuses_an_unknown_backend() -> None:
    connector = RtspCameraConnector(a_config("camera_rtsp", url="rtsp://x/y", backend="magic"))
    with pytest.raises(ValueError, match="unknown backend"):
        await connector.start()


@pytest.mark.asyncio
async def test_the_camera_refuses_a_missing_url() -> None:
    with pytest.raises(ValueError, match=r"options\.url"):
        await RtspCameraConnector(a_config("camera_rtsp")).start()


def test_the_gstreamer_pipeline_does_not_buffer() -> None:
    """The defaults buffer for smooth playback, which is right for a video player.

    A platform wants the newest frame and nothing else; a buffered pipeline means reading the past while
    appearing healthy.
    """
    connector = RtspCameraConnector(a_config("camera_rtsp", url="rtsp://x/y", backend="gstreamer"))
    pipeline = connector._gst_pipeline()
    assert "latency=0" in pipeline
    assert "drop-on-latency=true" in pipeline
    assert "max-buffers=1" in pipeline


# --- STAC satellite -------------------------------------------------------------------------------
def test_the_satellite_bbox_is_small() -> None:
    """A yard is a few hundred metres. Asking for a degree would return scenes covering half a state."""
    connector = StacSatelliteConnector(
        a_config("satellite_stac", lat=37.0, lon=-122.0, box_deg=0.05)
    )
    west, south, east, north = connector.bbox
    assert east - west == pytest.approx(0.05)
    assert north - south == pytest.approx(0.05)
    assert west < -122.0 < east


@pytest.mark.asyncio
async def test_a_cloudy_scene_is_skipped_even_if_the_server_returns_it() -> None:
    """Not every STAC implementation honours `query`.

    A 90%-cloud scene is not a worse observation — it is not an observation, because the site is not in it. One
    reaching the world model would put a white square in it and a spurious "conditions changed" in the events.
    """
    connector = StacSatelliteConnector(
        a_config("satellite_stac", max_cloud_percent=30, fetch_assets=False)
    )
    connector._client = FakeStac(
        {
            "features": [
                {
                    "id": "cloudy",
                    "properties": {"eo:cloud_cover": 92.0, "datetime": "2026-01-01T00:00:00Z"},
                },
                {
                    "id": "clear",
                    "properties": {"eo:cloud_cover": 4.0, "datetime": "2026-01-02T00:00:00Z"},
                },
            ]
        }
    )
    observations = await connector._poll()
    assert [item.payload["scene_id"] for item in observations] == ["clear"]
    assert connector._skipped_cloud == 1


@pytest.mark.asyncio
async def test_a_scene_is_only_ingested_once() -> None:
    """A STAC search over a fortnight returns the same scenes every poll.

    Without deduplication, one cloud-free pass would become an observation every six hours for two weeks.
    """
    connector = StacSatelliteConnector(a_config("satellite_stac", fetch_assets=False))
    connector._client = FakeStac(
        {
            "features": [
                {
                    "id": "s1",
                    "properties": {"eo:cloud_cover": 1.0, "datetime": "2026-01-01T00:00:00Z"},
                }
            ]
        }
    )
    assert len(await connector._poll()) == 1
    assert await connector._poll() == []


@pytest.mark.asyncio
async def test_the_scene_capture_time_is_distinct_from_the_ingest_time() -> None:
    """A pass from three days ago ingested today is an observation OF three days ago.

    Conflating the two would let a satellite scene contradict a camera about the present.
    """
    connector = StacSatelliteConnector(a_config("satellite_stac", fetch_assets=False))
    connector._client = FakeStac(
        {
            "features": [
                {
                    "id": "s1",
                    "properties": {"eo:cloud_cover": 1.0, "datetime": "2026-01-01T10:04:00Z"},
                }
            ]
        }
    )
    observation = (await connector._poll())[0]
    assert observation.payload["captured_ts"] == "2026-01-01T10:04:00Z"
    assert observation.ts.isoformat() != "2026-01-01T10:04:00Z"


@pytest.mark.asyncio
async def test_a_failing_stac_server_degrades_rather_than_raising() -> None:
    """This connector is pointed at an endpoint nobody here controls."""
    connector = StacSatelliteConnector(a_config("satellite_stac", fetch_assets=False))
    connector._client = FakeStac(None, status=503)
    assert await connector._poll() == []
    assert "503" in (await connector.health())


class FakeStac:
    """A fake Earth Search. Returns whatever body the test wants."""

    def __init__(self, body: dict[str, Any] | None, status: int = 200) -> None:
        self._body = body
        self._status = status

    async def post(self, _url: str, json: dict[str, Any] | None = None) -> Any:
        return FakeResponse(self._body, self._status)


class FakeResponse:
    def __init__(self, body: dict[str, Any] | None, status: int) -> None:
        self._body = body
        self.status_code = status
        self.text = "error" if status >= 400 else "ok"
        self.content = b"bytes"

    def json(self) -> Any:
        return self._body or {}


# --- traffic --------------------------------------------------------------------------------------
def test_distance_uses_a_great_circle() -> None:
    """Treating degrees as a grid is wrong by tens of percent at the latitudes where freight moves."""
    # San Francisco to Oakland, about 13km.
    assert _haversine_km(37.7749, -122.4194, 37.8044, -122.2712) == pytest.approx(13.4, abs=1.0)
    assert _haversine_km(1.0, 2.0, 1.0, 2.0) == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("geometry", "expected"),
    [
        ({"type": "Point", "coordinates": [-122.4, 37.8]}, (-122.4, 37.8)),
        ({"type": "LineString", "coordinates": [[-122.4, 37.8], [-122.5, 37.9]]}, (-122.4, 37.8)),
        (
            {"type": "Polygon", "coordinates": [[[-122.4, 37.8], [-122.5, 37.9]]]},
            (-122.4, 37.8),
        ),
        ({"type": "Point", "coordinates": []}, None),
        ({}, None),
    ],
)
def test_any_geojson_geometry_yields_a_representative_point(
    geometry: dict[str, Any], expected: tuple[float, float] | None
) -> None:
    """Feeds use Point for a crash, LineString for a closed stretch, sometimes Polygon for a zone.

    The question is "is this near the site", and the head of a closed stretch answers it.
    """
    assert _first_point(geometry) == expected


@pytest.mark.asyncio
async def test_an_incident_beyond_the_radius_is_dropped() -> None:
    """Most open feeds serve a whole country.

    Every incident in it would otherwise become an observation about our site.
    """
    connector = TrafficConnector(
        a_config("traffic_incidents", lat=37.7749, lon=-122.4194, radius_km=25, url="http://x")
    )
    connector._client = FakeFeed(
        {
            "features": [
                {
                    "geometry": {"type": "Point", "coordinates": [-122.42, 37.78]},
                    "properties": {"id": "near", "description": "Lane closed"},
                },
                {
                    # Los Angeles, ~560km away.
                    "geometry": {"type": "Point", "coordinates": [-118.24, 34.05]},
                    "properties": {"id": "far", "description": "Crash"},
                },
            ]
        }
    )
    observations = await connector._poll()
    assert [item.payload["incident_id"] for item in observations] == ["near"]
    assert connector._out_of_range == 1


@pytest.mark.asyncio
async def test_the_distance_travels_with_the_incident() -> None:
    """ "3km from the gate" and "24km away" warrant different responses.

    The consumer should not have to redo the trigonometry to tell them apart.
    """
    connector = TrafficConnector(
        a_config("traffic_incidents", lat=37.7749, lon=-122.4194, url="http://x")
    )
    connector._client = FakeFeed(
        {
            "features": [
                {
                    "geometry": {"type": "Point", "coordinates": [-122.4194, 37.8]},
                    "properties": {"id": "i1", "description": "Debris"},
                }
            ]
        }
    )
    observation = (await connector._poll())[0]
    assert observation.payload["distance_km"] == pytest.approx(2.8, abs=0.5)


@pytest.mark.asyncio
async def test_an_incident_is_reported_once() -> None:
    """Incidents persist in a feed for hours.

    Without deduplication a single closed lane produces an observation every five minutes for a day, and the
    event engine keeps re-deciding about it.
    """
    connector = TrafficConnector(
        a_config("traffic_incidents", url="http://x", lat=37.7749, lon=-122.4194)
    )
    feed = FakeFeed(
        {
            "features": [
                {
                    "geometry": {"type": "Point", "coordinates": [-122.42, 37.775]},
                    "properties": {"id": "i1", "description": "Lane closed"},
                }
            ]
        }
    )
    connector._client = feed
    assert len(await connector._poll()) == 1
    assert await connector._poll() == []


@pytest.mark.asyncio
async def test_a_keyed_provider_is_refused_with_a_pointer() -> None:
    """Rather than silently behaving like the open one."""
    connector = TrafficConnector(a_config("traffic_incidents", provider="tomtom", url="http://x"))
    with pytest.raises(ValueError, match="unknown traffic provider"):
        await connector.start()


@pytest.mark.asyncio
async def test_the_traffic_connector_needs_a_url() -> None:
    with pytest.raises(ValueError, match=r"options\.url"):
        await TrafficConnector(a_config("traffic_incidents")).start()


class FakeFeed:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def get(self, _url: str) -> Any:
        return FakeResponse(self._body, 200)


# --- health -----------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL_P7)
async def test_every_connector_reports_health_before_it_has_started(kind: str) -> None:
    """`/health` is polled from the moment the service is up.

    A connector whose `health()` raises because it has not connected yet takes the service's health endpoint
    down with it — and the endpoint exists to say what is wrong.
    """
    status = await build_connector(a_config(kind)).health()
    assert isinstance(status, str)
    assert status


# --- routing -----------------------------------------------------------------------------------------
def test_every_modality_routes_to_a_topic() -> None:
    """The gap the Phase 7 connectors exposed.

    `TOPIC_BY_MODALITY` had no entry for ENTERPRISE or TRAFFIC, and the fallback was a *silent* `raw.iot` — which
    is where the events engine reads sensor readings. So a WMS dock booking arrived on the bus looking like a
    temperature probe: not lost, but mislabelled in a way that makes every downstream filter on `raw.iot` subtly
    wrong, while the connector logged "3 rows read" and every counter said healthy.

    Asserted over the whole enum rather than the six modalities that happened to be mapped, so the next
    connector to introduce a modality fails here instead of quietly becoming IoT.
    """
    from sio_ingest.service import TOPIC_BY_MODALITY

    unmapped = [str(modality) for modality in Modality if modality not in TOPIC_BY_MODALITY]
    assert not unmapped, (
        "these modalities fall through to raw.iot, where sensor readings are read from: "
        f"{unmapped}. Add them to TOPIC_BY_MODALITY."
    )


def test_the_unmapped_fallback_is_loud() -> None:
    """A silent catch-all makes a routing mistake indistinguishable from correct behaviour.

    Once per modality rather than per message, because a mislabelled source at 15Hz would otherwise bury the
    rest of the log in one line.
    """
    from sio_ingest import service as ingest_service

    source = Path(ingest_service.__file__).read_text()
    assert "ingest.unmapped_modality" in source
    assert "_unmapped_warned" in source


def test_enterprise_and_traffic_have_their_own_topics() -> None:
    """Rather than sharing the sensor topic, which is what made this a bug rather than a tidiness question."""
    from sio_schemas import Topic

    assert str(Topic.RAW_ENTERPRISE) == "raw.enterprise"
    assert str(Topic.RAW_TRAFFIC) == "raw.traffic"
