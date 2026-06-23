"""Legacy person detector — Tier-1 of the old reception vision pipeline.

Status: legacy/fallback. The accepted product path uses
``reachy_mini_brain.official_runtime.perception``. Keep this module runnable for
regression/reference until legacy removal is explicitly approved.

Wraps RF-DETR Nano for fast, local person detection on camera frames.
STATELESS per frame: image in -> person boxes out. Tracking (stable IDs across
frames) and approach logic (box-growth / desk-zone / dwell) are separate layers
built on top of this — they live elsewhere, not here.

RF-DETR is COCO-trained; the `person` class id is 1, which is all we keep.
Returns a supervision `Detections` so it feeds straight into a tracker.
"""

from __future__ import annotations

import numpy as np


class PersonDetector:
    """RF-DETR Nano wrapped for person-only detection."""

    PERSON_CLASS_ID = 1  # COCO

    def __init__(self, threshold: float = 0.5, optimize: bool = True):
        from rfdetr import RFDETRNano

        self.threshold = threshold
        self._model = RFDETRNano()
        if optimize:
            try:
                self._model.optimize_for_inference()
            except Exception:
                pass  # not fatal — just slower inference

    def detect(self, image, *, bgr: bool = False):
        """Detect people in one frame.

        image: PIL.Image or HxWx3 numpy array. RGB by default; pass ``bgr=True``
               for OpenCV / robot frames (``session.get_frame()`` returns BGR).
        Returns a supervision ``Detections`` containing only persons.
        """
        if isinstance(image, np.ndarray) and bgr:
            image = np.ascontiguousarray(image[:, :, ::-1])  # BGR -> RGB

        det = self._model.predict(image, threshold=self.threshold)
        return det[det.class_id == self.PERSON_CLASS_ID]
