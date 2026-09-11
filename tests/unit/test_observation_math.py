"""Saved-sample analytics do not invent identities, continuity, or media links."""

import json
from copy import deepcopy

import pytest
from sio_api.observation_math import movement_summary, object_tracks

VIDEO = {"video_id": "vid_" + "a" * 32, "title": "Authored movement fixture"}
ANALYSIS_ID = "ana_" + "b" * 32
LINE = {"name": "Gate", "start": [0.2, 0.5], "end": [0.8, 0.5]}
ZONE = {
    "zone_id": "lower",
    "name": "Lower half",
    "points": [[0, 0.5], [1, 0.5], [1, 1], [0, 1]],
}


def obj(x=0.5, y=0.6, *, track="local-1", label="person", confidence=0.8):
    return {
        "track_id": track,
        "class_name": label,
        "confidence": confidence,
        "bbox": [x - 0.02, y - 0.04, x + 0.02, y],
    }


def analysis(*frames, zones=None):
    return {
        "analysis_id": ANALYSIS_ID,
        "video_id": VIDEO["video_id"],
        "status": "completed",
        "zones": zones or [],
        "detections": [
            {"at_s": index * 0.5, "frame_index": index, "objects": objects}
            for index, objects in enumerate(frames)
        ],
    }


def move(*positions, **configuration):
    recorded = analysis(*[[obj(x, y)] for x, y in positions])
    return movement_summary(VIDEO, recorded, {"line": LINE, **configuration})


def test_track_filters_only_retain_matching_observations_and_their_zones():
    saved = analysis(
        [obj(y=0.4)],
        [obj(y=0.52, confidence=0.4)],
        [obj(y=0.6, confidence=0.9)],
        [obj(y=0.7, confidence=0.7)],
        [obj(y=0.75, confidence=0.99)],
        zones=[ZONE],
    )
    tracks = object_tracks(
        VIDEO, saved, class_name="person", zone_id="lower", min_confidence=0.6, end_s=1.5
    )
    assert len(tracks) == 1
    track = tracks[0]
    assert (track["first_s"], track["last_s"], track["at_s"]) == (1, 1.5, 1)
    assert track["sample_count"] == 2
    assert track["confidence_min"] == 0.7 and track["confidence_max"] == 0.9
    assert track["zone_ids"] == ["lower"] and track["zone_names"] == ["Lower half"]
    assert [sample["at_s"] for sample in track["observations"]] == [1, 1.5]
    assert not object_tracks(VIDEO, saved, class_name="bus")
    assert not object_tracks(VIDEO, saved, zone_id="missing")
    assert [
        sample["at_s"] for sample in object_tracks(VIDEO, saved, start_s=1.5)[0]["observations"]
    ] == [1.5, 2]


def test_zone_filter_uses_bottom_centre_and_frozen_analysis_polygon():
    # Most of this box is above the zone, but its bottom centre is on its boundary.
    value = obj(y=0.5)
    value["bbox"][1] = 0.1
    video = {**VIDEO, "zones": [{**ZONE, "points": [[0, 0], [1, 0], [1, 0.2], [0, 0.2]]}]}
    saved = analysis([value], zones=[ZONE])
    assert len(object_tracks(video, saved, zone_id="lower")) == 1
    assert not object_tracks(video, analysis([value]), zone_id="lower")


def test_confidence_zero_is_known_unknown_is_not_and_motion_never_claims_confidence():
    saved = analysis(
        [
            obj(track="zero", confidence=0),
            obj(track="unknown", confidence=None),
            obj(track="moving", label="motion", confidence=0.99),
        ]
    )
    tracks = {row["track_id"]: row for row in object_tracks(VIDEO, saved)}
    assert tracks["zero"]["confidence_min"] == 0
    assert tracks["unknown"]["confidence_max"] is None
    assert tracks["moving"]["confidence_max"] is None
    assert tracks["moving"]["observations"][0]["confidence"] is None
    assert [row["track_id"] for row in object_tracks(VIDEO, saved, min_confidence=0)] == ["zero"]


