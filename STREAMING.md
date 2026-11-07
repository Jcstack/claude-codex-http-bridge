# Streaming and live interaction

Checked 2026-10-30 with Claude Code 2.1.314 and CLIProxyAPI 8.0.46. Claude Code remains the harness; OpenAI supplies model inference through the local HTTP bridge. Three different capabilities are often called “live stream”:

| Capability | Current result | Protocol boundary |
| --- | --- | --- |
| Progressive text/tool output | **Works in the tested paths.** A live GPT-6.1 Sol subscription-route `claude-codex -p --output-format stream-json --include-partial-messages` run returned 320 stream events over about 34 seconds and ended successfully. A fake OpenAI API-key upstream sent two text deltas 250 ms apart; the proxy forwarded both as distinct Anthropic Messages SSE deltas before `message_stop`. The same fake route forwarded a tool's two partial JSON argument deltas before its completion event. | Claude Code consumes Anthropic Messages SSE; CLIProxyAPI translates OpenAI Responses SSE. The fake upstream is a protocol test, not a live API-key model test. [OpenAI SSE guide](https://developers.openai.com/api/docs/guides/streaming-responses). |
| GPT-6 mid-turn steering | **Not available through this Claude Code HTTP path today.** OpenAI accepts `response.steer` on a persistent Responses WebSocket while a GPT-6 response is running. CLIProxyAPI has WebSocket steering code for Codex-protocol clients, but Claude Code sends Anthropic Messages HTTP requests to this bridge, not a Responses WebSocket steering frame. A separate HTTP turn after the first finishes is ordinary follow-up, not mid-turn steering. | A transparent implementation would need a Claude Code input event while its current request is open, a bridge-owned upstream WebSocket, and a tested mapping for interrupted text, tool calls, approvals, and continuation. No such end-to-end mapping is claimed here. [OpenAI steering guide](https://developers.openai.com/api/docs/guides/steering). |
| GPT-Live full-duplex voice | **Separate protocol and UI.** Real Realtime audio/transcript streaming now works through the existing subscription, without a project API key, via the optional [MCP pack](NATIVE_TOOLS.md). This does not establish GPT-Live entitlement or a full-duplex Claude terminal: the native WebRTC bootstrap returned 400 and Claude channels require Anthropic authentication. Video and steering remain unverified. [Live WebSocket protocol](https://developers.openai.com/api/docs/guides/voice-websockets), [channels gate](https://code.claude.com/docs/en/channels). |

For the existing terminal workflow, launch `claude-codex` normally. Its interactive UI already renders the streamed response. To inspect machine-readable partial events in a headless task, pass Claude Code's `--output-format stream-json --include-partial-messages --verbose` flags; the wrapper forwards them. This is useful for measuring time to first visible output and detecting broken SSE framing without logging prompt bodies or credentials.

Claude Code's [gateway guide](https://code.claude.com/docs/en/llm-gateway-protocol#streaming)
describes SSE pings as one way to prevent client-side idle timeouts during
long pauses. The [pinned translator's configuration](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/config.example.yaml)
documents streaming keep-alives as disabled by default; neither running local
generation explicitly enables them. A separate [upstream discussion](https://github.com/router-for-me/CLIProxyAPI/discussions/2138)
reports a similar incomplete-stream symptom and a participant's configuration
change, but does not establish this installation's cause. Downstream keep-alives
cannot reconstruct a missing upstream terminal event. Their framing, timing,
error handling, cancellation and interaction with safe retries still need an
isolated end-to-end test before changing the active runtime.

### Native Claude error compatibility, 2026-11-07

The selected source profile now includes a narrow
[Claude error compatibility patch](upstream/patches/claude-error-compat.patch).
The pinned Claude Messages handler first derives a native error type from the
HTTP status, but then replaces it with a foreign `server_error` or
`service_unavailable_error` read from the upstream body. These are not the
Claude error discriminators documented in the
[Messages error reference](https://platform.claude.com/docs/en/api/errors).
The patch retains the status-derived category for those two foreign names:
for example, 408 becomes `timeout_error` and 502/503 becomes `api_error`.
Existing Claude categories, unknown future types, diagnostic messages and
existing status/header handling are preserved. No overload is fabricated.

An isolated, integrity-locked Claude Code 2.1.316 client reproduced a concrete
consequence: after empty thinking blocks containing opaque signatures,
`server_error` ended the task, while the canonical category let the same
client retry and continue. A two-proxy fake Codex origin reproduced the real
missing-`response.completed` sentinel. The baseline failed the reasoning-only
case; the patched outer handler completed it. The
[interactive terminal fixture](tests/probe_opaque_reasoning_ui.py) also recovered
in the same owned Claude process. No model account or user session was used.

The [tool-safety fixture](tests/probe_claude_stream_safety.py) additionally
requires successful recovery after partial arguments, completed arguments and
a completed tool item. Each tested case performs the owned write once and
retains its result. Reasoning-only retries must preserve the requested model,
reasoning settings, instructions, input, tools, tool choice and prompt cache
key. The local delayed Sol/Sonnet-mapping control held opaque reasoning for
65 seconds before injecting EOF and passed. A separate local Astra/Opus-mapping
control held reasoning for 485 seconds, then recovered with the same semantic
request, one write and its retained result. Two interleaved owned tools also
completed once each at three injected EOF boundaries. Synthetic model identifiers and
`behavesAs` mappings exercise client protocol paths; they are not model quality
or provider latency measurements.

The [three-platform recovery gate](.github/workflows/claude-error-recovery.yml)
compares binaries differing only by this patch, checks a delayed Astra/Opus
mapping and tests draining managed handoff. Interactive terminal checks are
explicitly skipped on Windows; its native headless client is still tested.
The [run](https://github.com/Jcstack/harnesslift/actions/runs/42268092997)
passed on all three platforms. After a 68-check installed-source overlay and
persistent-client handoff passed locally, the source-locked Mac bundle was
selected through the managed upgrade. Origin state/process, ingress
identity/port, client key and account were preserved; Claude sessions were not
restarted. A short synthetic subscription control through the installed stable
endpoint returned complete nonstream JSON with the exact expected marker,
`end_turn` and a buffered completion observation. This checks the healthy route,
not recovery from an induced real provider failure. Already admitted old
requests finish on their original generation; legacy direct sessions do not
receive this managed-runtime patch.

Claude Code remains responsible for retry decisions. The managed translator's
request retry stays zero, and the separate experimental bridge replay below
remains disabled. This patch does not force a retry after arbitrary visible
output, resume provider state, guarantee exactly-once effects for arbitrary
tools, or prevent the physical upstream disconnection. Account refresh,
provider-side tool effects, client retry exhaustion and other error categories
still need their own acceptance evidence. A stream error after HTTP 200 must
be handled as a stream error, as the official
[streaming guide](https://platform.claude.com/docs/en/build-with-claude/streaming)
explains; ordinary HTTP retry behavior alone is insufficient.

#### Native retry budgets

The official [Claude Code retry reference](https://code.claude.com/docs/en/errors#tune-retry-behavior)
documents `CLAUDE_CODE_MAX_RETRIES` and a separate unattended watchdog.
The [budget fixture](.github/workflows/native-retry-budget.yml) sets a small
native budget only inside a disposable client. Local two-hop controls recovered
after two consecutive reasoning-only EOFs with one owned write and retained
result. A persistent-failure control with two permitted retries stopped after
three requests, performed no write and preserved the compared request semantics.
This checks both recovery and a finite stop condition. The
[three-platform budget run](https://github.com/Jcstack/harnesslift/actions/runs/42271883858)
passed. The production revision's
[full source-and-install run](https://github.com/Jcstack/harnesslift/actions/runs/42268027982)
also passed all eleven jobs. A larger candidate workflow is tracked separately;
its [run](https://github.com/Jcstack/harnesslift/actions/runs/42268096452)
passed all three platforms after a same-source Windows rerun. Its first Windows
job timed out while preparing a fixture's NTFS permissions, before its contract
checks. That failure is retained; the later pass does not identify why the
PowerShell preparation stalled. The Mac candidate run also includes the owned
parallel-tool Ctrl-C/resume probe. Real Windows account/login and provider-side
cancellation remain separate acceptance boundaries.

An additional local headless case closes a complete synthetic text block with
`response.output_text.done` and `response.content_part.done`, then injects EOF
before response completion. The baseline stopped after one request with no
write. Patched Sol/Sonnet and Astra/Opus mappings retained that text in the next
request and completed the task with one write and its result. This is the
harness's native continuation of committed text, not an identical-request
replay. It does not demonstrate interactive continuation. The extended budget
workflow checks this case separately; its result is independent of the earlier
budget run. The [extended run](https://github.com/Jcstack/harnesslift/actions/runs/42272951635)
passed on macOS, Linux and Windows.

No retry settings were changed in running user sessions. The watchdog is not
enabled automatically: its documented maximum-wait setting covers 429/529
waits, not every transient error, and its usage-limit recheck is specific to
Claude account limits. It cannot be assigned Codex semantics by inference.
Neither an attempt count nor a successful synthetic retry proves an overall
provider deadline, cancellation safety or hosted-tool idempotency.

#### Cancellation and server retry advice, 2026-11-07

The credential-free [native cancellation fixture](tests/probe_native_retry_cancel.py)
uses the locked headless client, an empty disposable home and two owned translators.
Local Sol/Sonnet and Astra/Opus mappings passed cancellation during an active
signature-only stream and during native 429 backoff. On POSIX the fixture sends
Ctrl-C through the child's controlling terminal. The active origin connection
closed, no subsequent inference attempt occurred, independent requests completed
before and after cancellation, and the workspace stayed empty. Windows coverage
uses termination of the owned child; it does not establish Windows console-key
behavior. These tests do not resolve the existing-HOME/installed-path timeout
or cancellation between real tool effects.

A separate [interactive fixture](tests/probe_interactive_retry_cancel.py) keeps
the actual terminal client alive. Local Sol/Sonnet and Astra/Opus controls cover
Esc and Ctrl-C during active streams and native 429 backoff. After cancellation,
the same process accepts a new user turn and displays its exact synthetic
result. The next upstream input must contain that new prompt, which distinguishes
continuation from a stale automatic retry. Active-stream cases also require
origin connection closure before gateway cleanup; independent requests complete
and no workspace effect occurs. These cases use fresh homes and no accounts.
They do not resolve the existing-HOME/installed-path issue or Windows console
behavior. The [separate POSIX gate](.github/workflows/interactive-retry-cancel.yml)
checks both mappings, both keys and both cancellation boundaries.
Its [run](https://github.com/Jcstack/harnesslift/actions/runs/42332780182) passed
all eight combinations on both Linux and macOS with the selected source profile.

The [interactive tool fixture](tests/probe_interactive_tool_cancel.py) adds a
different cancellation boundary: one owned write has completed while a second
write waits in a synthetic `PreToolUse` hook. After Esc or Ctrl-C, the waiting
effect must remain absent. The same client process then receives a new user
turn, preserves the successful tool result, completes only the missing write
and retains both final results. Independent requests must still complete,
and `PostToolUse` markers count each write separately to detect an overwrite
that would otherwise hide duplicate execution. Local Sol/Sonnet and Astra/Opus
controls passed with both keys through a selected outer and earlier inner
translator. All four controls also passed using the byte-identical client at
the installed executable path, still with an empty temporary HOME. The POSIX
workflow repeats these cases at the locked and copied executable paths;
cross-platform acceptance is pending. These are synthetic hook and file-tool
checks, not provider-hosted action cancellation or resolution of the earlier
existing-HOME/installed-path timeout. No running client, setting or retry
budget is changed by these fixtures.

Claude's [interactive-mode reference](https://code.claude.com/docs/en/interactive-mode#general-controls)
describes Ctrl-C interruption. This test checks observable tool and continuation
behavior instead of relying on a particular interruption message in the UI.

An initial fixture wrongly required the literal UI text `Interrupted`; this
client can silently restore the input instead. Acceptance now uses connection
closure and the actual next-turn request. One provisional run finished its
functional checks but failed during cleanup. The client now stops before its
temporary home or gateways are removed, and the affected case passed locally.
Neither fixture-development failure establishes an installed-service defect.

The official [Messages gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol)
requires preserving `x-should-retry` and integer-second `retry-after` advice.
The [header fixture](tests/probe_retry_advice.py) reproduced a separate gap:
the selected source loses a 503 wait and its retry veto; a 429 retains the wait
but loses the veto. Status and private-header filtering remain intact.
The [excluded candidate](upstream/patches/retry-advice-candidate.patch) retains
only validated retry controls independently of broad header passthrough. It
captures a deadline once and emits the remaining rounded-up seconds; a locally
trusted quota deadline takes precedence. Invalid or conflicting values do not
become default-passthrough controls. Cookies and arbitrary headers stay filtered.
This advisory clock starts at error capture after status-body decoding, not at
request admission or first-header arrival; it is not an overall request deadline.
An explicit veto stops additional credential selection and unauthorized refresh
attempts, including configured continuation rules, without reclassifying a
genuine credential-level quota failure as a request-only error. A positive hint
does not add attempts or expand the retry budget.

Local Go race checks, actual error-writer checks and two-hop probes passed.
Both synthetic caller keys are checked as accepted at each owned hop. With
multiple fake credentials and additional configured gateway retries, the old
profile repeats a vetoed request while the candidate makes only one upstream
attempt. The real native client honored a
synthetic 503 wait and stopped on its veto without changing compared request
semantics. A fixed outer translator with the old inner translator still loses
the advice and permits a native retry: the credential owner has already removed
the headers. Therefore this patch is **not in the selected runtime profile and
has not been deployed**. Its [three-platform gate](.github/workflows/retry-advice-cancellation.yml)
retains the old-inner negative control. The strengthened
[run](https://github.com/Jcstack/harnesslift/actions/runs/42331542417) passed on
macOS, Linux and Windows, including accepted fake caller keys at both hops,
the repeated-request baseline and native 429/503 wait/veto cases for both mappings.
Windows cancellation here terminates the owned headless child, not a console key.
The selected production profile also passed all eleven jobs in the
[source-and-install run](https://github.com/Jcstack/harnesslift/actions/runs/42331050548).
That build excludes this retry-advice candidate.
Safe migration of the original credential
owner remains necessary; merely building or upgrading an outer backend cannot
close this gap. This candidate concerns pre-header HTTP refusals, not headers
added after an HTTP 200 stream has been committed.

The OpenAI [cancellation reference](https://developers.openai.com/api/reference/resources/responses/methods/cancel)
limits the cancel endpoint to background responses. Its
[background guide](https://developers.openai.com/api/docs/guides/background)
describes cancelling synchronous responses by terminating the connection.
Closing an owned fake connection tests propagation through these translators;
it does not verify cancellation of provider computation or hosted-tool effects.
No project API, watchdog, production timeout or user retry setting was enabled.

A later read-only audit found natural completions and no retained post-rollout
API-error record in the tracked main/child scope. Another long automatic
compaction coincided with origin and managed inference requests lasting nearly
the whole interval; nearby token-count work was short. This narrows the timing
investigation but lacks a shared request ID and cannot distinguish model work,
queueing, network delay or hidden upstream attempts. It is neither a failure-rate
estimate nor proof that slow compaction or physical stream loss is resolved.

### HTTP request phases, 2026-11-07

The earlier stream observer starts after response headers and decoder setup.
The selected source now adds [HTTP phase observation](upstream/patches/codex-http-observation.patch)
at the two existing `Client.Do` calls in the Codex request helper. It observes
the initial HTTP call and the existing plain-body compression fallback separately;
it does not create another attempt. `dispatch` records entry to that call,
`headers` records its return, and `decode` records response decoder setup.
Each record uses the existing finite diagnostic opt-in: one hour by default,
at most an explicitly selected 24 hours, with expiry fixed at process startup.
No new network timeout, payload rewrite, body read or credential selection is added.

Header wait includes the HTTP client's connection, upload, redirect and response
work; it is not model computation time. Decoder setup can itself read a gzip
header, so it has a separate clock. Decoder readiness is not a first model token
or a completed response. The [Go HTTP contract](https://pkg.go.dev/net/http#Client.Do)
also permits errors from redirect policy after an earlier response; a failed
HTTP call is not automatically a preconnection failure.

Only fixed request classes, route categories, status, bounded elapsed times and
fixed error/context categories are retained. The observer does not retain a
request, body, header map, URL, model name, account identity or error message.
A random process nonce and local attempt counter distinguish compression
fallbacks and process restarts. The diagnostic report uses those only internally
and omits both, together with local request tags. Its per-log state is capped at
256 attempts and its recent/error lists at twelve. Duplicate or conflicting
sequences suppress paired timings. Missing phases can mean expiry, rotation,
cancellation, unavailable records or ongoing work; they do not prove a hang.

Local Go race tests passed real request-helper boundaries, decoder cancellation,
body cleanup and unchanged compression fallback. The
[owned HTTP fixture](tests/probe_http_phases.py) passed held headers, held gzip
setup, cancellation at both boundaries, a 503 and independent work. A two-hop
case with the previous inner binary also passed: when the inner decoder waits,
the outer still records a header wait. This negative control prevents attributing
the old credential owner's internal work to OpenAI or to an observed outer
decoder. The new observer cannot instrument the old origin-to-provider hop.
The [three-platform gate](.github/workflows/request-phase-observation.yml) adds
native-client recovery and persistent-client managed handoff. Its
[run](https://github.com/Jcstack/harnesslift/actions/runs/42336373490) passed all
three platforms, and the [source/install run](https://github.com/Jcstack/harnesslift/actions/runs/42336315848)
passed all eleven jobs. The local offline suite passed 433 tests with twenty
optional skips. An overlay of the installed controller sources passed 76 checks
and an old-to-new persistent-client handoff before activation.

The source-locked observer was then selected through a managed-backend upgrade.
Private before/after checks preserved origin state/process, ingress process/port,
frontend key, enabled account and account inventory. An older generation still
had admitted work after publication; this is a metadata observation rather than
proof of each individual stream. Existing Claude processes were not restarted.
A short synthetic subscription control, chosen from the current local catalog,
returned the exact marker and `end_turn` through the installed TLS endpoint.
Dispatch, HTTP return, decoder readiness and a buffered completion were recorded;
no phase refusal/error, noncompletion or parsing gap was observed in that bounded
postcheck. It verifies the healthy route and instrumentation, not recovery from
an induced real provider failure. The new explicit diagnostic window expires
after 24 hours. The paid Project API and experimental gateway replay remain off.

### Forwarded incomplete-stream failures

A credential-free [two-layer fixture](tests/probe_opaque_reasoning_stream.py)
sent three synthetic Responses reasoning items with opaque content and no
visible summary, followed by an incomplete stream. The pinned translators
produced three empty Claude `thinking` blocks with signatures and an error;
three synthetic SSE comments produced no such blocks. The
[translator](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/internal/translator/codex/claude/codex_claude_response.go)
has a signature-only reasoning path consistent with the fixture. Empty
thinking text alone therefore does not measure useful model progress, prove
upstream silence, or identify a real stream's upstream events. Preserving
opaque reasoning state does not require exposing or decoding it.

A separate [interactive client fixture](tests/probe_opaque_reasoning_ui.py)
used the locked Claude Code 2.1.316 client, an isolated empty home, a local
fake Responses origin, and no model account. Five synthetic signature-only
items with 8,501 characters each caused its terminal to show `1.6k` and then
`8.0k tokens`, despite no visible thinking text or real model tokens. A control
with five SSE comments showed no such token counter. These are observations of
this client and fixture, not a measurement of actual model work, billing, or
the events in another session. A growing terminal counter alone cannot prove
useful reasoning progress or establish final provider usage.

One [Windows workflow rerun](https://github.com/Jcstack/harnesslift/actions/runs/42035283391)
reported the synthetic draining handoff as passing, then failed while deleting
the temporary proxy executable with `WinError 5`. The log alone does not show
whether an exiting fixture process or an external file lock was responsible.
The fixture now retains and waits for its own spawned processes after stop; a
process that remains alive fails the test. Temporary-directory cleanup retries
Windows permission errors for a bounded period and still fails if the lock
persists. This is a test-cleanup change, not proof of a production fix.

In a read-only review of one tracked session and its children on 2026-11-04,
one main-session incomplete-stream record and one managed-backend error with
the same sentinel occurred in the same minute. No cross-layer request ID was
available and the screenshot had no confirmed time. These observations do not
prove they are the same request, which component caused the original missing
terminal event, or whether other clients were affected. A logged HTTP 200 is
not a completed stream. OpenAI documents `response.completed` as the terminal
completion event in its [Responses streaming reference](https://developers.openai.com/api/reference/resources/responses/streaming-events).

A later recurrence had a main-session error and a managed in-stream sentinel
at the same second. The managed access record and an origin `/v1/responses`
record ended in that second after approximately 744 and 743 seconds,
respectively. The preceding recorded session request time was consistent with
that duration. These are strong timing associations, not shared request IDs or
proof of which network hop ended the stream. A recorded 301-second origin
request in the same sample does not establish a universal timeout. The logs do
not measure the longest period without upstream bytes or model events.

A later read-only session sample recorded several assistant thinking blocks
between the preceding request record and an incomplete-stream error; the last
recorded block preceded the error by about 16 seconds. Another same-time origin
access entry and the managed failure had matching approximate durations. This
is evidence of recorded partial client output, not a shared request identifier
or an upstream network-silence measurement. A pre-first-output bootstrap retry
does not by itself recover this observed pattern, and replay could repeat
effects in other interrupted turns.

A further screenshot-confirmed failure at 08:06 on 2026-11-05 aligned with a
managed incomplete-stream record and an origin Responses access record ending
in the same second, after roughly 392 and 391 seconds respectively. A recorded
thinking block preceded the failure by about 25 seconds; the session continued
afterward. HTTP 200 and similar timings are not a shared request identifier or
proof of the hop or event that caused the closure. This is a separate symptom
from the HUD launch failure and does not establish that the stream issue is
fixed.

A read-only macOS unified-log query for Surge messages around that minute found
no matching ChatGPT, timeout, reset, or EOF entries. Its narrow process, time,
and message filters do not prove that every network event was logged or that
the network path was healthy. No raw system log was retained in the repository.

A later read-only power-log check inspected bounded windows around the reviewed
incidents. Assertions/notification timestamps spanned the windows, but the
entire retained sample had no recognized Sleep, Wake or DarkWake transitions,
including no positive transition control outside those windows. Timestamp
coverage therefore does not establish power-transition coverage or exclude
sleep. Only Boolean shape/window results were retained locally; no raw power
log, device details or process names were saved. Screen-lock state and any
causal relation to the disconnection remain unknown.

A separate credential-free reproduction shows a distinct recovery defect when
the pinned two-layer translator uses its default cooldown policy. When the fake origin closes early, the executor's
locally synthesized failure is request-scoped and a later request can reach the
origin. When that exact failure is forwarded as a `server_error` /
`request_timeout` terminal event, the receiving translator loses its scope and
cools the credential; the next independent request returns 503 without reaching
the fake origin. The [candidate fix](upstream/patches/forwarded-incomplete-stream-candidate.patch)
recognizes the exact sentinel, type and code only, with a 502 or 408 status;
it preserves the error body and prevents credential cooldown and implicit
cross-account failover for that failure. It does not resume or replay the
interrupted stream. Synthetic controls retain quota, authentication, unrelated
timeout and explicit conflicting-status behavior.

The existing managed backend explicitly disables cooling and sets request
retry to zero. With those settings, the old binary already allows a follow-up
after the same synthetic EOF. A further local fixture used the actual generated
managed configuration, a private TLS ingress, and two owned proxy processes;
the unmodified backend again admitted the next request. Therefore the earlier
503 reproduction is **not evidence of a 503 in the user's current managed
configuration**, and this candidate is not a demonstrated fix for the original
disconnection. No candidate rollout is being performed for that assumption.

On macOS, a pinned real Claude Code client using only a fake origin completed
the partial-argument and forwarded `response.failed` cases with one synthetic
write and a retained tool result. Completed-argument and completed-item cases
also wrote at most once. A second fixture connects two separate owned proxy
processes to a fake origin: the inner proxy synthesizes the missing-completion
error itself, and the outer baseline reproduces the 503 while the candidate
admits the next request for both streaming and non-streaming clients. The pinned
Claude client also completed partial- and completed-tool cases through both
processes with one synthetic write. This is not an exactly-once guarantee for
real tools, every client version, or Windows. In another local two-layer
fixture, two interleaved synthetic tools each ran once and both results were
retained across three EOF positions; this does not establish exactly-once
execution for arbitrary parallel tools or timing. The unmodified and fixed
proxy are compared by [a standalone fixture](tests/probe_forwarded_incomplete.py).
The fix and a separate [metadata-only observer](upstream/patches/codex-stream-observation-candidate.patch)
were included in the next source-lock candidate. Before the 2026-11-06
activation described below, neither was active in the managed backend.
The observer is disabled by default. When explicitly
enabled in a candidate backend, it logs only
allowlisted event/outcome/error categories, the preceding event category,
bounded timing intervals and a local request tag. By default it stops recording
one hour after that backend process starts, even if it remains running; events ending
after the window are not recorded. The clock starts after
response headers, and intervals include processing and backpressure; they are
not measurements of upstream network silence. A [manual three-platform workflow](.github/workflows/forwarded-stream-observation.yml)
uses synthetic traffic and no account credentials. Its [updated run](https://github.com/Jcstack/harnesslift/actions/runs/41939124084)
passed on macOS, Linux and Windows, including a draining old-to-candidate
handoff with an owned TLS ingress and a fake provider. It does not establish
live-installation or provider behavior. Further validation is required before
activation. The flag
is accepted only for a newly created backend, so an existing in-flight stream
cannot acquire observation retroactively.

The 2026-11-06 observer revision also records `completion_event` controls at
info level for `response.completed` and the legacy `response.done` event.
Nonempty `response.incomplete` events retain the separate `incomplete_event`
category. This describes the immediate upstream event, not successful client
delivery, tool execution, or provider provenance. Failure observations remain
at warning level. The same explicit opt-in and default one-hour lifetime apply.

The read-only diagnostics group maximum read gaps into fixed intervals by
outcome, retaining aggregate controls beyond the twelve most recent details.
This allows comparing interrupted and completed streams without recording
content or treating HTTP 200 as completion. A long read gap in a completed
stream is a useful control against a universal idle-timeout claim; it does not
rule out route-specific timeouts. Timing still includes local processing and
backpressure. Older observations without a measured gap do not enter those
buckets. Synthetic tests cover healthy delayed streams in both buffered and
unbuffered modes, invalid completion labels, redaction, and retention of an
earlier failure after twenty completion controls. An initial test failed
because its log hook did not copy the log level; fixing the fixture retained
the passing delay measurements. Cross-platform and live rollout results must
be recorded separately before claiming activation.

The revised candidate subsequently passed the local 419-case offline suite
(20 optional cases skipped), observer race checks, the generated two-layer TLS
failure/healthy control, and a draining handoff using the same persistent
Claude client. An overlay containing the installed controller modules and the
two proposed module updates passed that handoff separately. The explicit live
subscription probe verified a short model response, its client termination,
and the owned observer's completion record. The
[three-platform observation workflow](https://github.com/Jcstack/harnesslift/actions/runs/42233093102)
subsequently passed on Windows, Linux and macOS. At local activation, the
macOS cloud job was still pending and the local Mac checks had passed; those
states are retained separately rather than counting a pending job as a pass.

On the active Mac, the reviewed runtime controller, diagnostics parser and
source lock were installed with private rollback copies. The managed backend
was replaced through its existing admission/drain mechanism with observation
explicitly enabled. Read-only checks verified that the original service state,
ingress process and port, front key, and selected account did not change. Real
traffic then produced completion observations, including one access record of
about 96 seconds with an observed maximum read gap of about 22 seconds. The
observer initially used the default one-hour expiry; it does not retry requests or repair missing
events. The original origin and ingress were not replaced.

Immediately before this activation, another real session failed after about
63 seconds. Its retained client records contained opaque thinking signatures
and no text or tool call after that request's marker. A prior completed tool
and result belonged to the preceding request. The managed error and an origin
HTTP 200 access row had matching timing and duration; they did not share an
authenticated cross-layer identity. Observation cannot be added retroactively,
and these records do not establish upstream silence or safe hosted-tool replay.

The separate [upstream report about a 900-second boundary](https://github.com/router-for-me/CLIProxyAPI/issues/7287)
was closed as not planned on 2026-11-06. Its maintainer reports that tested
WebSocket requests also hit that boundary, while acknowledging a contradictory
longer WebSocket sample. It is not evidence for the cause of the 63-second
incident, nor a verified WebSocket fix for this installation. Keep fixed-duration,
capacity, cancellation and network hypotheses separate. Do not add dummy tool
calls or silently lower effort to make a timeout disappear.

On 2026-11-05 the updated candidate was built locally on macOS from the pinned
proxy source and reviewed patches with a checksum-verified Go toolchain. Its
observer unit tests and generated managed TLS-ingress failure fixture passed.
An additional local fake-upstream case introduced a 300 ms delay before EOF;
the managed observer recorded a maximum read gap of at least 150 ms and a
later independent request completed. This tests the instrumentation, not the
duration or cause of the real 08:06 interruption.
Another isolated test overlaid the installed controller sources with the
proposed observer-aware runtime controller and candidate source lock; a held
old stream drained and a persistent Claude client continued on the candidate.
The active controller and services were not changed by those tests.

An isolated candidate backend then completed a short synthetic subscription
request through the existing origin and a temporary TLS ingress. The reusable
[subscription probe](tests/live_observer_subscription.py) also passed with a
bounded response, reassembled SSE text, an `end_turn` and `message_stop`. It
uses the existing localhost client key in a private temporary directory, not an
OAuth file; a handled exit removes that directory. It consumes subscription
quota and is not run by ordinary unit tests. An earlier exploratory request had
HTTP 200 and `message_stop` but a raw-byte marker check failed; that check did
not distinguish split events from an unexpected answer and is not counted as a
pass. The later test only proves the healthy short-stream path. It does not
induce or diagnose a real interruption, verify a real long-running cache hit,
or prove that replacing an active backend is free of all effects. At that stage
the observer remained uninstalled and disabled in the active service; the
2026-11-06 activation above supersedes that installation status.

To repeat the explicit live check with binaries you have independently reviewed:

```sh
python tests/live_observer_subscription.py --proxy PRIVATE_PROXY_BINARY --ingress PRIVATE_INGRESS_BINARY --root PRIVATE_BRIDGE_INSTALLATION
```

With the buffered-observation candidate, add `--nonstream` for one short
subscription request that requires a complete JSON message, exact synthetic
text, `end_turn` and a buffered completion observation. This consumes
subscription quota; it is never run by ordinary offline tests.

The probe binds the supplied binaries to the current source-lock metadata; it
does not independently prove how those binaries were built. On an unhandled
process termination, inspect the private temporary directory before removing
it; automatic cleanup applies to handled exits.

After an observer-capable runtime and controller have been selected, a POSIX
shell can request one bounded observation window for newly routed requests:

```sh
CLAUDE_CODEX_STREAM_DIAGNOSTICS=1 claude-codex bridge reload
```

For an intermittent problem, a reviewed revision also accepts an explicit,
canonical integer window from 1 to 86,400 seconds. Unset uses 3,600 seconds.
Invalid explicit values are rejected before a managed backend is started; the
binary independently bounds its window. The choice affects only a newly
started observed backend, never the ingress or an already-started request:

```sh
CLAUDE_CODEX_STREAM_DIAGNOSTICS=1 CLAUDE_CODEX_STREAM_DIAGNOSTICS_SECONDS=86400 claude-codex bridge reload
```

Recording stops automatically on expiry even if the process continues. This
keeps a longer local incident window without requiring repeated reloads; only
the same fixed metadata is recorded. No setting is added to login startup.
Synthetic two-layer TLS cases cover both the explicit 24-hour window and a
one-second window that has expired before the requests arrive. The latter must
still forward both a failure and a healthy response without any observation.
Source and runtime tests reject invalid, noncanonical and excessive values.

After local race, two-layer expiry and 24-hour controls and the installed-source
persistent-client handoff passed, the active Mac was switched to an explicit
86,400-second window. The original service, ingress, client key and selected
account remained unchanged; the new process's enabled flag and exact finite
window were verified without exporting its environment. Natural traffic
produced a completion record. The
[extended three-platform gate](https://github.com/Jcstack/harnesslift/actions/runs/42237489905)
subsequently passed on macOS, Linux and Windows. At activation, the cloud jobs
were still pending and the local Mac checks had passed. The
[full source gate](https://github.com/Jcstack/harnesslift/actions/runs/42237424806)
subsequently passed all eleven jobs. These gates use synthetic upstreams;
they do not establish recovery at the real provider. Automatic replay remains disabled.

For PowerShell, set `$env:CLAUDE_CODEX_STREAM_DIAGNOSTICS = '1'` before the
reload and remove it afterward with `Remove-Item Env:CLAUDE_CODEX_STREAM_DIAGNOSTICS`.
For the longer window, also set `$env:CLAUDE_CODEX_STREAM_DIAGNOSTICS_SECONDS = '86400'`
and remove it afterward. Clearing the invoking shell's variables does not end
the already-started process window; that process stops recording on its expiry.
These commands do not enable the feature in an older runtime. A reload starts a
new managed backend and relies on the existing draining mechanism; it does not
replace the original Codex listener or capture an already-started request.

### Interpreting the next captured incident

Run the read-only report locally; keep its output private:

```sh
claude-codex diagnostics --tracked --hours 1 --include-children
```

Use the current session's API-error time and the matching managed-log record.
The observer measures only that backend's immediate upstream:

| Observation | Established boundary | Next distinction to investigate |
| --- | --- | --- |
| `terminal_event` with `error` or `response.failed` | An error event arrived from the immediate upstream. | The original origin may have forwarded or synthesized it; its provider-facing failure is still unknown. |
| `eof` | Reading ended without a recognized terminal event. | Premature closure, incomplete framing and the component that ended the stream remain separate possibilities. |
| `read_error` | The local reader returned a classified error. | Compare its fixed category and measured gaps with completed controls; a duration alone does not establish a timeout policy. |
| `incomplete_event` | An explicit incomplete terminal event was observed. | Do not reclassify it as a network failure or claim tool completion without item-state evidence. |
| `completion_event` | A completed/done terminal category arrived. | Client receipt and successful downstream delivery remain separate checks. |
| No observation | This report supplies no matching observer evidence. | Check process/window coverage, earlier in-flight requests, alternate endpoints and parser coverage before drawing conclusions. |

`same_log_match` associates records only inside one proxy log. Similar origin
and client timestamps do not authenticate a cross-layer request identity. The
twelve recent details are bounded; outcome/read-gap aggregates can outlive an
individual detail. The revised read-only parser also retains a separate
`recent_noncompletion` list of at most twelve observations, with
`noncompletion_omitted_at_limit`. Later completion controls cannot evict these
details. Both recent lists can contain the same observation and must not be
added together as independent failures. Synthetic tests cover a terminal error
followed by healthy controls, HTTP-200 matching, time filtering, legacy fields,
ambiguous/absent tags and bounded retention without exporting raw identifiers.
This is a reporting change and requires no inference-service restart.

A later private error-file review found the same incomplete-stream sentinel
in a nonstream Messages request. The initially activated stream observer does
not cover buffered execution or failures before a successful upstream HTTP response.
An absent stream observation therefore cannot rule out those failures. A
separate observed `response.failed` event does not establish that it has the
same cause. No raw error-file contents were copied into the repository.

The next candidate adds `phase=buffered` observations to the nonstream
executor. Its elapsed time starts after successful HTTP headers, before the
existing body read. Since its events are parsed after buffering, it emits no
`since_read_ms`, prior event or event/read-gap fields; the read-only report
represents these as unknown and excludes them from gap buckets. Response
bytes, terminal handling, request construction and retries remain unchanged.
The helper resides under the upstream `helps` package.

The candidate also adds an optional `failure_kind` with a fixed vocabulary:
`none`, `other`, `reported_incomplete`, `reported_overload`,
`reported_rate_limit`, `reported_auth`, `reported_context_limit` and
`reported_content_filter`. These describe recognized structured error claims,
never provider provenance or permission to retry. Unknown code/message fields
are not recorded. Older observations have no reported kind. Only an owned
copy of an allowlisted event-name string is retained in the observer.

Local tests compare buffered response bytes/errors with observation disabled
and enabled, classify synthetic errors without their text, and exercise the
actual generated two-layer TLS path. The first failed nonstream response
remains 502, a separate healthy request still succeeds, expiry stops recording,
and no automatic retry occurs. Build, race, source reconstruction, client
handoff and live-control evidence remain distinct from cloud and installed
results. These changes improve diagnosis and do not repair a missing upstream
terminal event.

Do not add records from both proxy layers as separate
failed requests, infer provider silence from read gaps, or expose raw error
bodies, opaque reasoning, prompts or credentials to obtain a diagnosis.

A separate local before/after fixture compared synthetic model, instructions,
tools, input, reasoning, tool choice and prompt-cache key bytes across three
dynamic-tool stages and found them unchanged. It does not measure provider
cache hits or establish cache behavior after a real interrupted request. A
separate two-layer local fixture also compared those raw fields and the
synthetic session cache key between two identical requests on either side of
an EOF; it does not imply that a real Claude retry sends identical content.

Community reports such as [CLIProxyAPI issue 6469](https://github.com/router-for-me/CLIProxyAPI/issues/6469)
show both short and long incomplete streams; they do not establish a universal
timeout or this installation's cause. The unmerged [diagnostics proposal 6497](https://github.com/router-for-me/CLIProxyAPI/pull/6497)
is useful background but does not cover every terminal-error path. No
user-generated prompt, transcript, error body, or credential is needed in the
observer output.

An independent [Codex Desktop report](https://github.com/openai/codex/issues/59377)
also describes intermittent stream and compaction failures on macOS with a
proxy. Its proxy-mode change and later successful requests were not a controlled
comparison; it does not identify the cause of this bridge's HTTP stream closure.

In [CLIProxyAPI issue 5545](https://github.com/router-for-me/CLIProxyAPI/issues/5545),
an operator reported Astra and Terra streams ending after approximately 30-second
silent phases. The maintainer attributed those reports to the upstream service,
but the report does not establish the cause of this installation's failures.
The 25-second gap from the last recorded client thinking block to the 08:06
error is not an upstream event or byte-idle measurement. Compare bounded
event-gap observations across failures and successful controls before acting
on this hypothesis. A separate [issue 6534](https://github.com/router-for-me/CLIProxyAPI/issues/6534)
concerns streams that remain open without progress and was closed as not
planned; a silent hang is not equivalent to the observed terminal error.

[Issue 6856](https://github.com/router-for-me/CLIProxyAPI/issues/6856)
describes one distinct bootstrap-buffering failure involving a moderation
metadata event. Its fix is present in the pinned source and precedes the
original v8.0.46 release; the feature is disabled by default. The local logs do
not establish that this specific failure or an overload caused the observed
incomplete streams. Do not enable buffering or replay partial tool calls on
that assumption.

The opt-in observer recognizes `response.metadata` as an event
category, while retaining none of its fields or moderation data. A synthetic
privacy check injects a private marker into that event. Recognition does not
establish whether this event occurred in any real interrupted request.

### Response resume is a separate provider capability

The official OpenAI API documents [background response streaming and resuming
from a sequence number](https://developers.openai.com/api/docs/guides/background).
That mechanism requires creating the response in background streaming mode;
the documentation also says response data is temporarily stored to support
asynchronous operation even with `store=false`. It is not a way to retrieve an
already interrupted foreground response. The local pinned proxy returned 404
for a synthetic `GET /v1/responses/:id?stream=true&starting_after=...` without
contacting its fake upstream; [the routing fixture](tests/probe_response_resume_route.py)
does not test a real provider. The Codex subscription endpoint's entitlement,
retention and resume semantics are unverified. Do not infer them from the
separately billed API documentation or enable background storage implicitly.
Safe adoption would also need ordered deduplication, cancellation and tool-effect
tests, cache behavior, privacy review, and a user-selected supported route.
The separately authorized public [ChatGPT plan usage preview](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
explicitly excludes the `background` field. That limitation applies to its
documented public API flow, not automatically to the different Codex backend
used by this installation.

### Experimental recovery of an undelivered reasoning-only stream

The [isolated experiment](tests/upstream_reasoning_retry_experiment.patch)
buffers a bounded initial Responses SSE stream and permits one retry for the
exact incomplete-stream sentinel or a narrowly structured in-stream
`service_unavailable_error` / `server_is_overloaded` only when every complete
event so far is allowlisted and the request declares no server-executed tools. Text, client or
server tool items, quota or moderation metadata, unknown or malformed events,
duplicate JSON keys, conflicting status fields, background or stored requests,
and the buffer limits release the original stream without replay. The
experimental request classifier also declines to retry inputs above 16 MiB or
with JSON nesting deeper than 64 levels; these limits affect only the test
candidate, not the installed runtime. Buffering also withholds initial thinking
events from the client until a response is released; responsiveness and
keepalive behavior require real-world assessment. A nonblocking process-local
cap admits at most 16 concurrent recovery paths; a slot remains held until its
stream finishes, even after buffering is released. Additional streams pass
through without retry, including on cancellation. This bounds admitted paths,
not total process memory or request-parsing overhead. The fixture
preserves the full synthetic model request, including effort, tool definitions,
input and cache key; this does not measure a real provider cache hit.

On macOS, a locked Claude Code client, two disposable translators, and a fake
Responses origin completed a synthetic task after an initial signature-only
failure. The origin saw the same first and second request, the client received
the successful attempt, and an owned Write tool ran once and returned its result.
The [reproducible fixture](tests/probe_reasoning_retry_candidate.py) exercises
only fake credentials and network endpoints. Its test-only integration patch
uses a deliberately unsafe always-true account check, and is **not a runtime
bundle or an available production setting**. A separate race test reproduces
how an account can change after a simple check and before retry selection.
For an alternative placement inside the credential-owning origin executor, a
synthetic HTTP test switched a fake selected account after the first attempt;
cloning that attempt's request retained both its original fake authorization
and account header on retry. A separate two-layer [client fixture](tests/probe_reasoning_retry_account_switch.py)
changed a disposable fake API-key configuration while a request was in flight;
the retry used the original fake key, the following request selected the new
fake key, and the owned tool executed once. These illustrate a possible
same-account boundary for the tested fake route. A further executor-level test
used disposable OAuth-shaped metadata and separate synthetic account headers:
the in-flight request and its retry retained the first credential snapshot,
while a separately selected later request used the second. The test does not
run the OAuth refresh or account manager; it does not establish real refresh,
revocation, a live subscription route, or safe migration of the running origin
service.

The existing origin's `X-CPA-TRACE-ID` is another candidate observation point,
not an account-binding guarantee. A [fake-origin probe](tests/probe_origin_trace_selection.py)
observed a stable selection index that changed when a disposable API key was
replaced, but that fake API-key entry was absent from the management
`auth-files` list. More importantly, the pinned origin derives an OAuth
credential's index from its file path and type; the experiment's unit test
confirms that replacing the account at the same path keeps the same index.
Headers can also be unavailable if a response commits before selection. Never
expose the index in logs or use it alone to authorize a retry; a real solution
needs a private account-identity check coordinated with selection and switching.

The origin-placement fixtures above disabled automatic image-tool injection on
both fake hops. The installed managed hop explicitly uses passthrough, while
the original service configuration leaves this option unset. In the pinned
source, an unset origin can automatically add `image_generation` for eligible
non-free, non-lite, non-spark requests. The actual provider-bound request has
not been captured, so this does not establish whether a particular live request
contained that tool. A [negative two-hop fixture](tests/probe_reasoning_retry_origin_image_guard.py)
left the fake origin at its default, observed the injected image tool, and
verified that the experiment passed through the failure without replay. Thus
the positive origin tests do not establish that recovery would apply to the
current default. Do not silently disable image generation to widen retries;
safe recovery with provider-executed tools needs its own evidence.

The overload case was added after an actual session reported the same structured
type and code; a coincident origin access record returned HTTP 200. The session
had four retained empty signed thinking blocks before the error, but the
transcript cannot establish all raw upstream events or a cross-layer request
identity. An [OpenAI Codex issue](https://github.com/openai/codex/issues/56346)
independently reports the same in-stream error on the official route; it is a
user report, not confirmation of the cause here. The experimental default waits
30–60 seconds with positive jitter and supports cancellation, but performs at
most one retry. The fast fake end-to-end fixture replaces the wait with a
zero-delay test clock. Separate fake-origin tests explicitly enabled pre-output
keepalives on both disposable proxies and used the actual delay: one raw-client
run waited 51.2 seconds, received 51 SSE comments and completed after one
identical retry; a real Claude Code client waited 50.1 seconds and completed
its owned Write exactly once. These are individual macOS observations, not
proof of real-session liveness. The installed configurations did not set the
keepalive options, and were not changed. A [portable fixture](tests/probe_reasoning_retry_wait.py)
checks the raw-client case. An
unmerged [Codex proposal](https://github.com/openai/codex/pull/31058) describes
a longer, independently bounded policy for selected capacity errors.

The test-only wiring now converts a second-attempt HTTP rejection into a
bounded in-band error, preserving its status and valid error object rather than
returning the earlier stream error. A [two-layer synthetic quota fixture](tests/probe_reasoning_retry_quota.py)
observed HTTP 429 when no keepalive had committed the response; with two
pre-output comments already sent it observed HTTP 200 with an in-band
`rate_limit_error`. This does not establish lossless HTTP-header handling,
behavior for every error shape, or a real provider quota reset.

Do not deploy this experiment: atomic account binding across the existing
two-layer origin and real OAuth account switching is unresolved; second-attempt
error headers, real pre-output keepalives, total process memory limits,
raw EOF recovery, all server tools, and real subscription behavior remain unverified.
Even a bounded repeat can consume more quota and
cannot promise exactly-once effects for arbitrary model or provider behavior.
There is no automatic recovery for the observed live failures yet.

Claude Code's [StopFailure hook](https://code.claude.com/docs/en/hooks#stopfailure)
can observe an API-failed turn, but its output and exit code do not direct a
retry. It is not an official way to resume a partial model stream. Reissuing a
turn or injecting terminal input after partial tool output can repeat effects;
the bridge does not do that automatically.

An initial isolated test of the pinned translator enabled a one-second
keep-alive against a fake delayed Responses upstream. After the first output,
the downstream stream included keep-alive comments and finished normally. With
pre-first-token keep-alives disabled, a delayed upstream failure remained an
HTTP 503; enabling them committed HTTP 200 first and delivered that failure as
an in-band stream error. The latter can change client retry behavior. With
post-first-token keep-alives, an isolated Claude client completed a synthetic
response; separate fake-stream cases showed cancellation reaching the fake
origin and a truncated stream neither being replayed nor marked complete. A
second disposable TLS ingress preserved the keep-alive, let the Claude client
finish, and forwarded cancellation to the fake origin. These fixtures do not
test the active ingress or recovery from a missing upstream terminal, and have
not changed runtime settings.

On 2026-11-02 one short, low-effort Sol request used a disposable TLS ingress
and a one-second keep-alive against the selected subscription origin. It
completed in 2.81 seconds, with one keep-alive comment, one `message_stop` and
no reported stream error; the existing origin and ingress identities remained
unchanged. This is a connectivity sample, not evidence that a long request or
the recurring incomplete-stream failure is fixed.

### Incomplete tool-call safety, 2026-11-02

A credential-free fake Responses origin exposed a separate boundary bug in the
then-running translator. It sent a valid JSON argument followed by
`response.incomplete` / `max_output_tokens`, while the function item still had
`status: in_progress` and had not emitted `response.output_item.done`. The old
translator closed the tool block and the real isolated Claude client executed
one synthetic write. This is a reproduction with an owned fixture; it does not
establish that the active user session encountered this event. OpenAI documents
[`response.incomplete` and item lifecycle events](https://developers.openai.com/api/reference/resources/responses/streaming-events),
while Anthropic says an incomplete tool block at a [token limit](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons#incomplete-tool-use-blocks)
requires special handling.

The [candidate guard](upstream/patches/incomplete-tool-guard.patch) reports an
error instead of completing an unconfirmed streamed client-tool block. It also
rejects an explicitly unfinished `output_item.done`; a tool explicitly marked
completed remains eligible for normal handling. HTTP and WebSocket non-stream
requests fail with a request-scoped 502 instead of cooling the account. The
native executor and real-Claude fixtures use synthetic traffic: they cover
partial JSON, valid but unconfirmed JSON, an unfinished item, a completed-item
control, and two parallel tools with a completed first item. The Claude client
can independently retry after the error; in the tested parallel fixture the
completed result was retained and neither effect was duplicated. These samples
do not prove exactly-once behavior for every plugin, cancellation or retry.
Separately, [a before/after fixture](tests/probe_incomplete_cache_handoff.py)
compares exact synthetic model, instructions, tools, input, reasoning, choice
and session cache-key bytes across three dynamic-tool stages, including a long
gateway name. The safety-only candidate preserves them; enabling the separate
experimental name improvement intentionally fails this comparison. This is not
a measurement of the provider's actual cache hits.
If Claude records the guard's fixed error as an API-error message, the local
session audit reports the allowlisted `incomplete_tool_call` category without
printing that message. An automatic client retry may not leave such a record;
absence of the category is not proof that no guard fired.

On 2026-11-02, the safety-only candidate passed a [three-platform gate](https://github.com/Jcstack/harnesslift/actions/runs/41345932840)
and was rolled into the local managed backend. The selected account, original
origin and stable TLS ingress remained unchanged. One old stream was still
active during handoff and subsequently drained; a new backend inference later
completed HTTP handling without a recorded stream error. HTTP 200 alone does
not prove a complete answer. No extra live inference was sent for this rollout.
The original origin and sessions still directly connected to it retain the old
translator and need a separately safe migration. This issue is distinct from the
previously observed EOF/503 errors, whose root cause remains unknown.

### POSIX cancellation and fixture isolation

On the local macOS host, the non-interactive `ctrl_c_between_effects` fixture
timed out repeatedly when the installed Claude Code 2.1.316 ran with the
existing HOME, despite a foreground pseudoterminal with `ISIG` enabled. At one
timeout the process was reported as trying to exit; this does not establish
whether SIGINT was ignored. A byte-identical, locked client at another path
passed with that HOME, and the installed client passed the original focused
fixture with an empty temporary HOME. A separate minimal pseudoterminal
control also received SIGINT. These observations narrow the difference to the
launch environment and path together, not a proven Claude or bridge root
cause. That investigation did not cover interactive cancellation. The newer
[fresh-home terminal fixture](#cancellation-and-server-retry-advice-2026-11-07)
checks interactive cancellation separately; existing user state and Windows
console keys remain open boundaries.

The credential-free client fixtures now use a temporary HOME and, on Windows,
temporary profile and application-data paths. They also retain their separate
Claude configuration directory. The temporary translator helper and the
keep-alive probe also pass a limited environment to their disposable proxy
processes. Claude's [settings documentation](https://code.claude.com/docs/en/settings)
describes additional global state outside individual project settings, so a
separate configuration directory alone should not be treated as a complete
home-directory isolation guarantee. This environment change is not an OS
sandbox. The parallel fixture also falls back to terminating its own child if
the owned process-group signal is denied; its original Ctrl-C timeout remains
a failure. Other proxy launch paths and the live subscription clients have not
all been migrated to this helper and still require a separate privacy review.

### Upstream WebSocket keepalive test timing

The macOS build job in [candidate run 41487752563, attempt 1](https://github.com/Jcstack/harnesslift/actions/runs/41487752563/attempts/1)
failed the pinned upstream `TestCodexWebsockets_KeepalivePingDuringUpload_NonstreamSessionless`:
the synthetic client attempted a write after its local test connection had
closed. This occurred before the new schema contract tests ran. The same
candidate had passed an earlier three-platform gate; neither result explains
the cause of a real user's stream failure.

In the [pinned test source](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/internal/runtime/executor/codex_websockets_executor_test.go),
the fake server sends completion and returns immediately after receiving a
pong, while that same pong releases the client's blocked payload write. A
server close racing the write is a plausible fixture-level explanation, not
yet a proven root cause. A separate test-only patch makes three fake servers
wait until their reader observes the payload before sending completion; the
original requirement that the pong arrive while the write is held remains.
The separate immediate-terminal test is left intact. The manually dispatched
observation [passed on all three operating systems](https://github.com/Jcstack/harnesslift/actions/runs/41489076037):
30 repeats of each unmodified fixture, 50 of each modified fixture, and 10 of
the separate immediate-terminal test passed. The original failure did not
recur in that bounded sample, so these observations do not prove its cause.
The upstream [write hook](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/internal/runtime/executor/codex_websockets_session.go)
is reached before the payload is written. In the [three-platform negative control](https://github.com/Jcstack/harnesslift/actions/runs/41490027317),
deliberately prolonging that hold produced five matching closed-connection test
failures on each platform; the fake-server barrier passed five repeats with the
same hold. Each platform also passed the unmodified bounded sample, the patched
normal sample, and the separate immediate-terminal test. This demonstrates a
possible fixture race but does not prove what caused the earlier failed run.
The candidate workflow applies the barrier only to the pinned upstream test
file; production code and binaries do not include it. The [full candidate
run](https://github.com/Jcstack/harnesslift/actions/runs/41490731389)
subsequently passed on macOS, Linux and Windows with that test-only barrier;
the [earlier failed attempt](https://github.com/Jcstack/harnesslift/actions/runs/41487752563/attempts/1)
remains part of the record. These checks do not establish real WebSocket
behavior.

A separate community [controlled experiment](https://github.com/puxu-msft/copilot-api-js/blob/master/exp/cc-idle-280s/REPORT.md)
on Claude Code 2.1.201 reported an additional content-progress timeout that
comments and ping events did not reset. That result is version- and setup-specific
and has not been reproduced on this installation. Claude Code's current
[network documentation](https://code.claude.com/docs/en/network-config#streaming-idle-watchdogs)
describes separate event-, byte-, and body-idle watchdogs, with gateway defaults
of roughly five minutes and bounded extensions when bytes continue arriving.
A heartbeat can therefore delay detection of a stalled upstream even if another
watchdog eventually fires. The observed 46.8- and 215-second request durations
are not themselves measurements of idle gaps. These trade-offs require a
deliberate liveness and cancellation policy; this bridge does not fabricate
empty thinking content to extend a stream.

The official primary GPT-Live WebSocket uses `/v1/live/sessions` without a
model query, then `session.start` and `session.started`. It is distinct from
Realtime `/v1/realtime` and an existing Live call's sideband. A 2026-10-31
[local route probe](tests/probe_live_route.py) returned 404 before session start.
The pinned server registers `/v1/live/:call_id`, which does not provide that
primary-session entry point. This establishes a local protocol gap, not a
denied account entitlement; the earlier WebRTC 400 does not test the primary
WebSocket either. No microphone, playback or public project API was enabled.

Claude Code disables fine-grained tool-input streaming by default behind a custom `ANTHROPIC_BASE_URL`. On the subscription route, this launcher sets `CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING=1` unless you explicitly set it to `0`. In an isolated Claude Code 2.1.314 request capture, `1` added `eager_input_streaming: true` to all 21 built-in tool definitions; `0` added it to none. A small live GPT-6.1 Sol subscription-route run completed a Bash tool call and emitted 18 `input_json_delta` events in 32 stream events. This improves the chance of seeing a long tool input progressively; it does not mean Claude Code executes incomplete JSON. [Claude Code gateway behavior](https://code.claude.com/docs/en/llm-gateway-protocol#feature-pass-through), [fine-grained tool streaming](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming).

The optional direct API route leaves the flag off. The pinned translator forwards `eager_input_streaming` into the OpenAI Responses function-tool schema, where its acceptance has not been established by a live API call. A strict fake API rejects extra function-tool fields; a credential-free contract test still confirms that upstream function-argument deltas can reach Claude Code progressively when the fake API emits them. Explicitly forcing the flag on the API route fails before inference. Do not infer from the fake stream that the real API grants fine-grained tool-input generation.

The next optimization gate is to measure the same task with both harnesses and the same model: time to first visible event, event gaps, completed tool calls, task success, and cache use. A stream that starts quickly but loses a function result or breaks a subagent is a regression, not an improvement. Model effort and tool definitions should change only after those measures show a benefit.
