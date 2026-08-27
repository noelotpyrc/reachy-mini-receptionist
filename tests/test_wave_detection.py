import pytest

from reachy_mini_brain.official_runtime.wave_detection import HandMotionWaveDetector


def test_hand_motion_wave_detects_one_horizontal_round_trip():
    detector = HandMotionWaveDetector()
    observations = [0.30, 0.30, 0.30, 0.50, 0.70, 0.50, 0.30, 0.50, 0.70]

    statuses = [
        detector.update(timestamp_s=index * 0.2, center_x=center_x)
        for index, center_x in enumerate(observations)
    ]

    assert statuses[-1].detected is True
    assert statuses[-1].direction_changes >= 2
    assert statuses[-1].displacement >= 0.08


def test_hand_motion_wave_rejects_one_way_hand_movement():
    detector = HandMotionWaveDetector()

    statuses = [
        detector.update(timestamp_s=index * 0.2, center_x=center_x)
        for index, center_x in enumerate([0.30, 0.38, 0.46, 0.54, 0.62, 0.70])
    ]

    assert all(not status.detected for status in statuses)
    assert statuses[-1].reason == "insufficient_direction_changes"


def test_hand_motion_wave_rejects_small_jitter():
    detector = HandMotionWaveDetector()

    statuses = [
        detector.update(timestamp_s=index * 0.2, center_x=center_x)
        for index, center_x in enumerate([0.50, 0.515, 0.495, 0.514, 0.496, 0.51])
    ]

    assert all(not status.detected for status in statuses)


def test_hand_motion_wave_expires_old_samples():
    detector = HandMotionWaveDetector(timeout_s=2.0)
    for index, center_x in enumerate([0.40, 0.50, 0.60, 0.50]):
        detector.update(timestamp_s=index * 0.2, center_x=center_x)

    status = detector.update(timestamp_s=3.0, center_x=0.40)

    assert status.detected is False
    assert status.samples == 1
    assert status.reason == "insufficient_samples"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timeout_s": 0.0}, "timeout_s"),
        ({"min_displacement": 0.0}, "min_displacement"),
        ({"min_cycles": 0}, "min_cycles"),
    ],
)
def test_hand_motion_wave_rejects_invalid_configuration(kwargs, message):
    with pytest.raises(ValueError, match=message):
        HandMotionWaveDetector(**kwargs)