def test_local_track_class_pair_prevents_class_collision_and_duplicate_sample_counts():
    saved = analysis(
        [obj(confidence=0.4), obj(confidence=0.9), obj(label="bus")],
        [obj(), obj(label="bus")],
    )
    tracks = object_tracks(VIDEO, saved)
    assert [(row["class_name"], row["sample_count"]) for row in tracks] == [
        ("bus", 2),
        ("person", 2),
    ]
    assert tracks[1]["observations"][0]["confidence"] == 0.9
    assert all(row["analysis_id"] == ANALYSIS_ID for row in tracks)
    another = {**saved, "analysis_id": "ana_" + "c" * 32}
    assert object_tracks(VIDEO, another)[0]["analysis_id"] != tracks[0]["analysis_id"]


def test_media_links_are_canonical_and_functions_do_not_mutate_retained_data():
    saved = analysis([obj()], [obj(y=0.7)])
    saved["detections"][0]["frame_url"] = "https://untrusted.invalid/a.jpg"
    original = deepcopy(saved)
    expected = f"/api/review/videos/{VIDEO['video_id']}/frames/{ANALYSIS_ID}/0"
    track = object_tracks(VIDEO, saved)[0]
    summary = movement_summary(VIDEO, saved, {"line": LINE})
    assert track["frame_url"] == track["observations"][0]["frame_url"] == expected
    assert summary["occupancy"][0]["frame_url"] == expected
    assert saved == original


@pytest.mark.parametrize(
    "invalid", [float("nan"), float("inf"), float("-inf"), True, "0.5", 10**1000]
)
def test_malformed_retained_values_are_discarded_without_nonfinite_json(invalid):
    saved = analysis([obj()], [obj()], [obj()])
    saved["detections"][0]["at_s"] = invalid
    saved["detections"][1]["objects"][0]["bbox"][0] = invalid
    saved["detections"][2]["objects"][0]["confidence"] = invalid
    tracks = object_tracks(VIDEO, saved)
    summary = movement_summary(VIDEO, saved, {"line": LINE})
    assert len(tracks) == 1 and tracks[0]["sample_count"] == 1
    assert tracks[0]["confidence_max"] is None
    json.dumps({"tracks": tracks, "movement": summary}, allow_nan=False)


def test_invalid_frames_and_duplicate_frames_do_not_create_counts_or_continuity():
    saved = analysis([obj(y=0.4)], [obj(y=0.5)], [obj(y=0.6)])
    saved["detections"][1]["objects"] = None
    saved["detections"].append(deepcopy(saved["detections"][2]))
    saved["detections"].append({"at_s": 1.5, "frame_index": True, "objects": [obj()]})
    saved["detections"].append({"at_s": 2, "frame_index": 400, "objects": [obj()]})
    summary = movement_summary(VIDEO, saved, {"line": LINE})
    assert summary["sample_count"] == 2
    assert summary["totals"]["total"] == 0


def test_samples_outside_known_video_duration_are_excluded():
    saved = analysis([obj()], [obj()], [obj()])
    video = {**VIDEO, "duration_s": 0.5}
    summary = movement_summary(video, saved, {})
    assert summary["sample_count"] == 2
    assert summary["coverage"] == {"start_s": 0, "end_s": 0.5}
    assert object_tracks(video, saved)[0]["sample_count"] == 2


def test_corrupted_nonempty_sample_is_unknown_not_zero_or_a_partial_count():
    saved = analysis([obj(y=0.4)], [obj(), {"bbox": []}], [obj(y=0.6)])
    summary = movement_summary(VIDEO, saved, {"line": LINE})
    assert summary["sample_count"] == 2
    assert [point["frame_index"] for point in summary["occupancy"]] == [0, 2]
    assert summary["mean_sampled_occupancy"] == 1
    assert summary["totals"]["total"] == 0
    assert object_tracks(VIDEO, saved)[0]["sample_count"] == 2
    entirely_unknown = movement_summary(VIDEO, analysis([{"bbox": []}]), {})
    assert entirely_unknown["mean_sampled_occupancy"] is None
    assert entirely_unknown["sample_count"] == 0


