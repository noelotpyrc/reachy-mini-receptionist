#!/usr/bin/env python3
"""Run a PCM16 WAV through the deployed VAD and Parakeet STT handlers."""

from __future__ import annotations

import argparse
import json
import time
import wave
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any

import numpy as np
import soundfile as sf

from speech_to_speech.STT.parakeet_tdt_handler import ParakeetTDTSTTHandler
from speech_to_speech.VAD.vad_handler import VADHandler
from speech_to_speech.pipeline.messages import PIPELINE_END, PartialTranscription, Transcription, VADAudio
from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker


class CapturingQueue(Queue[Any]):
    def __init__(self, output_dir: Path) -> None:
        super().__init__()
        self.output_dir = output_dir
        self.final_audio: np.ndarray | None = None
        self.outputs: list[dict[str, Any]] = []
        self.stage = "target"

    def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
        if isinstance(item, VADAudio):
            audio = np.asarray(item.audio, dtype=np.float32).copy()
            self.outputs.append(
                {
                    "stage": self.stage,
                    "mode": item.mode,
                    "samples": int(audio.size),
                    "duration_s": audio.size / 16_000,
                    "turn_id": item.turn_id,
                    "turn_revision": item.turn_revision,
                }
            )
            if item.mode == "final" and self.stage == "target":
                self.final_audio = audio
                np.save(self.output_dir / "vad-final.npy", audio, allow_pickle=False)
                sf.write(self.output_dir / "vad-final.wav", audio, 16_000, subtype="PCM_16")
        super().put(item, block=block, timeout=timeout)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument(
        "--preceding-wav",
        type=Path,
        help="Previous WAV to process first on the same VAD/STT connection.",
    )
    parser.add_argument("--timeout-s", type=float, default=30.0)
    return parser.parse_args()


def load_pcm16_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
            raise ValueError(f"{path} must be mono, uncompressed PCM16 WAV")
        sample_rate = wav.getframerate()
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").copy()
    return audio, sample_rate


def stream_audio(
    audio: np.ndarray,
    sample_rate: int,
    frame_ms: int,
    rechunk_buffer: bytearray,
    vad_input: Queue[Any],
) -> None:
    frame_samples = round(sample_rate * frame_ms / 1000)
    for offset in range(0, audio.size, frame_samples):
        frame = audio[offset : offset + frame_samples]
        rechunk_buffer.extend(frame.astype("<i2", copy=False).tobytes())
        while len(rechunk_buffer) >= 512 * 2:
            vad_input.put(bytes(rechunk_buffer[: 512 * 2]))
            del rechunk_buffer[: 512 * 2]
        time.sleep(frame.size / sample_rate)


def collect_transcripts(
    stt_output: Queue[Any],
    transcript_events: list[dict[str, Any]],
    *,
    stage: str,
    timeout_s: float,
) -> Transcription | None:
    deadline = time.monotonic() + timeout_s
    quiet_deadline: float | None = None
    latest_final: Transcription | None = None
    while time.monotonic() < deadline:
        try:
            item = stt_output.get(timeout=0.1)
        except Empty:
            if quiet_deadline is not None and time.monotonic() >= quiet_deadline:
                return latest_final
            continue
        if isinstance(item, PartialTranscription):
            transcript_events.append({"stage": stage, "mode": "progressive", "text": item.text})
        elif isinstance(item, Transcription):
            latest_final = item
            transcript_events.append(
                {
                    "stage": stage,
                    "mode": "final",
                    "text": item.text,
                    "turn_id": item.turn_id,
                    "turn_revision": item.turn_revision,
                }
            )
            quiet_deadline = time.monotonic() + 0.75
    return latest_final


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    audio, sample_rate = load_pcm16_mono(args.input_wav)
    if sample_rate != 16_000:
        raise ValueError(f"expected 16000 Hz input, got {sample_rate}")
    rechunk_buffer = bytearray()
    preceding_audio: np.ndarray | None = None
    if args.preceding_wav is not None:
        preceding_audio, preceding_rate = load_pcm16_mono(args.preceding_wav)
        if preceding_rate != sample_rate:
            raise ValueError(f"preceding WAV sample rate is {preceding_rate}, expected {sample_rate}")

    stop_event = Event()
    should_listen = Event()
    should_listen.set()
    speculative_turns = SpeculativeTurnTracker()
    vad_input: Queue[Any] = Queue()
    vad_output = CapturingQueue(args.output_dir)
    stt_output: Queue[Any] = Queue()

    vad = VADHandler(
        stop_event,
        queue_in=vad_input,
        queue_out=vad_output,
        setup_args=(should_listen,),
        setup_kwargs={
            "thresh": 0.6,
            "sample_rate": 16_000,
            "min_silence_ms": 64,
            "min_speech_ms": 384,
            "min_speech_continuation_ms": 192,
            "max_speech_ms": float("inf"),
            "speech_pad_ms": 500,
            "audio_enhancement": False,
            "enable_realtime_transcription": True,
            "realtime_processing_pause": 0.5,
            "speculative_turns": speculative_turns,
        },
    )
    stt = ParakeetTDTSTTHandler(
        stop_event,
        queue_in=vad_output,
        queue_out=stt_output,
        setup_kwargs={
            "language": None,
            "enable_live_transcription": True,
            "live_transcription_update_interval": 0.5,
        },
    )
    stt.speculative_turns = speculative_turns
    threads = [
        Thread(target=vad.run, name="diagnostic-vad"),
        Thread(target=stt.run, name="diagnostic-stt"),
    ]
    for thread in threads:
        thread.start()

    started = time.monotonic()
    transcript_events: list[dict[str, Any]] = []
    if preceding_audio is not None:
        vad_output.stage = "preceding"
        stream_audio(preceding_audio, sample_rate, args.frame_ms, rechunk_buffer, vad_input)
        preceding_final = collect_transcripts(
            stt_output,
            transcript_events,
            stage="preceding",
            timeout_s=args.timeout_s,
        )
        if preceding_final is None:
            raise RuntimeError("preceding WAV did not produce a final transcript")
        speculative_turns.commit(preceding_final.turn_id, preceding_final.turn_revision)

    vad_output.stage = "target"
    stream_audio(audio, sample_rate, args.frame_ms, rechunk_buffer, vad_input)
    target_final = collect_transcripts(
        stt_output,
        transcript_events,
        stage="target",
        timeout_s=args.timeout_s,
    )
    final_transcript = None if target_final is None else target_final.text

    vad_input.put(PIPELINE_END)
    for thread in threads:
        thread.join(timeout=10)

    report = {
        "input_wav": str(args.input_wav),
        "input_samples": int(audio.size),
        "input_duration_s": audio.size / sample_rate,
        "frame_ms": args.frame_ms,
        "preceding_wav": None if args.preceding_wav is None else str(args.preceding_wav),
        "ending_remainder_samples": len(rechunk_buffer) // 2,
        "elapsed_s": time.monotonic() - started,
        "vad_outputs": vad_output.outputs,
        "final_vad_samples": None if vad_output.final_audio is None else int(vad_output.final_audio.size),
        "final_vad_duration_s": (
            None if vad_output.final_audio is None else vad_output.final_audio.size / sample_rate
        ),
        "transcripts": transcript_events,
        "final_transcript": final_transcript,
    }
    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if final_transcript is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
