# Reception Agent Tools

Status: time/date and Firecrawl search implemented locally for testing; not deployed.

## Scope

- First-pass tools: `time_now` and `web_search`.
- Optional later: weather, nearby places, and individual web-page reading.
- Planned separately: approved local TCM/acupuncture vault search and reading.
- Existing `reference_catalog` / `reference_read` tools are test infrastructure,
  not implicitly enabled production tools.

## Shared Credential

Use one Firecrawl credential across profiles and test/production uses:

```text
FIRECRAWL_API_KEY=<your-key>
```

On the local Mac it belongs in the repository's Git-ignored `.env`:
`/Users/noel/projects/reachy_mini_receptionist_clean/.env`.
The empty entry has been created with owner-only permissions. No profile-specific
credential file is needed. Shell environment values take precedence over `.env`.

When deploying on m1max, provision the **same key**, not a new profile key, in
`/Users/leon/projects/reachy_mini_receptionist_deploy/.env`. Frozen releases use
that shared deployment environment. The key belongs to the receptionist client,
which executes the HTTP request, not to Hermes or the S2S backend environment.
No credential or runtime configuration has been deployed by this change.

## Contracts

`time_now` accepts `{}` or an optional IANA `timezone`. Default:
`America/New_York`. It returns local/UTC timestamps, date, weekday, and timezone
using the runtime machine's system clock, including daylight-saving offsets.
It does not query a time service or establish clinic/appointment availability.