def test_bounds_limit_saved_samples_and_objects_per_sample():
    saved = analysis(*[[obj(track=f"track-{index}") for index in range(40)]] * 370)
    summary = movement_summary(VIDEO, saved, {})
    assert summary["sample_count"] == 360
    assert summary["peak_count"] == 32
    tracks = object_tracks(VIDEO, saved)
    assert len(tracks) == 32 and all(row["sample_count"] == 360 for row in tracks)


def test_occupancy_counts_samples_and_classes_and_breaks_peak_ties_earliest():
    saved = analysis(
        [],
        [obj(track="one"), obj(track="two"), obj(track="bus", label="bus")],
        [obj(track="one")],
        [obj(track="one"), obj(track="two"), obj(track="bus", label="bus")],
    )
    summary = movement_summary(VIDEO, saved, {})
    assert [row["count"] for row in summary["occupancy"]] == [0, 3, 1, 3]
    assert summary["peak_count"] == 3 and summary["peak_at_s"] == 0.5
    assert summary["mean_sampled_occupancy"] == 1.75
    assert summary["coverage"] == {"start_s": 0, "end_s": 1.5}
    assert summary["class_counts"] == [
        {"class_name": "bus", "observation_count": 2, "track_count": 1, "peak_count": 1},
        {"class_name": "person", "observation_count": 5, "track_count": 2, "peak_count": 2},
    ]
    assert summary["totals"] == {"a_to_b": 0, "b_to_a": 0, "total": 0}


def test_empty_saved_samples_are_zero_but_no_samples_have_unknown_mean_and_time():
    zero = movement_summary(VIDEO, analysis([], []), {})
    unknown = movement_summary(VIDEO, analysis(), {})
    assert zero["peak_count"] == zero["mean_sampled_occupancy"] == 0
    assert zero["peak_at_s"] == 0 and zero["sample_count"] == 2
    assert unknown["sample_count"] == 0 and unknown["occupancy"] == []
    assert unknown["peak_at_s"] is None and unknown["mean_sampled_occupancy"] is None
    assert unknown["coverage"] == {"start_s": None, "end_s": None}


def test_image_coordinate_left_is_a_and_returning_track_counts_each_observed_crossing():
    summary = move((0.5, 0.4), (0.5, 0.6), (0.5, 0.4))
    assert summary["totals"] == {"a_to_b": 1, "b_to_a": 1, "total": 2}
    first, second = summary["crossings"]
    assert (first["from_s"], first["to_s"], first["at_s"]) == (0, 0.5, 0.5)
    assert first["direction"] == "a_to_b" and second["direction"] == "b_to_a"
    assert first["track_id"] == second["track_id"] == "local-1"
    reverse = {**LINE, "start": LINE["end"], "end": LINE["start"]}
    assert move((0.5, 0.4), (0.5, 0.6), line=reverse)["crossings"][0]["direction"] == "b_to_a"


def test_infinite_line_crossing_outside_finite_endpoints_is_not_counted():
    assert move((0.9, 0.4), (0.9, 0.6))["totals"]["total"] == 0
    # The chord of stable endpoints crosses the finite line, but the actual path goes around it.
    assert move((0.5, 0.49), (0.9, 0.498), (0.9, 0.502), (0.5, 0.51))["totals"]["total"] == 0
    assert move((0.8, 0.4), (0.8, 0.6))["totals"]["total"] == 1


def test_near_line_jitter_never_establishes_a_side_or_counts_a_crossing():
    assert move((0.5, 0.497), (0.5, 0.503), (0.5, 0.499), (0.5, 0.501))["totals"]["total"] == 0
    assert move((0.5, 0.4), (0.5, 0.503), (0.5, 0.497), (0.5, 0.4))["totals"]["total"] == 0


def test_touch_and_return_inside_the_band_do_not_support_a_later_crossing_around_endpoint():
    # Touching the segment before going around its endpoint is not a finite crossing.
    touch = move((0.5, 0.49), (0.8, 0.5), (0.9, 0.498), (0.9, 0.502), (0.9, 0.51))
    assert touch["totals"]["total"] == 0
    # Opposite-direction jitter inside the band cancels, then the path crosses outside.
    returned = move(
        (0.5, 0.49), (0.5, 0.502), (0.5, 0.498), (0.9, 0.498), (0.9, 0.502), (0.9, 0.51)
    )
    assert returned["totals"]["total"] == 0


