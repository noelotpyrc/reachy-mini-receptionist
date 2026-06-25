"""Legacy offline audio review utility for reception-daemon runs.

Status: legacy/fallback. The accepted product path records official-runtime
artifacts under ``artifacts/official-runtime-live``. Keep this module runnable
for old-run review until legacy removal is explicitly approved.

Generates human-review clips and an index from one recorded run. The raw run
copy stays immutable under artifacts/remote-runs/<run_id>/; derived review
clips/reports go under artifacts/reviews/<run_id>/.
"""

from __future__ import annotations

import csv
import json
import math
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import click
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = PROJECT_ROOT / "artifacts"
DEFAULT_REMOTE = "leon@100.127.86.67"
DEFAULT_REMOTE_ARTIFACTS = "/Users/leon/projects/reachy_mini_receptionist_clean/artifacts"
SAMPLE_RATE = 16000


@dataclass
class RunFiles:
    run_dir: Path
    manifest: Path
    log: Path
    raw_wav: Path
    sidecar: Path
    turns_jsonl: Path
    turn_wavs: dict[int, Path]


@dataclass
class TurnReview:
    run_id: str
    turn: int
    flags: list[str]
    score: float
    method: str
    heard: str
    reply: str
    turn_wav: str
    clip: str | None
    context_clip: str | None
    wide_clip: str | None
    match_start_clock: str
    match_end_clock: str
    wav_duration_s: float
    wall_duration_s: float
    heard_delay_s: float | None
    reply_delay_s: float | None
    json_after_heard_s: float | None
    json_after_match_end_s: float
    robot_speaking_overlap_pct: float
    rms: float
    peak_abs: float
    leading_low_s: float
    trailing_low_s: float


def _need_audio_deps():
    try:
        import soundfile as sf
        from scipy import signal
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(
            "review-audio needs the audio extra: .venv/bin/python -m pip install -e '.[audio]'"
        ) from e
    return sf, signal


def _remote_path(remote: str, remote_artifacts: str, rel: str) -> str:
    return f"{remote}:{remote_artifacts.rstrip('/')}/{rel}"


def sync_run(
    run_id: str,
    *,
    remote: str,
    remote_artifacts: str,
    local_root: Path,
    include_video: bool,
) -> Path:
    """Rsync the run-specific artifacts into a flat local run directory."""
    run_dir = local_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    rels = [
        f"runs/run-{run_id}.json",
        f"logs/reception-{run_id}.log",
        f"audio-{run_id}-*.wav",
        f"audio-{run_id}-*.jsonl",
        f"capture-{run_id}-*.jsonl",
        f"turns/turns-{run_id}.jsonl",
        f"turns/turn-{run_id}-*.wav",
        "events.jsonl",
    ]
    if include_video:
        rels.append(f"video-{run_id}-*.mkv")
    cmd = [
        "rsync",
        "-av",
        "-e",
        "ssh -o ConnectTimeout=8",
        *[_remote_path(remote, remote_artifacts, rel) for rel in rels],
        str(run_dir) + "/",
    ]
    subprocess.run(cmd, check=True)
    return run_dir


def _one(globbed: list[Path], label: str) -> Path:
    if not globbed:
        raise click.ClickException(f"missing {label}")
    if len(globbed) > 1:
        names = ", ".join(p.name for p in globbed)
        raise click.ClickException(f"expected one {label}, found: {names}")
    return globbed[0]


def find_run_files(run_dir: Path, run_id: str) -> RunFiles:
    manifest = _one(sorted(run_dir.glob(f"run-{run_id}.json")), "run manifest")
    log = _one(sorted(run_dir.glob(f"reception-{run_id}.log")), "durable log")
    raw_wav = _one(sorted(run_dir.glob(f"audio-{run_id}-*.wav")), "raw audio WAV")
    sidecar = raw_wav.with_suffix(".jsonl")
    if not sidecar.exists():
        sidecar = _one(sorted(run_dir.glob(f"audio-{run_id}-*.jsonl")), "raw audio sidecar")
    turns_jsonl = _one(sorted(run_dir.glob(f"turns-{run_id}.jsonl")), "turns JSONL")
    turn_wavs: dict[int, Path] = {}
    pat = re.compile(rf"^turn-{re.escape(run_id)}-(\d{{3}})\.wav$")
    for wav in sorted(run_dir.glob(f"turn-{run_id}-*.wav")):
        m = pat.match(wav.name)
        if m:
            turn_wavs[int(m.group(1))] = wav
    if not turn_wavs:
        raise click.ClickException("missing per-turn WAV files")
    return RunFiles(run_dir, manifest, log, raw_wav, sidecar, turns_jsonl, turn_wavs)


