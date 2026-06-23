"""Legacy separate speech-to-text worker process for live voice ingestion.

Status: legacy/fallback. The accepted product path uses the m1max S2S backend
instead of this old-daemon STT worker. Keep this module runnable for
regression/reference until legacy removal is explicitly approved.

The media/VAD thread should stay lightweight: it captures audio, endpoints speech,
and queues utterance records. This process does the expensive Whisper work and emits
timestamped transcript records back to the caller.
"""

from __future__ import annotations

import os
import time

import numpy as np


def stt_worker_main(utterance_q, transcript_q, model: str, language: str) -> None:
    """Process queued utterances into transcript records until a ``None`` sentinel."""
    try:
        os.nice(5)
    except OSError:
        pass

    # Keep faster-whisper/CTranslate2 from taking all cores by default. The worker is
    # isolated from the media thread, but it still shares the machine's CPU scheduler.
    os.environ.setdefault("REACHY_STT_CPU_THREADS", "2")
    os.environ.setdefault("OMP_NUM_THREADS", os.environ["REACHY_STT_CPU_THREADS"])
    os.environ.setdefault("CT2_NUM_THREADS", os.environ["REACHY_STT_CPU_THREADS"])

    from reachy_mini_brain import stt

    lang = None if language == "auto" else language
    while True:
        item = utterance_q.get()
        if item is None:
            return

        rec = dict(item)
        audio = np.asarray(rec.get("audio", np.zeros(0, dtype=np.float32)), dtype=np.float32)
        if audio.ndim > 1:
            audio = audio[:, 0]

        stt_start_ts = time.time()
        error = None
        text = ""
        try:
            text = stt.transcribe_array(
                audio, sample_rate=16000, model_size=model, language=lang,
            )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        stt_done_ts = time.time()

        rec.update({
            "audio": audio,
            "text": text,
            "model": model,
            "language": language,
            "stt_start_ts": round(stt_start_ts, 3),
            "stt_done_ts": round(stt_done_ts, 3),
            "stt_latency": round(stt_done_ts - stt_start_ts, 3),
        })
        if error:
            rec["error"] = error
        transcript_q.put(rec)
