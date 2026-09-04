# S2S Backend Event Trace Specification

## Status

The first pass is implemented on pinned S2S migration commit `2e4449c`. It
remains opt-in and has not been promoted to production. The trace is diagnostic
evidence only: it must not participate in pipeline decisions or change
conversation behavior.

## Goals

The backend trace must make one response reconstructable across:

```text
session -> VAD -> STT -> LLM provider -> LLM sentence chunks
        -> assistant transcript -> TTS input/coalescing -> Qwen
        -> backend transport -> client/robot sink
```

In particular, a retained trace must answer:

- which turn revision and cancellation generation produced each response;
- what text crossed the provider, sentence-batching, transcript, and TTS
  boundaries;
- why work completed, failed, was cancelled, or was discarded as stale;
- what Qwen configuration was used and how much PCM it generated;
- whether generated PCM reached the backend transport;
- which backend release, model configuration, and dependency versions ran.

The first pass does not add per-token provider events, per-PCM-frame events,
remote telemetry, durable conversation memory, or a new control path.

## Model

There is no single backend state. A pipeline may listen for input while an
earlier response is generating or playing. State belongs to an independently
identified entity, and related entities are joined through correlation IDs.

An internal `response_key` identifies one generation and its downstream work.
It maps to at most one public Realtime `response_id`; work cancelled before
public exposure has no `response_id`. Every public response normally maps to
exactly one internal key. The trace records both values together when the
public response is created and again when it reaches a terminal state.

The append-only trace uses four record kinds:

- `state.transition`: a meaningful lifecycle state changed;
- `milestone`: a boundary was crossed without creating a durable state;
- `measurement`: aggregate counts, sizes, durations, or hashes;
- `snapshot`: immutable process or session configuration.

Errors are terminal state transitions with a bounded `error_category`. A
diagnostic message may be included, but trace consumers must not need to parse
free-form text.

### Envelope

Every JSONL record has this common envelope:

```json
{
  "schema_version": 1,
  "event_id": "evt_...",
  "sequence": 1842,
  "wall_time": "2026-09-04T14:03:22.381492Z",
  "wall_time_unix_ns": 1788545002381492000,
  "monotonic_ns": 482193847231,
  "backend_instance_id": "backend_...",
  "pipeline_id": 0,
  "session_id": "session_...",
  "entity_type": "tts_job",
  "entity_id": "tts_...",
  "event_kind": "state.transition",
  "event_name": "tts.started",
  "state_to": "running",
  "correlation": {
    "turn_id": "turn_...",
    "turn_revision": 2,
    "cancel_generation": 5,
    "response_key": "tts_...",
    "response_id": "resp_..."
  },
  "attributes": {
    "text_chars": 73,
    "text_sha256": "...",
    "coalesced_input_count": 3,
    "max_new_tokens": 360
  }
}
```

`wall_time` is UTC and intended for human and cross-process alignment.
`monotonic_ns` is authoritative for durations. `sequence` establishes an
emission order within one backend process when worker threads share a timestamp.
Timestamps are captured at the source before the record is queued for writing.

Optional values are omitted rather than written as `null`. Attribute keys are
event-specific but bounded by the schema tests. Raw binary audio, images,
credentials, authorization headers, and provider media payloads are forbidden.

### Payload lineage

Text-bearing boundaries record these metadata by default:

- `text_chars`;
- `text_utf8_bytes`;
- `text_sha256`;
- `part_count` or `delta_count`, where applicable.

When the existing `--log_transcripts` diagnostic gate is enabled, the same
records may additionally include `text`. The default trace remains useful by
comparing hashes between adjacent boundaries without retaining clinic speech.

Provider token deltas are accumulated in memory. The trace records first-delta
timing, final text lineage, and delta count rather than one JSONL row per token.
Audio is handled similarly: record first-audio timing and final chunk, sample,
duration, and optional incremental-hash summaries rather than raw frames.

## Entity States

