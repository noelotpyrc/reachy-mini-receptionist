"""Process-wide serialization for models sharing Apple's MPS command queue."""

from __future__ import annotations

import threading
from contextlib import nullcontext
from typing import ContextManager


_MPS_INFERENCE_LOCK = threading.Lock()


def inference_guard(device: str) -> ContextManager[None]:
    if device.casefold() == "mps":
        return _MPS_INFERENCE_LOCK
    return nullcontext()
