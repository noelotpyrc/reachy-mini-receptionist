"""Camera and head-tracking capability adapters for the official runtime."""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from .capabilities import CapabilityRegistry, RuntimeContext
from .events import RuntimeEvent


class CameraFrameProvider(Protocol):
    """Object that can return the latest BGR camera frame."""

    def get_latest_frame(self) -> NDArray[np.uint8] | None:
        """Return a BGR frame, or None when no frame is available."""


class LocalVisionProcessor(Protocol):
    """Optional local vision model used before falling back to backend image input."""

    def process_image(self, frame: NDArray[np.uint8], prompt: str) -> str:
        """Answer a question about a BGR frame."""


class HeadTrackingController(Protocol):
    """Object that can toggle head tracking in the camera/movement path."""

    def set_head_tracking_enabled(self, enabled: bool) -> None:
        """Enable or disable head tracking."""


async def camera_question(
    context: RuntimeContext,
    *,
    question: str,
    camera_worker: CameraFrameProvider | None = None,
    vision_processor: LocalVisionProcessor | None = None,
    jpeg_quality: int = 95,
) -> dict[str, Any]:
    """Official-style camera Q&A capability.

    Behavior mirrors the official app tool boundary:

    - read latest camera frame
    - if a local vision processor exists, answer locally
    - otherwise return base64 JPEG for a realtime backend/model to consume
    """

    prompt = question.strip()
    if not prompt:
        return {"error": "question must be a non-empty string"}

    frame_provider = camera_worker or _state_value(context, "camera_worker")
    if frame_provider is None:
        return {"error": "Camera worker not available"}

    get_latest_frame = getattr(frame_provider, "get_latest_frame", None)
    if not callable(get_latest_frame):
        return {"error": "Camera worker does not expose get_latest_frame"}

    frame = get_latest_frame()
    if frame is None:
        return {"error": "No frame available"}
    frame = np.asarray(frame)
    if frame.ndim != 3 or frame.shape[2] != 3:
        return {"error": "Camera frame must be HxWx3 BGR"}

    context.event_sink.emit(
        RuntimeEvent(
            kind="capability.camera_frame",
            source="camera",
            data={"shape": list(frame.shape), "dtype": str(frame.dtype), "question_chars": len(prompt)},
        )
    )

    processor = vision_processor or _state_value(context, "vision_processor")
    if processor is not None:
        process_image = getattr(processor, "process_image", None)
        if not callable(process_image):
            return {"error": "Vision processor does not expose process_image"}
        vision_result = await asyncio.to_thread(process_image, frame, prompt)
        return (
            {"image_description": vision_result}
            if isinstance(vision_result, str)
            else {"error": "vision returned non-string"}
        )

    jpeg_bytes = encode_bgr_frame_as_jpeg(frame, quality=jpeg_quality)
    return {
        "b64_im": base64.b64encode(jpeg_bytes).decode("utf-8"),
        "mime_type": "image/jpeg",
        "question": prompt,
    }


async def set_head_tracking(
    context: RuntimeContext,
    *,
    start: bool,
    camera_worker: HeadTrackingController | None = None,
) -> dict[str, str]:
    """Official-style head-tracking toggle capability."""

    controller = camera_worker or _state_value(context, "camera_worker")
    if controller is None:
        return {"error": "Camera worker not available"}

    set_enabled = getattr(controller, "set_head_tracking_enabled", None)
    if not callable(set_enabled):
        return {"error": "Camera worker does not expose set_head_tracking_enabled"}

    enabled = bool(start)
    set_enabled(enabled)
    context.event_sink.emit(
        RuntimeEvent(
            kind="capability.head_tracking",
            source="head_tracking",
            data={"enabled": enabled},
        )
    )
    status = "started" if enabled else "stopped"
    return {"status": f"head tracking {status}"}


def register_camera_capabilities(registry: CapabilityRegistry) -> None:
    """Register official-style camera and head-tracking capabilities."""

    registry.register("camera", camera_question)
    registry.register("head_tracking", set_head_tracking)


def encode_bgr_frame_as_jpeg(frame: NDArray[np.uint8], *, quality: int = 95) -> bytes:
    """Encode a BGR camera frame as JPEG bytes without depending on PyAV."""

    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python is required to encode camera frames") from exc

    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("frame must be HxWx3 BGR")

    ok, encoded = cv2.imencode(".jpg", np.ascontiguousarray(frame), [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("Failed to encode frame as JPEG")
    return bytes(encoded)


def _state_value(context: RuntimeContext, key: str) -> Any:
    return getattr(context, "state", {}).get(key)
