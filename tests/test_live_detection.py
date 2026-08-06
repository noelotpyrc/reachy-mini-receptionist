from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import numpy as np
import pytest

from reachy_mini_brain.official_runtime.artifacts import ArtifactRecorder
from reachy_mini_brain.official_runtime.live_detection import (
    DetectionLayerObservation,
    FramePacket,
    LayerDetection,
    LiveDetectionManager,
    NoopLayerTracker,
    PipelineConfig,
    PipelineSpec,
    load_pipeline_config,
)
from reachy_mini_brain.official_runtime.live_rerun import LiveRerunPublisher
from reachy_mini_brain.official_runtime.rerun_vision import RerunVisionRenderer


def test_pipeline_config_loads_versioned_multiple_detectors(tmp_path: Path) -> None:
    path = tmp_path / "pipelines.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pipelines": [
                    _pipeline_payload("door_yolo", "yolo-world"),
                    _pipeline_payload("door_dino", "grounding-dino"),
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_pipeline_config(path)

    assert [item.id for item in config.pipelines] == ["door_yolo", "door_dino"]
    assert len(config.sha256) == 64
    assert config.to_dict()["path"] == str(path.resolve())


def test_pipeline_config_accepts_policy_role_and_dino_input_size(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    payload = _pipeline_payload("door_dino", "grounding-dino")
    payload.update({"role": "policy", "input_size": 640, "inference_fps": 2.0})
    path.write_text(
        json.dumps({"schema_version": 1, "pipelines": [payload]}),
        encoding="utf-8",
    )

    spec = load_pipeline_config(path).pipelines[0]

    assert spec.role == "policy"
    assert spec.input_size == 640
    assert spec.inference_fps == 2.0


def test_pipeline_config_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "pipelines.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pipelines": [
                    _pipeline_payload("door", "yolo-world"),
                    _pipeline_payload("door", "grounding-dino"),
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unique"):
        load_pipeline_config(path)


def test_multiple_pipelines_receive_the_same_cadence_frames() -> None:
    results: list[DetectionLayerObservation] = []
    config = _config(
        PipelineSpec("yolo", "yolo-world", "model", ("door",), 0.1, 1.0),
        PipelineSpec("dino", "grounding-dino", "model", ("door",), 0.3, 1.0),
    )
    manager = LiveDetectionManager(
        run_id="test",
        config=config,
        result_callback=results.append,
        detector_factory=lambda spec: _FakeDetector(spec.id),
        tracker_factory=lambda spec: NoopLayerTracker(),
    )
    manager.start()
    frame = np.zeros((8, 10, 3), dtype=np.uint8)
    manager.submit(FramePacket(0, 10.0, frame))
    manager.submit(FramePacket(1, 10.2, frame))
    _wait_until(lambda: len(results) == 2)
    manager.submit(FramePacket(2, 11.0, frame))
    _wait_until(lambda: len(results) == 4)
    manager.close()

    by_pipeline = {
        pipeline_id: [item.frame_index for item in results if item.pipeline_id == pipeline_id]
        for pipeline_id in ("yolo", "dino")
    }
    assert by_pipeline == {"yolo": [0, 2], "dino": [0, 2]}


def test_slow_pipeline_replaces_stale_pending_frame() -> None:
    started = threading.Event()
    release = threading.Event()
    results: list[DetectionLayerObservation] = []
    detector = _BlockingDetector(started, release)
    manager = LiveDetectionManager(
        run_id="test",
        config=_config(
            PipelineSpec("slow", "grounding-dino", "model", ("door",), 0.3, 100.0)
        ),
        result_callback=results.append,
        detector_factory=lambda spec: detector,
        tracker_factory=lambda spec: NoopLayerTracker(),
    )
    manager.start()
    frame = np.zeros((8, 10, 3), dtype=np.uint8)
    manager.submit(FramePacket(0, 10.0, frame))
    assert started.wait(1.0)
    manager.submit(FramePacket(1, 10.02, frame))
    manager.submit(FramePacket(2, 10.04, frame))
    release.set()
    _wait_until(lambda: len(results) == 2)
    manager.close()

    assert [item.frame_index for item in results] == [0, 2]
    assert manager.snapshot()["slow"] == {
        "submitted_frames": 3,
        "completed_frames": 2,
        "dropped_frames": 1,
    }


def test_detection_artifact_and_manifest_are_finalized(tmp_path: Path) -> None:
    rerun_path = tmp_path / "rerun" / "review.rrd"
    recorder = ArtifactRecorder(
        tmp_path,
        run_id="door-test",
        capture_detections=True,
        rerun_path=rerun_path,
    )
    recorder.detection_layer(_layer_observation().to_dict())
    recorder.close()

    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    detection_record = manifest["artifacts"]["detections"][0]
    assert detection_record["status"] == "closed"
    assert Path(detection_record["path"]).read_text(encoding="utf-8").count("\n") == 1
    assert manifest["artifacts"]["rerun"][0]["status"] == "closed"


def test_disabled_detection_capture_does_not_create_detection_artifact(tmp_path: Path) -> None:
    recorder = ArtifactRecorder(tmp_path, run_id="no-door-diagnosis")
    recorder.close()

    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifacts"]["detections"] == []
    assert not (tmp_path / "detections").exists()


def test_live_rerun_publisher_serializes_frame_and_layer_events(tmp_path: Path) -> None:
    renderer = _FakeRenderer()
    publisher = LiveRerunPublisher(
        mode="file",
        recording_id="test",
        save_path=tmp_path / "review.rrd",
        renderer_factory=lambda **kwargs: renderer,
    )
    publisher.start()
    publisher.submit_frame(FramePacket(4, 10.0, np.zeros((8, 10, 3), dtype=np.uint8)))
    publisher.submit_detection_layer(_layer_observation())
    _wait_until(lambda: len(renderer.calls) == 2)
    stats = publisher.close()

    assert [item[0] for item in renderer.calls] == ["frame", "layer"]
    assert stats.rendered_events == 2
    assert renderer.closed is True


def test_rerun_renderer_uses_named_live_detector_and_tracker_entities() -> None:
    calls: list[tuple[str, object, object | None]] = []

    def archetype(name: str):
        return lambda *args, **kwargs: (name, args, kwargs)

    rr = SimpleNamespace(
        init=lambda *args, **kwargs: calls.append(("init", args, kwargs)),
        save=lambda path, **kwargs: calls.append(("save", path, kwargs)),
        set_time=lambda timeline, **kwargs: calls.append(("time", timeline, kwargs)),
        log=lambda entity, value, **kwargs: calls.append(("log", entity, (value, kwargs))),
        Boxes2D=archetype("Boxes2D"),
        Scalars=archetype("Scalars"),
        Clear=archetype("Clear"),
    )
    renderer = RerunVisionRenderer(
        save_path="review.rrd",
        recording_id="test",
        mode="live",
        rr_module=rr,
    )

    renderer.render_detection_layer(_layer_observation())

    entities = [str(call[1]) for call in calls if call[0] == "log"]
    assert "live/detectors/door_yolo_world/detections" in entities
    assert "live/trackers/door_yolo_world/tracks" in entities
    assert "live/diagnostics/inference_latency_ms/door_yolo_world" in entities
    assert "live/diagnostics/dropped_frames/door_yolo_world" in entities


class _FakeDetector:
    def __init__(self, label: str) -> None:
        self.label = label

    def detect(self, frame_bgr: np.ndarray) -> list[LayerDetection]:
        del frame_bgr
        return [LayerDetection(0, 0, self.label, 0.9, (1.0, 2.0, 8.0, 7.0))]


class _BlockingDetector:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release
        self.calls = 0

    def detect(self, frame_bgr: np.ndarray) -> list[LayerDetection]:
        del frame_bgr
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            assert self.release.wait(1.0)
        return []


class _FakeRenderer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.closed = False

    def render_frame(self, **kwargs: object) -> None:
        self.calls.append(("frame", kwargs))

    def render_detection_layer(self, observation: DetectionLayerObservation) -> None:
        self.calls.append(("layer", observation))

    def render(self, observation: object) -> None:
        self.calls.append(("visitor", observation))

    def close(self) -> None:
        self.closed = True


def _pipeline_payload(pipeline_id: str, detector: str) -> dict[str, object]:
    return {
        "id": pipeline_id,
        "detector": detector,
        "model": "model",
        "targets": ["door"],
        "threshold": 0.2,
        "inference_fps": 1.0,
        "tracker": "none",
        "role": "diagnosis",
    }


def _config(*specs: PipelineSpec) -> PipelineConfig:
    return PipelineConfig(Path("config.json"), "a" * 64, tuple(specs))


def _layer_observation() -> DetectionLayerObservation:
    return DetectionLayerObservation(
        run_id="test",
        pipeline_id="door_yolo_world",
        frame_index=4,
        frame_ts=10.0,
        completed_ts=10.02,
        inference_latency_ms=20.0,
        detector_config={"implementation": "yolo-world"},
        tracker_config={"implementation": "none"},
        detections=(LayerDetection(0, 0, "door", 0.9, (1.0, 2.0, 8.0, 7.0)),),
        submitted_frames=1,
        completed_frames=1,
    )


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for background worker")
