-- Mission comms log (PRD M17, Phase 6).
--
-- The `missions` table already exists in 004, with objectives in its `payload` jsonb. This adds the comms log,
-- which is the part that makes a mission reviewable rather than merely trackable: after an incident the question
-- is never "what was the state" but "who said what, when, and what did they know at the time".
--
-- APPEND-ONLY, enforced by a trigger, unlike `webhook_deliveries` which is deliberately mutable. The distinction
-- is whether the row is a record of the world or a record of our own machinery. A delivery attempt is machinery
-- and updating it is honest bookkeeping. A comms entry is testimony — somebody said a thing at a time — and a
-- testimony that can be edited afterwards is worth nothing in the review that follows a bad outcome. This is the
-- same reasoning as `audit_log`, and it reuses the same trigger function.

CREATE TABLE IF NOT EXISTS mission_comms (
    tenant_id     text NOT NULL,
    comm_id       text NOT NULL,
    mission_id    text NOT NULL,
    ts            timestamptz NOT NULL DEFAULT now(),
    -- Who. A free string rather than a foreign key to a users table: comms come from people, from the platform
    -- itself, and from radio call-signs that exist in nobody's directory.
    author        text NOT NULL,
    -- What kind of traffic. `message` from a human, `system` from the platform (a state change, an objective
    -- completing), `order` for an instruction, `sitrep` for a status report.
    kind          text NOT NULL DEFAULT 'message',
    body          text NOT NULL,
    -- What it refers to, when it refers to something: an objective, an event, an alert, a decision. Kept as a
    -- loose reference rather than five nullable foreign keys, because the interesting property is "this message
    -- was about that thing" and the referent's table is implied by its id prefix.
    ref           text,
    PRIMARY KEY (tenant_id, comm_id)
);

-- The only query: the log for one mission, in order. Ascending, because a comms log is read forwards — it is a
-- narrative, unlike an alert list where the newest matters most.
CREATE INDEX IF NOT EXISTS mission_comms_mission_idx
    ON mission_comms (tenant_id, mission_id, ts);

DO $$
BEGIN
    -- Reuses the append-only trigger from 005. Guarded, so this migration is still idempotent if 005 has not
    -- run — a missing function should not stop the table from being created.
    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'sio_forbid_mutation') THEN
        DROP TRIGGER IF EXISTS mission_comms_append_only ON mission_comms;
        CREATE TRIGGER mission_comms_append_only
            BEFORE UPDATE OR DELETE ON mission_comms
            FOR EACH ROW EXECUTE FUNCTION sio_forbid_mutation();
    END IF;
END $$;

-- Resource assignment, as its own table rather than the `resources` text[] on `missions`.
--
-- The array cannot answer the question that matters: "is this drone already committed?" Answering it from an
-- array means scanning every active mission and unnesting, and the check has to happen on every assignment. A
-- row per assignment with a partial unique index makes double-booking impossible in the database rather than
-- merely unlikely in the service — which is the difference between an invariant and a habit.
--
-- The `resources` array on `missions` remains as the denormalised read path, because the console renders a
-- mission whole and a join per mission for a list view is waste.
CREATE TABLE IF NOT EXISTS mission_resources (
    tenant_id     text NOT NULL,
    mission_id    text NOT NULL,
    resource_id   text NOT NULL,
    assigned_ts   timestamptz NOT NULL DEFAULT now(),
    released_ts   timestamptz,
    assigned_by   text,
    role          text,
    PRIMARY KEY (tenant_id, mission_id, resource_id)
);

-- ONE ACTIVE MISSION PER RESOURCE, enforced here rather than checked in the service.
--
-- Dispatching the same drone to two fires is the failure mode this exists to prevent, and it is precisely the
-- kind of thing that slips through a service-level check under concurrency: two requests read "not assigned",
-- both write. A partial unique index makes the second write fail, whatever the service believed.
CREATE UNIQUE INDEX IF NOT EXISTS mission_resources_one_mission_idx
    ON mission_resources (tenant_id, resource_id)
    WHERE released_ts IS NULL;

CREATE INDEX IF NOT EXISTS mission_resources_mission_idx
    ON mission_resources (tenant_id, mission_id, released_ts);