def test_dead_band_boundary_is_inclusive_despite_floating_point_roundoff():
    assert move((0.5, 0.495), (0.5, 0.505))["totals"]["total"] == 0


def test_contiguous_on_line_samples_bridge_stable_sides_without_counting_first_sighting():
    summary = move((0.5, 0.4), (0.5, 0.5), (0.5, 0.5), (0.5, 0.6))
    assert summary["totals"]["total"] == 1
    assert summary["crossings"][0]["from_s"] == 0
    assert summary["crossings"][0]["to_s"] == 1.5
    assert move((0.5, 0.5), (0.5, 0.6))["totals"]["total"] == 0


def test_diagonal_line_tolerance_is_perpendicular_distance_not_unnormalized_cross_product():
    line = {"name": "Short diagonal", "start": [0.4, 0.4], "end": [0.6, 0.6]}
    result = move((0.5, 0.48), (0.5, 0.52), line=line)
    assert result["totals"]["a_to_b"] == 1
    assert move((0.5, 0.496), (0.5, 0.504), line=line)["totals"]["total"] == 0


def test_missing_observation_missing_frame_and_time_gap_break_crossing_continuity():
    missing_object = analysis([obj(y=0.4)], [], [obj(y=0.6)])
    assert movement_summary(VIDEO, missing_object, {"line": LINE})["totals"]["total"] == 0
    missing_frame = analysis([obj(y=0.4)], [obj(y=0.6)])
    missing_frame["detections"][1].update(at_s=1, frame_index=2)
    assert movement_summary(VIDEO, missing_frame, {"line": LINE})["totals"]["total"] == 0
    long_gap = analysis([obj(y=0.4)], [obj(y=0.6)])
    long_gap["detections"][1]["at_s"] = 1.10001
    assert movement_summary(VIDEO, long_gap, {"line": LINE})["totals"]["total"] == 0
    long_gap["detections"][1]["at_s"] = 1
    assert movement_summary(VIDEO, long_gap, {"line": LINE})["totals"]["total"] == 1


def test_confidence_and_zone_filters_break_observed_crossing_continuity():
    confidence_gap = analysis([obj(y=0.4)], [obj(y=0.5, confidence=0.2)], [obj(y=0.6)])
    summary = movement_summary(VIDEO, confidence_gap, {"line": LINE, "min_confidence": 0.7})
    assert summary["totals"]["total"] == 0
    assert [point["count"] for point in summary["occupancy"]] == [1, 0, 1]
    zone = {**ZONE, "points": [[0.2, 0], [0.8, 0], [0.8, 1], [0.2, 1]]}
    zone_gap = analysis([obj(y=0.4)], [obj(x=0.9, y=0.5)], [obj(y=0.6)], zones=[zone])
    assert (
        movement_summary(VIDEO, zone_gap, {"line": LINE, "zone_id": "lower"})["totals"]["total"]
        == 0
    )


def test_class_filter_and_reused_track_id_do_not_join_different_classes():
    saved = analysis([obj(y=0.4)], [obj(y=0.6, label="bus")])
    summary = movement_summary(VIDEO, saved, {"line": LINE})
    assert summary["totals"]["total"] == 0
    filtered = movement_summary(VIDEO, saved, {"line": LINE, "class_name": "bus"})
    assert [point["count"] for point in filtered["occupancy"]] == [0, 1]


@pytest.mark.parametrize(
    "line", [None, {}, {**LINE, "end": LINE["start"]}, {**LINE, "start": [float("nan"), 0]}]
)
def test_absent_or_malformed_line_keeps_occupancy_but_cannot_create_crossings(line):
    summary = move((0.5, 0.4), (0.5, 0.6), line=line)
    assert summary["peak_count"] == 1
    assert summary["totals"]["total"] == 0
    assert summary["configuration"]["line"] is None
    json.dumps(summary, allow_nan=False)
