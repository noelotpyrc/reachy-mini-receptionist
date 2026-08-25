"""Source-frame-aligned live fusion for door policy observations."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from .door_observation import (
    DoorDetectionInput,
    DoorFrameObservation,
    DoorMotionObserver,
    DoorObserverSettings,
    PersonBoxInput,
)
from .door_policy import DoorPolicyFrameObservation, DoorPolicySettings, DoorPolicyTriggerEngine
from .live_detection import DetectionLayerObservation, FramePacket
from .vision_observation import VisionObservation


DoorPolicyResultCallback = Callable[[DoorFrameObservation, DoorPolicyFrameObservation], None]
DoorPolicyHealthCallback = Callable[[str, dict[str, object]], None]


@dataclass(frozen=True)
class _BufferedFrame:
    packet: FramePacket
    people: tuple[PersonBoxInput, ...]
    occluders: tuple[PersonBoxInput, ...]


class LiveDoorPolicyCoordinator:
    """Fuse delayed policy-role detections with their original person observations."""

    def __init__(
        self,
        *,
        result_callback: DoorPolicyResultCallback,
        health_callback: DoorPolicyHealthCallback | None = None,
        observer_settings: DoorObserverSettings | None = None,
        policy_settings: DoorPolicySettings | None = None,
        max_buffered_frames: int = 50,
    ) -> None:
        if max_buffered_frames < 2:
            raise ValueError("max_buffered_frames must be at least 2")
        self.result_callback = result_callback
        self.health_callback = health_callback
        self.max_buffered_frames = max_buffered_frames
        self._observer = DoorMotionObserver(observer_settings)
        self._policy = DoorPolicyTriggerEngine(policy_settings)
        self._frames: OrderedDict[int, _BufferedFrame] = OrderedDict()
        self._last_processed_frame = -1
        self._lock = threading.RLock()

    def submit_frame(self, packet: FramePacket, people: VisionObservation | None) -> None:
        person_inputs = tuple(
            PersonBoxInput(track_id=track.logical_track_id, box=track.box)
            for track in (people.tracks if people is not None else ())
        )
        raw_person_inputs = tuple(
            PersonBoxInput(track_id=f"raw-{item.detection_index}", box=item.box)
            for item in (getattr(people, "detections", ()) if people is not None else ())
        )
        with self._lock:
            self._frames[packet.frame_index] = _BufferedFrame(
                packet=packet,
                people=person_inputs,
                occluders=raw_person_inputs or person_inputs,
            )
            while len(self._frames) > self.max_buffered_frames:
                frame_index, _ = self._frames.popitem(last=False)
                self._health("source_frame_dropped", frame_index=frame_index, reason="buffer_limit")

    def submit_detection(self, detection: DetectionLayerObservation) -> None:
        if detection.detector_config.get("role") != "policy":
            return
        results: list[tuple[DoorFrameObservation, DoorPolicyFrameObservation]] = []
        with self._lock:
            if detection.frame_index <= self._last_processed_frame:
                self._health(
                    "stale_detection_dropped",
                    frame_index=detection.frame_index,
                    last_processed_frame=self._last_processed_frame,
                )
                return
            target = self._frames.get(detection.frame_index)
            if target is None:
                self._health("source_frame_missing", frame_index=detection.frame_index)
                return
            frame_indices = [
                frame_index
                for frame_index in self._frames
                if self._last_processed_frame < frame_index <= detection.frame_index
            ]
            for frame_index in frame_indices:
                buffered = self._frames.pop(frame_index)
                semantic = frame_index == detection.frame_index
                door_observation = self._observer.update(
                    frame_index=frame_index,
                    frame_ts=buffered.packet.frame_ts,
                    frame_bgr=buffered.packet.frame_bgr,
                    door_detections=(
                        [
                            DoorDetectionInput(confidence=item.confidence, box=item.box)
                            for item in detection.detections
                        ]
                        if semantic
                        else None
                    ),
                    people=list(buffered.people),
                    occluders=list(buffered.occluders),
                    semantic_completed_ts=detection.completed_ts if semantic else None,
                    semantic_inference_latency_ms=(
                        detection.inference_latency_ms if semantic else None
                    ),
                )
                policy_observation = self._policy.update(
                    door_observation,
                    decision_ts=detection.completed_ts,
                )
                self._last_processed_frame = frame_index
                results.append((door_observation, policy_observation))
        for door_observation, policy_observation in results:
            self.result_callback(door_observation, policy_observation)

    def _health(self, event: str, **data: object) -> None:
        if self.health_callback is not None:
            self.health_callback(event, data)