| Entity | Normal states | Exceptional terminal states |
| --- | --- | --- |
| `backend_instance` | `starting`, `loading`, `ready`, `draining`, `stopped` | `failed` |
| `pipeline_slot` | `free`, `claimed`, `draining`, `free` | `quarantined` |
| `session` | `created`, `configured`, `active`, `closing`, `closed` | `failed` |
| `input_segment` | `open`, `speech_active`, `endpointed`, `closed` | `cancelled`, `failed` |
| `turn_revision` | `current`, `reopen_pending`, `committed` | `superseded`, `cancelled`, `empty`, `failed` |
| `stage_job` | `queued`, `running`, optional `streaming`, `completed` | `cancelled`, `failed`, `stale_discarded` |
| `public_response` | `pending`, `created`, `outputting`, `completed` | `cancelled`, `failed` |
| `audio_delivery` | `queued`, `sending`, `drained` | `cancelled`, `failed`, `discarded` |
| `cancel_generation` | `active`, `discarding`, `cleared` | none |
| `tool_request` | `generated`, `exposed`, `awaiting_result`, `result_received`, `followup_queued`, `completed` | `cancelled`, `failed` |

Stages use `attributes.stage` with one of `vad`, `stt`, `llm`,
`llm_output`, `tts`, or `transport`. A stage may omit states that do not apply;
for example, a nonstreaming STT job moves directly from `running` to
`completed`.

## First-Pass Events

### Backend and session

- `backend.starting`, `backend.ready`, `backend.stopping`, `backend.stopped`;
- `backend.configuration` with selected backends/model IDs, relevant flags,
  Python version, and installed package versions;
- `pipeline.stage_started`, `pipeline.stage_stopped`, and
  `pipeline.stage_failed` for worker threads;
- `session.active`, `session.configured`, `session.draining`,
  `session.quarantined`, and `session.released`.

The server-generated session ID and backend instance ID must reach the client
artifact once so the long-running backend trace can be joined to one reception
run.

### Input and STT

- `vad.speech_started`, `vad.segment_held`, `vad.segment_discarded`, and
  `vad.turn_finalized`; reopened status is an attribute of speech start;
- `stt.started`, `stt.completed`, and `stt.failed`; an empty successful result
  uses `stt.completed` with `reason=empty_transcript`;
- `stt.transcript_published` and `stt.transcript_publish_failed`;
- audio sample count/duration, processing delay, language, text lineage, and
  turn/revision IDs.

Progressive transcription updates are summarized by count. Final STT remains a
separate event.

### LLM and transcript

- `llm.request_enqueued`, `llm.request_prepared`,
  `llm.provider_request_started`, `llm.first_provider_event`,
  `llm.first_text_delta`,
  `llm.completed`, `llm.cancelled`, and `llm.failed`;
- `llm.output_chunk_emitted` and `llm.output_chunk_received` around the
  sentence-batching/output-processor boundary;
- `assistant.transcript_part_sent` and
  `assistant.transcript_finalized`;
- `public_response.created`, `public_response.completed`,
  `public_response.cancelled`, `public_response.failed`, and
  `public_response.incomplete`, each carrying both `response_key` and
  `response_id` when public exposure occurred;
- provider/model, message count, provider request ID when supplied, token usage,
  delta/chunk counts, text lineage, and timing.

The first pass records the ordered output sequence and sentence count. Explicit
boundary-reason classification is deferred.

### TTS and audio

- `tts.input_enqueued` for every `TTSInput`;
- `tts.started` with the coalesced source count and combined text lineage;
- `tts.first_audio`, `tts.completed`,
  `tts.cancelled`, and `tts.failed`;
- selected backend/model/voice/language, `max_tokens`, source chunk count,
  pipeline PCM chunk/sample counts, audio duration, generation duration, and
  RTF;
- provider termination reason when exposed; otherwise
  `termination_reason=generator_exhausted` plus a separate
  `token_limit_suspected` measurement when output duration is approximately the
  configured codec-token ceiling;
- `transport.first_audio_sent`, `transport.audio_completed`,
  `transport.audio_cancelled`, `transport.audio_discarded`, and
  `transport.send_loop_error`, summarized per response.

### Cancellation and discard