`web_search` accepts `query` (1-500 characters) and optional `source`: `web`
(default), `news`, or `both`. It calls Firecrawl's fixed
HTTPS `/v2/search` endpoint once, requesting up to three results per selected type without scraping
pages or automatic retries. See the
[Firecrawl search contract](https://docs.firecrawl.dev/api-reference/endpoint/search).

The result contains source URLs (up to 2,048 characters), titles (200 characters),
and search-description/news-snippet excerpts (1,500 characters each), plus retrieval time and
explicit truncation/omission indicators. It forwards no Markdown page bodies,
HTML, images, or arbitrary extra provider fields. The HTTP response body is
limited to 256 KiB and the serialized tool output to 48 KiB. A valid empty search
returns an empty result list, not an invented answer.

The final result contains at most three entries **total**, interleaving web and
news when both were requested. Each is labelled with its source type. News
retains the provider-reported publication date (up to 120 characters) when
available, without inventing a date or confusing it with retrieval time.

The HTTP socket timeout is 12 seconds; the tool executor's wait limit is 15
seconds. HTTP runs on a worker thread, outside the media event loop. Cancellation
stops waiting/submission through the existing coordinator; it does not forcibly
interrupt an HTTP operation already running in that thread. There are no
automatic HTTP redirects or retries. Authentication, quota, malformed response,
network and timeout failures return bounded messages, not raw provider bodies.

## Discovery and Instructions

The registry supplies both schemas directly in `session.tools`: no discovery
tool call or separately maintained tool dictionary is needed. `time-web` appends
short usage guidance to `session.instructions` and updates its hash/provenance.
The guidance favors approved clinic context and reusing results for the current
request, and asks for brief spoken synthesis instead of verbatim web output.

The guidance is organized as shared Selection rules, Available tools (one
subsection per tool), Evidence and authority, and Spoken answers. Each tool
subsection identifies Purpose, When to use, Inputs, and Result. Search query
privacy and publication-date interpretation belong to the search subsection,
not the shared answer-format rules. Add future tools using the same subsection
structure; keep their schemas as the executable input contract, and keep shared
rules outside individual tool descriptions. Optional tools are not advertised
until they are actually enabled.

The shared spoken instructions in
`profiles/clinic_receptionist/session_instructions.txt` define the voice-only
channel: no screen or clickable links, normally one or two natural sentences,
and website names with supported navigation steps instead of web addresses or
Markdown. The search schema and appended tool guidance reinforce that source
URLs are provenance, not speech. These components are composed into the final
`session.instructions`; the private SOUL and clinic facts remain unchanged.

After profile and tool guidance composition, `with_session_date` appends runtime
date context: the New York local ISO date, weekday, and `America/New_York`
timezone with its current EST/EDT designation. It reads the system clock locally,
without a tool/provider call. This section anchors words such as today, latest,
and recent; `time_now` remains available for exact time or a changed local date.
The final hash includes the date section and provenance adds `runtime:local_date`.
No date is written into the underlying private profile sources.

The preview command applies this final step. The text-test driver assembles a
fresh date snapshot for each new session (steps 5 and 6 share one snapshot).
The live client's opt-in agent-profile path captures it when initializing the
run's instructions; it is not automatically refreshed during a long run.
Date context is therefore labelled as an assembly-time snapshot, not a live
clock. The real Hermes-source composer still awaits live-startup migration.
The base profile composers remain deterministic and do not themselves read time.

Tool results are model context, not input to the TTS sink. The existing LLM
continuation generates the spoken answer. Conciseness and omitting identifying
details from search queries are instruction-level behavior to evaluate, not
claims of deterministic word limits or a programmatic PII filter. No extra LLM
summarizer, query cache, or per-turn call budget is introduced in this pass.

## Enablement

The live client now defaults to `time-web` when a client-owned profile is enabled:

```text
RECEPTION_AGENT_TOOLS=time-web
```

Use `--agent-tools none` or `RECEPTION_AGENT_TOOLS=none` to disable tools explicitly.
Unprofiled legacy runs still have no client tools. The separate
`reference-test` value preserves the old reference integration tool set.
Choosing `time-web` does not also enable reference tools.

The live client selects the reviewed Hermes-source composer with
`RECEPTION_AGENT_PROFILE_FORMAT=hermes`, the profile ID, and a private source
directory containing original `HERMES.md`, `personality.md`, facts, capabilities,
and catalog. Missing private sources fail startup; fictional public facts are
not fallback content in this mode. The public spoken-rule file remains shared.
See [production promotion](s2s-production-promotion.md) for deployment status.

To preview the additional tool instructions without overwriting the reviewed file:

```bash
PYTHONPATH=src .venv/bin/python scripts/compose_s2s_profile.py \
  --profile-id reachyclinic \
  --source-dir private/profiles/clinic_receptionist \
  --soul private/profiles/clinic_receptionist/personality.md \
  --spoken-instructions profiles/clinic_receptionist/session_instructions.txt \
  --tools time-web \
  --output private/profiles/clinic_receptionist/S2S_TIME_WEB.preview.md
```

## Direct Smoke Tests

These invoke a tool directly, without a robot, LLM, or S2S process. The search
command makes one paid API request; its query is sent to Firecrawl.

```bash
PYTHONPATH=src .venv/bin/python scripts/test_reception_tools.py time_now
PYTHONPATH=src .venv/bin/python scripts/test_reception_tools.py \
  web_search "Firecrawl official search API documentation"
PYTHONPATH=src .venv/bin/python scripts/test_reception_tools.py \
  web_search "New Jersey news" --source news
```

Offline tests mock HTTP and cover DST/timezones, schemas, credential handling,
bounded results, malformed/provider errors, and opt-in instruction provenance.
Actual LLM tool selection, repeated-call frequency, spoken answer length,
evidence quality, and latency require integration checks.

## Text-Only Integration Results

The sequential driver is `scripts/test_reception_tool_conversation.py`. It uses
the composed clinic profile, both tool schemas, and the existing realtime tool
coordinator against isolated S2S staging. All response requests are text-only.
Each step requires semantic review before continuing; any issue stops the run.
The format guard rejects explicit URLs and links, not every possible
spoken-format failure, and is a test assertion rather than a runtime filter.
Short useful domains are accepted. Markdown emphasis alone is a TTS review
concern, not a text-only blocker, per subsequent user review of step 4.

The initial MVC question returned full renewal URLs, which the user rejected.
The revised answer below was subsequently accepted by the user.

The revised voice-only instructions were tested in
`artifacts/s2s-main-migration/time-web-text-voice-v2-20260904-01`, using profile
hash `b38a736285d587937f355d960ce157f77b8a0b6937ea1a1fa7a6b0b3204c6cd5`.
The private review copy is
`private/profiles/clinic_receptionist/S2S_TIME_WEB.voice-v2.preview.md`.

| Step | Observation | Elapsed |
| --- | --- | --- |
| 1: hours and small talk | Passed; correct context, no tools | 1.20 s / 1.32 s |
| 2: New York time | Passed; one time call, correct local date/time | 2.11 s |
| 3: Seoul time | Passed; one time call, correct timezone/date | 2.06 s |
| 4: MVC renewal | User accepted; one search, concise supported navigation with a short domain | 3.04 s |
| 5: recent news | Failed recency review; one news search, March update described as recent in September | 3.94 s |
| 6-7: follow-up, bounded-result synthesis | Not run; stopped at step 5 | - |

Step 4 produced 31 words, including `NJMVC.gov` twice and bold navigation labels.
The initial review rejected these formats too strictly; the user accepted the
answer, and regression coverage now allows short domains and emphasis alone.
The existing instruction wording was not changed for the resumed tests.

Step 5 resumed with the same instruction hash in
`artifacts/s2s-main-migration/time-web-text-voice-v2-20260904-02`.
It called `web_search` once with source `news` and query
`recent New Jersey news reported March 2026`. The user had not specified March.
The selected result was a rolling Sokolove Law lawsuit-update page with provider
date `3 days ago`, but its returned excerpt described a March 16, 2026 update.
The final 35-word answer called that update recent, despite the September 4
local test date. Search retrieval succeeded in 1.34 seconds; no clock lookup or
corrective search occurred. This is a temporal-grounding failure, not a tool
transport failure. The source of the model's March assumption is unconfirmed.
Steps 6-7 remain pending. No production configuration was changed.

Following this finding, runtime date context was added to instruction assembly
and tested offline for UTC/local date boundaries, DST, provenance hashing, and
the final instruction size limit.

### Date-Context Regression

Steps 1-5 were rerun sequentially with runtime date context, using instruction
hash `ea2d199e76f2f8b9c847cf7c8c40c1e9ace4b3e0a9bfed0017a508b66d749fa9`.
Artifacts: `artifacts/s2s-main-migration/time-web-text-date-context-20260904-01`.
All five passed semantic review:

| Step | Tool calls | Elapsed | Result |
| --- | --- | --- | --- |
| 1: hours and small talk | 0 / 0 | 1.27 s / 1.48 s | Correct clinic hours; concise small talk |
| 2: New York time | 1 time call | 2.02 s | Correct clock time and Friday date |
| 3: Seoul time | 1 time call | 2.25 s | Correct clock time and Saturday date |
| 4: MVC renewal | 1 web search | 4.09 s | Supported navigation; short domain; 31 words |
| 5: recent news | 1 news search | 4.22 s | September 4 WRNJ story and date supported by returned excerpt; 45 words |

Step 5 queried `New Jersey latest news September 2026 reported` and summarized
the Target Zero Commission story, naming WRNJ and its September 4 report date.
The March assumption did not recur in this run. The tool returned three results,
with two excerpts bounded to the configured limit; the answer did not reproduce
the returned social-sharing links. This validates this case, not a guarantee
about future search freshness. Tests were text-only; no TTS/robot playback ran.
Stopped at step 5 as requested, with four tool calls total across six prompts.
No production deployment was performed.

### Follow-Up and Long-Result Tests

After approval to continue, the driver repeated step 5 because the earlier
session had been closed. Steps 5 and 6 then shared the same new backend session.
Artifacts: `artifacts/s2s-main-migration/time-web-text-date-context-20260904-02`.
The instruction hash was unchanged from the date-context regression.

| Step | Tool calls | Elapsed | Result |
| --- | --- | --- | --- |
| 5: news setup repeated | 1 news search | 4.34 s | Passed; source-supported recent New Jersey Globe story and reported age |
| 6: source follow-up | 0 | 1.33 s | Passed; answered New Jersey Globe from conversation context |
| 7: long-result synthesis | 1 search with mocked transport | 2.62 s | Passed; 25-word answer with the supplied hours and reservation requirement |

Step 7 used the real LLM, tool coordinator, and result formatter, but a synthetic
search transport response: three results with 200-character titles and
1,500-character excerpts each. The fictional museum's Tuesday-Sunday,
10 am-6 pm hours and reservation requirement were followed by repeated background
text. The assistant preserved the requested facts, inferred Monday closure from
the supplied schedule, and omitted URLs and the background filler. It did not
call Firecrawl for this step. This is a bounded-result synthesis check, not
evidence about real museum information or search latency.

All seven text-only cases have now passed with date context. This does not
constitute audio playback acceptance or guarantee future tool-answer quality.

### Organized Tool-Guidance Regression

All seven cases passed again after restructuring tool guidance into Selection,
per-tool Purpose/When to use/Inputs/Result, Evidence and authority, and Spoken
answers. The assembled instruction matched the v2 review copy byte-for-byte:
6,795 characters, SHA-256
`fc822899903f985241556ae34cf25d00695edc4c96f61c845d13777cc1d735eb`.

Artifacts: `artifacts/s2s-main-migration/time-web-text-organized-v2-20260904-01`.

| Step | Tool calls | Elapsed | Result |
| --- | --- | --- | --- |
| 1: hours and small talk | 0 / 0 | 1.68 s / 1.09 s | Correct hours; concise small talk |
| 2: New York time | 1 time call | 2.38 s | Correct time and Friday |
| 3: Seoul time | 1 time call | 2.54 s | Correct time and Saturday date |
| 4: MVC renewal | 1 web search | 2.74 s | Supported directions, 28 words |
| 5: recent news | 1 news search | 5.76 s | Carteret broadband expansion; supported September 4 publication date, 42 words |
| 6: source follow-up | 0 | 1.37 s | Broadband Communities Magazine from the same conversation |
| 7: long-result synthesis | 1 search with mocked transport | 2.30 s | Correct hours and reservation requirement, 20 words |

Five tool calls across eight prompts: two time calls, two real Firecrawl
searches, and one synthetic search-transport call. No extra searches occurred
for the follow-up. Step 5 used `New Jersey latest news September 2026`, correctly
distinguishing the article publication date from the announcement date in its
excerpt. Step 7 retained the existing three 1,500-character synthetic excerpts.
All tests were text-only; no robot playback or production deployment occurred.