def _clock_seconds(hms: str) -> int:
    h, m, s = map(int, hms.split(":"))
    return h * 3600 + m * 60 + s


def _epoch_to_clock(ts: float, *, anchor_clock: int, anchor_epoch: float) -> str:
    sec = (anchor_clock + (ts - anchor_epoch)) % (24 * 3600)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def _log_hms_to_epoch(hms: str, *, anchor_clock: int, anchor_epoch: float) -> float:
    clock = _clock_seconds(hms)
    delta = clock - anchor_clock
    if delta < -12 * 3600:
        delta += 24 * 3600
    elif delta > 12 * 3600:
        delta -= 24 * 3600
    return anchor_epoch + delta


def load_sidecar(path: Path) -> tuple[dict, list[dict], dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    starts = [r for r in rows if r.get("type") == "start"]
    stops = [r for r in rows if r.get("type") == "stop"]
    chunks = [r for r in rows if r.get("type") == "chunk"]
    if len(starts) != 1 or len(stops) != 1 or not chunks:
        raise click.ClickException(
            f"bad sidecar shape: starts={len(starts)} chunks={len(chunks)} stops={len(stops)}"
        )
    return starts[0], chunks, stops[0]


def load_log_times(log_path: Path, *, anchor_clock: int, anchor_epoch: float) -> tuple[list[float], list[float]]:
    heard: list[float] = []
    replies: list[float] = []
    for line in log_path.read_text().splitlines():
        if not re.match(r"^\d\d:\d\d:\d\d ", line):
            continue
        ts = _log_hms_to_epoch(line[:8], anchor_clock=anchor_clock, anchor_epoch=anchor_epoch)
        if "voice: heard " in line:
            heard.append(ts)
        elif "voice: reply:" in line:
            replies.append(ts)
    return heard, replies


def _sample_for_epoch(ts: float, chunk_ts: np.ndarray, chunk_samples: np.ndarray) -> int:
    idx = int(np.searchsorted(chunk_ts, ts, side="left"))
    idx = min(len(chunk_samples) - 1, max(0, idx))
    return int(chunk_samples[idx])


def _norm_xcorr_valid(signal_mod, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    if len(y) > len(x):
        return np.array([])
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    x = x - x.mean()
    y = y - y.mean()
    yn = math.sqrt(float(np.sum(y * y))) + 1e-12
    corr = signal_mod.correlate(x, y, mode="valid", method="fft")
    denom = np.sqrt(
        signal_mod.correlate(x * x, np.ones(len(y)), mode="valid", method="fft")
    ) * yn + 1e-12
    return corr / denom


def _best_match(
    signal_mod,
    raw: np.ndarray,
    turn_audio: np.ndarray,
    *,
    sample_start: int,
    sample_end: int,
) -> tuple[float, int]:
    sample_start = max(0, min(sample_start, max(0, len(raw) - len(turn_audio))))
    sample_end = min(len(raw), max(sample_end, sample_start + len(turn_audio)))
    scores = _norm_xcorr_valid(signal_mod, raw[sample_start:sample_end], turn_audio)
    if len(scores) == 0:
        return float("nan"), sample_start
    local = int(np.argmax(scores))
    return float(scores[local]), sample_start + local


def _audio_stats(audio: np.ndarray, *, silence_rms: float) -> tuple[float, float, float, float]:
    if len(audio) == 0:
        return 0.0, 0.0, 0.0, 0.0
    audio = np.asarray(audio, dtype=np.float64)
    rms = math.sqrt(float(np.mean(audio * audio)))
    peak = float(np.max(np.abs(audio)))
    win = 320
    frames = len(audio) // win
    if frames == 0:
        low = rms < silence_rms
        dur = len(audio) / SAMPLE_RATE if low else 0.0
        return rms, peak, dur, dur
    env = np.sqrt(np.mean(audio[: frames * win].reshape(frames, win) ** 2, axis=1))
    leading = 0
    for v in env:
        if v >= silence_rms:
            break
        leading += 1
    trailing = 0
    for v in env[::-1]:
        if v >= silence_rms:
            break
        trailing += 1
    return rms, peak, leading * win / SAMPLE_RATE, trailing * win / SAMPLE_RATE


def _flags(
    *,
    score: float,
    match_threshold: float,
    heard_delay: float | None,
    delay_threshold: float,
    json_after_end: float,
    action_delay_threshold: float,
    robot_overlap: float,
    peak: float,
    leading_low: float,
    trailing_low: float,
) -> list[str]:
    out: list[str] = []
    if score < match_threshold:
        out.append("low-match")
    if heard_delay is not None and heard_delay > delay_threshold:
        out.append("queued-stale")
    if json_after_end > action_delay_threshold:
        out.append("late-action")
    if robot_overlap > 0:
        out.append("robot-speaking-overlap")
    if peak >= 0.99:
        out.append("hot-or-clipped")
    if leading_low > 0.4:
        out.append("long-leading-silence")
    if trailing_low > 0.6:
        out.append("long-trailing-silence")
    return out


def _selected(flags: list[str], mode: str) -> bool:
    if mode == "all":
        return True
    if mode == "none":
        return False
    if mode == "delayed":
        return "queued-stale" in flags or "late-action" in flags
    return bool(flags)


def _write_clip(sf, path: Path, raw: np.ndarray, start: int, end: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    start = max(0, min(start, len(raw)))
    end = max(start, min(end, len(raw)))
    sf.write(str(path), raw[start:end].astype(np.float32), SAMPLE_RATE)


def build_review(
    run_id: str,
    files: RunFiles,
    *,
    review_root: Path,
    clip_mode: str,
    pre: float,
    post: float,
    wide_pre: float,
    wide_post: float,
    search_back: float,
    search_forward: float,
    delay_threshold: float,
    action_delay_threshold: float,
    match_threshold: float,
    silence_rms: float,
) -> tuple[Path, list[TurnReview]]:
    sf, signal_mod = _need_audio_deps()

    review_dir = review_root / run_id
    clips_dir = review_dir / "clips"
    review_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)

    start_row, chunks, _stop_row = load_sidecar(files.sidecar)
    chunk_ts = np.array([float(c["ts"]) for c in chunks])
    chunk_samples = np.array([int(c["sample_start"]) for c in chunks])
    chunk_speaking = np.array([bool(c.get("speaking")) for c in chunks])

    log_lines = files.log.read_text().splitlines()
    start_log = next((line for line in log_lines if "audio_record: started" in line), None)
    if not start_log:
        raise click.ClickException("log does not contain audio_record: started")
    anchor_clock = _clock_seconds(start_log[:8])
    anchor_epoch = float(start_row["ts"])
    heard_times, reply_times = load_log_times(
        files.log, anchor_clock=anchor_clock, anchor_epoch=anchor_epoch
    )

    raw, raw_sr = sf.read(str(files.raw_wav), dtype="float32", always_2d=False)
    if raw_sr != SAMPLE_RATE:
        raise click.ClickException(f"expected {SAMPLE_RATE} Hz raw WAV, got {raw_sr}")
    raw = np.asarray(raw, dtype=np.float32).reshape(-1)
    turns = [json.loads(line) for line in files.turns_jsonl.read_text().splitlines() if line.strip()]

    reviews: list[TurnReview] = []
    for turn in turns:
        n = int(turn["n"])
        turn_wav = files.turn_wavs.get(n)
        if not turn_wav:
            raise click.ClickException(f"missing turn WAV for turn {n}")
        audio, turn_sr = sf.read(str(turn_wav), dtype="float32", always_2d=False)
        if turn_sr != SAMPLE_RATE:
            raise click.ClickException(f"expected {SAMPLE_RATE} Hz for {turn_wav}, got {turn_sr}")
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)

        heard_epoch = heard_times[n - 1] if n - 1 < len(heard_times) else None
        reply_epoch = reply_times[n - 1] if n - 1 < len(reply_times) else None
        if heard_epoch is not None:
            search_start = _sample_for_epoch(heard_epoch - search_back, chunk_ts, chunk_samples)
            search_end = _sample_for_epoch(heard_epoch + search_forward, chunk_ts, chunk_samples) + len(audio)
            method = "near-log"
        else:
            search_start = 0
            search_end = len(raw)
            method = "global"
        score, match_start = _best_match(
            signal_mod,
            raw,
            audio,
            sample_start=search_start,
            sample_end=search_end,
        )
        if score < match_threshold and method != "global":
            global_score, global_start = _best_match(
                signal_mod, raw, audio, sample_start=0, sample_end=len(raw)
            )
            if global_score > score:
                score, match_start, method = global_score, global_start, "global"

        match_end = match_start + len(audio)
        chunk_start_idx = min(len(chunks) - 1, max(0, match_start // 320))
        chunk_end_idx = min(len(chunks) - 1, max(0, (match_end - 1) // 320))
        match_start_epoch = float(chunk_ts[chunk_start_idx])
        match_end_epoch = float(chunk_ts[chunk_end_idx])
        robot_overlap = float(np.mean(chunk_speaking[chunk_start_idx : chunk_end_idx + 1]) * 100)
        heard_delay = None if heard_epoch is None else heard_epoch - match_end_epoch
        reply_delay = None if heard_epoch is None or reply_epoch is None else reply_epoch - heard_epoch
        json_epoch = float(turn["ts"])
        json_after_heard = None if heard_epoch is None else json_epoch - heard_epoch
        json_after_end = json_epoch - match_end_epoch
        rms, peak, leading_low, trailing_low = _audio_stats(audio, silence_rms=silence_rms)
        flags = _flags(
            score=score,
            match_threshold=match_threshold,
            heard_delay=heard_delay,
            delay_threshold=delay_threshold,
            json_after_end=json_after_end,
            action_delay_threshold=action_delay_threshold,
            robot_overlap=robot_overlap,
            peak=peak,
            leading_low=leading_low,
            trailing_low=trailing_low,
        )

        clip_rel = context_rel = wide_rel = None
        if _selected(flags, clip_mode):
            clip_name = f"turn-{n:03d}.wav"
            context_name = f"turn-{n:03d}-context.wav"
            wide_name = f"turn-{n:03d}-wide.wav"
            shutil.copy2(turn_wav, clips_dir / clip_name)
            _write_clip(
                sf,
                clips_dir / context_name,
                raw,
                match_start - int(pre * SAMPLE_RATE),
                match_end + int(post * SAMPLE_RATE),
            )
            _write_clip(
                sf,
                clips_dir / wide_name,
                raw,
                match_start - int(wide_pre * SAMPLE_RATE),
                match_end + int(wide_post * SAMPLE_RATE),
            )
            clip_rel = f"clips/{clip_name}"
            context_rel = f"clips/{context_name}"
            wide_rel = f"clips/{wide_name}"

        reviews.append(
            TurnReview(
                run_id=run_id,
                turn=n,
                flags=flags,
                score=round(float(score), 6),
                method=method,
                heard=turn.get("heard", ""),
                reply=turn.get("reply", ""),
                turn_wav=turn_wav.name,
                clip=clip_rel,
                context_clip=context_rel,
                wide_clip=wide_rel,
                match_start_clock=_epoch_to_clock(
                    match_start_epoch, anchor_clock=anchor_clock, anchor_epoch=anchor_epoch
                ),
                match_end_clock=_epoch_to_clock(
                    match_end_epoch, anchor_clock=anchor_clock, anchor_epoch=anchor_epoch
                ),
                wav_duration_s=round(len(audio) / SAMPLE_RATE, 3),
                wall_duration_s=round(match_end_epoch - match_start_epoch, 3),
                heard_delay_s=None if heard_delay is None else round(heard_delay, 3),
                reply_delay_s=None if reply_delay is None else round(reply_delay, 3),
                json_after_heard_s=None if json_after_heard is None else round(json_after_heard, 3),
                json_after_match_end_s=round(json_after_end, 3),
                robot_speaking_overlap_pct=round(robot_overlap, 3),
                rms=round(rms, 6),
                peak_abs=round(peak, 6),
                leading_low_s=round(leading_low, 3),
                trailing_low_s=round(trailing_low, 3),
            )
        )

    _write_indexes(review_dir, reviews, clip_mode=clip_mode)
    return review_dir, reviews


def _write_indexes(review_dir: Path, reviews: list[TurnReview], *, clip_mode: str) -> None:
    rows = [asdict(r) | {"flags": ",".join(r.flags)} for r in reviews]
    csv_path = review_dir / "review.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)

    json_path = review_dir / "review.json"
    json_path.write_text(json.dumps([asdict(r) for r in reviews], indent=2) + "\n", encoding="utf-8")

    md = [
        f"# Audio Review {reviews[0].run_id if reviews else ''}",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Clip mode: `{clip_mode}`",
        "",
        "| Turn | Flags | Heard Delay | Action Delay | Clip | Context | Wide | Heard |",
        "|---:|---|---:|---:|---|---|---|---|",
    ]
    for r in reviews:
        flags = ", ".join(r.flags) if r.flags else ""
        clip = f"[clip]({r.clip})" if r.clip else ""
        context = f"[context]({r.context_clip})" if r.context_clip else ""
        wide = f"[wide]({r.wide_clip})" if r.wide_clip else ""
        heard_delay = "" if r.heard_delay_s is None else f"{r.heard_delay_s:.1f}s"
        action_delay = f"{r.json_after_match_end_s:.1f}s"
        heard = r.heard.replace("|", "\\|")
        md.append(
            f"| {r.turn} | {flags} | {heard_delay} | {action_delay} | "
            f"{clip} | {context} | {wide} | {heard} |"
        )
    (review_dir / "review.md").write_text("\n".join(md) + "\n", encoding="utf-8")


@click.command(context_settings={"show_default": True})
@click.argument("run_id")
@click.option("--sync/--no-sync", "do_sync", default=False, help="Rsync the run from m1max first.")
@click.option("--remote", default=DEFAULT_REMOTE, help="Remote SSH target for --sync.")
@click.option("--remote-artifacts", default=DEFAULT_REMOTE_ARTIFACTS, help="Remote artifacts dir.")
@click.option("--include-video/--no-include-video", default=False, help="Also sync the video file.")
@click.option(
    "--local-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=ARTIFACTS / "remote-runs",
    help="Local root containing synced run dirs.",
)
@click.option(
    "--review-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=ARTIFACTS / "reviews",
    help="Where to write derived review outputs.",
)
@click.option(
    "--clips",
    type=click.Choice(["flagged", "delayed", "all", "none"]),
    default="flagged",
    help="Which turns should get copied/generated clips.",
)
@click.option("--pre", default=2.0, type=float, help="Seconds before turn in context clip.")
@click.option("--post", default=2.0, type=float, help="Seconds after turn in context clip.")
@click.option("--wide-pre", default=8.0, type=float, help="Seconds before turn in wide clip.")
@click.option("--wide-post", default=8.0, type=float, help="Seconds after turn in wide clip.")
@click.option("--search-back", default=120.0, type=float, help="Seconds before voice log to search.")
@click.option("--search-forward", default=5.0, type=float, help="Seconds after voice log to search.")
@click.option("--delay-threshold", default=10.0, type=float, help="Flag heard-log delay above this.")
@click.option("--action-delay-threshold", default=25.0, type=float, help="Flag action delay above this.")
@click.option("--match-threshold", default=0.75, type=float, help="Flag match scores below this.")
@click.option("--silence-rms", default=0.005, type=float, help="RMS threshold for silence flags.")
def cli(
    run_id,
    do_sync,
    remote,
    remote_artifacts,
    include_video,
    local_root,
    review_root,
    clips,
    pre,
    post,
    wide_pre,
    wide_post,
    search_back,
    search_forward,
    delay_threshold,
    action_delay_threshold,
    match_threshold,
    silence_rms,
):
    """Generate audio-review clips and indexes for a reception run."""
    if do_sync:
        click.echo(f"syncing {run_id} from {remote}:{remote_artifacts}")
        run_dir = sync_run(
            run_id,
            remote=remote,
            remote_artifacts=remote_artifacts,
            local_root=local_root,
            include_video=include_video,
        )
    else:
        run_dir = local_root / run_id

    files = find_run_files(run_dir, run_id)
    review_dir, reviews = build_review(
        run_id,
        files,
        review_root=review_root,
        clip_mode=clips,
        pre=pre,
        post=post,
        wide_pre=wide_pre,
        wide_post=wide_post,
        search_back=search_back,
        search_forward=search_forward,
        delay_threshold=delay_threshold,
        action_delay_threshold=action_delay_threshold,
        match_threshold=match_threshold,
        silence_rms=silence_rms,
    )
    flagged = [r for r in reviews if r.flags]
    clipped = [r for r in reviews if r.clip]
    click.echo(f"review -> {review_dir}")
    click.echo(f"turns={len(reviews)} flagged={len(flagged)} clips={len(clipped)}")
    if flagged:
        summary = ", ".join(f"{r.turn}:{'/'.join(r.flags)}" for r in flagged)
        click.echo(f"flagged: {summary}")


if __name__ == "__main__":
    cli()
