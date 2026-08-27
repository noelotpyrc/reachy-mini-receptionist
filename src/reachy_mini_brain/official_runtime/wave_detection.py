"""Temporal wave detection from normalized hand-center observations."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class HandMotionWaveStatus:
    """One update of the temporal hand-motion detector."""

    detected: bool
    center_x: float
    samples: int
    direction_changes: int
    displacement: float
    reason: str


class HandMotionWaveDetector:
    """Detect one left-right-left hand movement within a bounded time window."""

    def __init__(
        self,
        *,
        history_size: int = 30,
        timeout_s: float = 2.0,
        min_samples: int = 3,
        smoothing_window: int = 3,
        min_displacement: float = 0.08,
        min_cycles: int = 1,
        direction_noise_floor: float = 0.01,
    ) -> None:
        if history_size < 1:
            raise ValueError("history_size must be positive")
        if timeout_s <= 0.0:
            raise ValueError("timeout_s must be positive")
        if min_samples < 1:
            raise ValueError("min_samples must be positive")
        if smoothing_window < 1:
            raise ValueError("smoothing_window must be positive")
        if min_displacement <= 0.0:
            raise ValueError("min_displacement must be positive")
        if min_cycles < 1:
            raise ValueError("min_cycles must be positive")
        if direction_noise_floor < 0.0:
            raise ValueError("direction_noise_floor cannot be negative")

        self.timeout_s = float(timeout_s)
        self.min_samples = int(min_samples)
        self.smoothing_window = int(smoothing_window)
        self.min_displacement = float(min_displacement)
        self.min_cycles = int(min_cycles)
        self.direction_noise_floor = float(direction_noise_floor)
        self._history: deque[tuple[float, float]] = deque(maxlen=history_size)

    def reset(self) -> None:
        self._history.clear()

    def update(self, *, timestamp_s: float, center_x: float) -> HandMotionWaveStatus:
        now = float(timestamp_s)
        x = float(center_x)
        self._history.append((now, x))
        pruned = False
        while self._history and now - self._history[0][0] > self.timeout_s:
            self._history.popleft()
            pruned = True

        sample_count = len(self._history)
        if sample_count < self.min_samples:
            if pruned:
                self.reset()
            return self._status(x, sample_count, reason="insufficient_samples")

        smoothed = self._moving_average([value for _, value in self._history])
        if len(smoothed) < 3:
            return self._status(x, sample_count, reason="insufficient_smoothed_samples")

        direction_changes = self._direction_changes(smoothed)
        displacement = max(abs(value - smoothed[0]) for value in smoothed)
        enough_changes = direction_changes >= self.min_cycles * 2
        enough_displacement = displacement >= self.min_displacement
        detected = enough_changes and enough_displacement
        if detected:
            reason = "wave"
        elif not enough_changes:
            reason = "insufficient_direction_changes"
        else:
            reason = "insufficient_displacement"

        status = HandMotionWaveStatus(
            detected=detected,
            center_x=x,
            samples=sample_count,
            direction_changes=direction_changes,
            displacement=displacement,
            reason=reason,
        )
        if detected:
            self.reset()
        return status

    def _status(self, center_x: float, samples: int, *, reason: str) -> HandMotionWaveStatus:
        return HandMotionWaveStatus(
            detected=False,
            center_x=center_x,
            samples=samples,
            direction_changes=0,
            displacement=0.0,
            reason=reason,
        )

    def _moving_average(self, values: list[float]) -> list[float]:
        window = self.smoothing_window
        if len(values) < window:
            return list(values)
        return [
            sum(values[index : index + window]) / window
            for index in range(len(values) - window + 1)
        ]

    def _direction_changes(self, values: list[float]) -> int:
        changes = 0
        last_direction: int | None = None
        for previous, current in zip(values, values[1:], strict=False):
            delta = current - previous
            if abs(delta) < self.direction_noise_floor:
                continue
            direction = 1 if delta > 0.0 else -1
            if last_direction is not None and direction != last_direction:
                changes += 1
            last_direction = direction
        return changes
