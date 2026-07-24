"""Closed vocabularies: bus topics, modalities, entity/relationship/event types, severity."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Topic(StrEnum):
    """Redis Streams topic names (PRD §10 inter-service contract).

    ``raw.*`` carries un-interpreted signals straight off a connector; everything else is
    derived. Consumers use a group named after the service, so adding a consumer never
    steals messages from an existing one.
    """

    RAW_FRAMES = "raw.frames"
    RAW_GPS = "raw.gps"
    RAW_IOT = "raw.iot"
    RAW_AUDIO = "raw.audio"
    RAW_WEATHER = "raw.weather"
    RAW_SATELLITE = "raw.satellite"
    DETECTIONS = "detections"
    TRACKS = "tracks"
    ENTITIES = "entities"
    EVENTS = "events"
    FORECASTS = "forecasts"
    DECISIONS = "decisions"
    ALERTS = "alerts"
    ACTIONS = "actions"
    AUDIT = "audit"

    @classmethod
    def raw_topics(cls) -> tuple[Topic, ...]:
        return tuple(t for t in cls if t.value.startswith("raw."))

    @property
    def dead_letter(self) -> str:
        """Stream that receives messages exceeding ``SIO_BUS_MAX_RETRIES`` deliveries."""
        return f"dlq.{self.value}"


class Modality(StrEnum):
    """The kind of signal an observation carries."""

    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    GPS = "gps"
    IOT = "iot"
    RFID = "rfid"
    RADAR = "radar"
    LIDAR = "lidar"
    SATELLITE = "satellite"
    WEATHER = "weather"
    TRAFFIC = "traffic"
    ENTERPRISE = "enterprise"
    DOCUMENT = "document"
    MANUAL = "manual"


class EntityType(StrEnum):
    """Real-world object classes in the world model (PRD M7)."""

    PERSON = "person"
    VEHICLE = "vehicle"
    TRUCK = "truck"
    FORKLIFT = "forklift"
    DRONE = "drone"
    SHIP = "ship"
    AIRCRAFT = "aircraft"
    ANIMAL = "animal"
    BUILDING = "building"
    ROAD = "road"
    BRIDGE = "bridge"
    HOSPITAL = "hospital"
    CAMERA = "camera"
    SENSOR = "sensor"
    MACHINE = "machine"
    CONTAINER = "container"
    PACKAGE = "package"
    ZONE = "zone"
    GATE = "gate"
    DOCK = "dock"
    COMPANY = "company"
    COUNTRY = "country"
    RESOURCE = "resource"
    HAZARD = "hazard"
    UNKNOWN = "unknown"


class RelationshipType(StrEnum):
    """Edge types in the world model (PRD M7). Edges are bitemporal, never overwritten."""

    OWNS = "owns"
    VISITED = "visited"
    CONTAINS = "contains"
    CONNECTED_TO = "connected_to"
    SEEN_BY = "seen_by"
    TRANSPORTING = "transporting"
    COMMUNICATED_WITH = "communicated_with"
    ASSIGNED_TO = "assigned_to"
    ENTERED = "entered"
    EXITED = "exited"
    SAME_AS = "same_as"
    NEAR = "near"
    COVERS = "covers"
    CAUSED = "caused"
    RESPONDS_TO = "responds_to"


class EventType(StrEnum):
    """Meaningful things the event engine can assert (PRD M9)."""

    UNAUTHORIZED_ENTRY = "unauthorized_entry"
    FIRE_DETECTED = "fire_detected"
    SMOKE_DETECTED = "smoke_detected"
    SPEEDING = "speeding"
    CROWD_GATHERING = "crowd_gathering"
    MACHINE_STOPPED = "machine_stopped"
    POWER_FAILURE = "power_failure"
    FORCED_DOOR = "forced_door"
    PERSON_FELL = "person_fell"
    ABANDONED_PACKAGE = "abandoned_package"
    SUSPICIOUS_MEETING = "suspicious_meeting"
    DWELL_EXCEEDED = "dwell_exceeded"
    ZONE_ENTERED = "zone_entered"
    ZONE_EXITED = "zone_exited"
    ZONE_BREACH = "zone_breach"
    CONGESTION = "congestion"
    TEMPERATURE_SPIKE = "temperature_spike"
    GUNSHOT = "gunshot"
    GLASS_BREAK = "glass_break"
    SCREAM = "scream"
    ANOMALY_DETECTED = "anomaly_detected"
    ENTITY_APPEARED = "entity_appeared"
    ENTITY_LOST = "entity_lost"
    WORKFLOW_STEP = "workflow_step"
    MISSION_UPDATE = "mission_update"
    AGENT_PROPOSAL = "agent_proposal"


class Severity(StrEnum):
    """Ordered severity. Use :meth:`rank` for comparisons — string order is meaningless."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Severity):
            return self.rank < other.rank
        return NotImplemented


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class SeverityRank(IntEnum):
    """Numeric mirror of :class:`Severity` for scoring maths and SQL ordering."""

    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class TrackStatus(StrEnum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    LOST = "lost"
    REMOVED = "removed"


class AlertState(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


class ApprovalState(StrEnum):
    """Human-on-the-loop gate for anything an agent wants to actually do (PRD M14)."""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class MissionState(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPENSATED = "compensated"


class EvidenceKind(StrEnum):
    """What a piece of evidence in an explanation points at."""

    FRAME = "frame"
    OBSERVATION = "observation"
    DETECTION = "detection"
    TRACK = "track"
    ENTITY = "entity"
    EVENT = "event"
    FORECAST = "forecast"
    DECISION = "decision"
    QUERY = "query"
    DOCUMENT = "document"
    SIMULATION = "simulation"
    RULE = "rule"
    MODEL = "model"


class ActionType(StrEnum):
    """Things SIO can recommend or do in the physical world (PRD M12)."""

    MOVE_DRONE = "move_drone"
    DISPATCH_DRONE = "dispatch_drone"
    DEPLOY_AMBULANCE = "deploy_ambulance"
    DISPATCH_PATROL = "dispatch_patrol"
    EVACUATE_SECTOR = "evacuate_sector"
    CLOSE_GATE = "close_gate"
    OPEN_GATE = "open_gate"
    INCREASE_SECURITY = "increase_security"
    DELAY_SHIPMENT = "delay_shipment"
    REROUTE_VEHICLE = "reroute_vehicle"
    NOTIFY = "notify"
    CREATE_INCIDENT = "create_incident"
    GENERATE_REPORT = "generate_report"
    NO_ACTION = "no_action"


class Role(StrEnum):
    """RBAC roles (PRD §4 personas → PRD M21 roles)."""

    OPERATOR = "operator"
    COMMANDER = "commander"
    INTEGRATOR = "integrator"
    ML_ENGINEER = "ml_engineer"
    ADMIN = "admin"
    SERVICE = "service"