- `response.cancel_requested` with the cancelled generation and reason;
- `pipeline.input_discarded` and `assistant.output_discarded` with stage,
  payload type, response key, generation, and bounded reason;

An explicit discard-cleared event is deferred; the keyed transport terminal
currently closes that lifecycle.

### Tools

- `tool.call_generated` when the provider call reaches the output processor;
- `tool.call_sent` after the Realtime event is accepted by the client
  transport;
- `tool.result_received` when a function-call output returns to the backend;
- `tool.call_discarded` when cancellation or an obsolete response key prevents
  exposure.

Tool argument/result bodies follow the same transcript gate as other text;
their hashes and sizes are always available for lineage.

Every started stage job must have exactly one terminal state in the trace.

## Runtime Architecture

Components emit into one process-local recorder:

```text
backend worker threads / asyncio router
              |
              | trace.emit() -- timestamp + sequence + nonblocking enqueue
              v
       bounded thread-safe queue
              |
              v
       dedicated writer thread
              |
              v
       rotating backend JSONL
```

The producer path must not serialize JSON, perform filesystem I/O, await an
async operation, or inspect raw audio. It computes small text hashes only at
payload boundaries when tracing is enabled. The writer thread owns JSON
serialization, buffering, date rotation, and periodic flushes. A separate OS
process or remote collector is not required for this event rate.

## Failure Isolation

Tracing is never part of backend correctness:

- `emit()` never raises into pipeline code;
- enqueue is nonblocking;
- a full queue drops trace records and increments `dropped_events`;
- serialization, permission, disk-full, and writer exceptions stay inside the
  recorder;
- writer health exposes `writer_alive`, `queue_depth`, `dropped_events`,
  `write_errors`, and `last_write_wall_time`;
- shutdown uses a bounded drain and cannot indefinitely delay backend exit;
- no traced value is read back by VAD, STT, LLM, TTS, cancellation, or protocol
  logic.

The initial release remains feature-gated. Disabling the trace restores the
previous execution path except for no-op recorder calls.

## Storage and Retention

The location is supplied explicitly by the deployment launcher. Files are
named with backend instance ID and UTC date, written with owner-only
permissions, and rotated by UTC date. Backend trace files must be
included in the same report-first retention workflow as other runtime logs
before production enablement.

Process configuration snapshots must never contain API keys, environment
secrets, complete private profiles, or provider authorization data.

## Acceptance

Focused automated checks must prove:

1. Envelope fields, UTC/monotonic timestamps, and per-process sequences are
   valid.
2. Concurrent emitters produce parseable JSONL with unique event IDs.
3. Queue-full, serialization, path, and writer failures never propagate to a
   caller.
4. Transcript text is absent by default and present only under the existing
   diagnostic gate.
5. One synthetic response can be joined from STT through LLM chunks, transcript,
   coalesced TTS input, generated audio, and its terminal response.
6. Started jobs receive one terminal event for completion, cancellation, stale
   discard, and failure paths.
7. Producer-side `emit()` p99 remains below `0.25 ms` in a local focused
   microbenchmark. The first local 50,000-event run measured `0.0022 ms` p99
   with zero drops; hashing a 1,000-character payload before the enqueue raised
   p99 to `0.0029 ms`, also with zero drops.
8. The reviewed 12-turn replay still completes with no new Realtime errors and
   no unexplained trace gaps.

The first real-audio validation for item 5 passed on 2026-09-04 using reviewed
turn 1. Its backend trace explicitly joined `response_key` to public
`response_id` at creation and completion, matched text hashes across every
STT/LLM/TTS boundary, matched generated and transported PCM byte counts, and
reported zero dropped events and write errors. The retained evidence is
`artifacts/s2s-main-migration/event-trace-turn1-20260904-155442/`. Item 8 still
requires running the structured backend trace during the full reviewed
12-turn replay; the earlier 12-turn functional replay predates this trace.

The first implementation may leave exhaustive prefetch, WebRTC, and
non-production backend instrumentation for a follow-up, but it must cover the
production Parakeet, Responses API, Qwen3-TTS, WebSocket transport, cancellation,
and session paths.
