"""Legacy wave / hand-gesture detection for the old reception daemon.

Status: legacy/fallback. The accepted product path uses
``reachy_mini_brain.official_runtime.perception``. Keep this module runnable for
regression/reference until legacy removal is explicitly approved.

Uses MediaPipe's pretrained Gesture Recognizer (Open_Palm, Thumb_Up, Thumb_Down,
Pointing_Up, Closed_Fist, Victory, ILoveYou) — no hand-landmark math, same approach
as Pollen's Greetings app. Stateless per-frame `recognize()`; the caller debounces a
sustained gesture into a single event.
"""

from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path

os.environ.setdefault("GLOG_minloglevel", "2")  # quiet MediaPipe/glog init spam

log = logging.getLogger("gesture")

_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
              "gesture_recognizer/float16/1/gesture_recognizer.task")
_MODEL_PATH = Path.home() / ".cache" / "reachy_mini" / "gesture_recognizer.task"


def _ensure_model() -> str:
    if not _MODEL_PATH.exists():
        _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        log.info("gesture: downloading model -> %s", _MODEL_PATH)
        urllib.request.urlretrieve(_MODEL_URL, _MODEL_PATH)
    return str(_MODEL_PATH)


class GestureDetector:
    """Per-frame gesture recognition. `detect(bgr_frame) -> (name, score) | None`."""

    def __init__(self, gestures: tuple[str, ...] = ("Open_Palm",), threshold: float = 0.5):
        import mediapipe as mp

        self._mp = mp
        self.gestures = set(gestures)
        self.threshold = threshold
        opts = mp.tasks.vision.GestureRecognizerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=_ensure_model()),
            running_mode=mp.tasks.vision.RunningMode.IMAGE,
        )
        self._rec = mp.tasks.vision.GestureRecognizer.create_from_options(opts)
        log.info("gesture: recognizer ready (gestures=%s, threshold=%.2f)",
                 sorted(self.gestures), self.threshold)

    def detect(self, frame_bgr):
        """One BGR frame -> (gesture_name, score) if a target gesture scores >= threshold, else None."""
        import cv2

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        res = self._rec.recognize(img)
        if not res.gestures:
            return None
        top = res.gestures[0][0]  # highest-score gesture of the first detected hand
        if top.category_name in self.gestures and top.score >= self.threshold:
            return (top.category_name, float(top.score))
        return None
