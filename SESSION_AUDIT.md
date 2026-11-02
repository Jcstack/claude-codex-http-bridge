# Longitudinal session audit

The operator's long-running conversation remains active while this read-only
audit inspects its available main and child logs. Nothing is sent to a model,
written to a transcript, or uploaded. No service is started or restarted, and
no account credential is opened by the audit.

## Run locally

```sh
claude-codex diagnostics --hours 2
claude-codex diagnostics --lifetime --session YOUR_LOCAL_SESSION_UUID --include-children
# Only where an operator has already recorded a private audit target:
claude-codex diagnostics --lifetime --tracked --include-children
```

`--tracked` reads only the private `runtime-audit/target.json` receipt under the
installation root. The receipt contains a SHA-256-derived session tag, not a
transcript or credential. Missing or ambiguous selections do not fall back to
another recent conversation. `--session` and `--tracked` are mutually exclusive,
as are `--hours` and `--lifetime`. Existing two-hour diagnostics retain their
previous behavior.

Reports include requested model/effort, cache-counter coverage, compaction
durations, known tool activity, command mentions, API-error categories and
parser coverage. Tool arguments/results, prompt content, project paths,
original session/response/tool identifiers, plugin names and raw errors are
withheld. Unknown tool names are grouped; third-party MCP names become `mcp`.
The report itself contains usage and timing metadata and should be reviewed
before sharing. It is not automatically saved or committed.

## Coverage and interpretation

- Each file is read only up to its initial byte extent. Concurrent appends,
  truncation, unterminated tails, malformed data and skipped oversized lines
  appear in coverage. Undated metadata is included only in lifetime scans.
- Lifecycle session lines are bounded at 16 MiB. Lightweight session and proxy
  scans use a separate two-million-character line limit. Usage/tool identities
  and compaction details have explicit limits; up to 256 child logs per main log
  are inspected. Each scan reports its limits and skipped large lines; lifecycle
  reports also include malformed records. Symlinked paths below a configured
  Claude root are skipped.
- Cache usage is deduplicated by response identity, including across children.
  Each counter keeps the maximum valid reported value for that identity;
  smaller repeated output counters do not erase an earlier observation. These
  totals are not billing or final-context measurements. Missing counters are
  reported separately. The fraction describes reported
  input tokens, not request hit rate, throughput, subscription allowance or
  billing. Cached input still occupies context; cumulative input is not a
  context window. Child durations can overlap.
- A tool error may be a valid failed test, missing file or denied operation.
  Command mentions and requested model labels do not certify successful
  execution, actual provider identity or compatibility with every plugin.
- A candidate audit category recognizes a bounded, ownership-shaped `TaskStop`
  error without exporting the result text, task IDs or arguments. Other errors
  remain unclassified, and truncated error text is counted separately. A text
  match does not establish ownership, task liveness, or a common root cause.
  The candidate has not yet been installed locally; see [NESTED_AGENTS.md](NESTED_AGENTS.md).
- Proxy logs are shared across conversations. Origin and relay observations
  are not additive. An HTTP 200 can precede an SSE failure; both stream-failure
  mentions in proxy logs and API-error categories in transcripts are retained.
  Neither is automatically a count of distinct failed inference requests.
- The managed-log filter must retain an incomplete-stream sentinel even when
  no HTTP access record or general transport keyword exists. Oversized lines
  also need visible coverage because their timestamps and content are unknown.
  A synthetic regression checks both cases without returning the lines, tags,
  paths or keys. The unit-test steps in the [three-platform run](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41457409921)
  passed, and the fix was installed locally without restarting any service.
  Other jobs in that run may still be in progress.

## Current evidence and next investigation

The first complete scan found working cache reads, slow classic compactions
and separate connection/stream failures. See [CACHE_AUDIT.md](CACHE_AUDIT.md)
for synthetic cache controls and [PLAN.md](PLAN.md) for acceptance gates.

The target session also contains incomplete native streams whose corresponding
HTTP access records say 200. Relay logs include structured `server_error` /
`request_timeout` payloads for these symptoms. Their shape is now reported
without returning the payload; it does not establish whether the original
local service or a more distant upstream component generated the error. The
durations vary substantially, and the historical provider-side terminal event
sequence was not retained.

A later read-only audit on 2026-11-02 observed one incomplete-stream error in
the tracked main conversation. Within the shared managed-backend log, two
separate short local request tags reported the same incomplete-stream sentinel
around 02:42 China time. Their respective same-log HTTP records reported 200
and approximately 215 and 46.8 seconds. The tracked error timestamp is close
to the latter, and another main-session model request was recorded afterward;
neither fact identifies the corresponding request across layers or proves the
task recovered. Shared backend counts must not be attributed wholesale to this
conversation.

The diagnostics report now pairs a recent failure with an HTTP completion
only when one same-log local tag and a nearby timestamp give a unique match;
ambiguous and missing matches stay explicit. It does not expose the tag, log
line, request body, or private path. The local tag is not a cross-layer ID.

A 24-hour snapshot ending near 02:46 China time on 2026-11-02 contained 56
child logs, of which 12 had request model/effort metadata in that window. Those
records requested Astra with `high` 94 times and `low` 27 times; the main log
mostly requested Astra `max`. Counts are request records, not independent
tasks, verified provider identities, or evidence of why an effort was chosen.
The remaining child logs did not contain request metadata in that window.

In a separate credential-free test with Claude Code 2.1.316 and a synthetic
Messages server, an Astra parent at `max` invoked an Astra child. Omitting the
child effort produced `max`; explicitly requesting `high` or `low` preserved
that value. This verifies those specific client request shapes without the
translator or real model. It does not establish how the observed live children
were configured, so the bridge does not silently override their effort.

The sampled timeout messages exactly match the pinned origin's incomplete
stream sentinel. The running origin is the installed pinned release and its
binary contains that sentinel. The matching [executor code](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/internal/runtime/executor/codex_executor_terminal.go)
constructs a request-scoped timeout when a stream ends without a terminal
completion, and its [Responses error mapper](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/sdk/api/handlers/openai_responses_stream_error.go)
maps that status to `server_error` / `request_timeout`. This strongly suggests
origin-side wrapping of an incomplete stream, rather than establishing a
provider timeout. The exact string could also be forwarded; the retained logs
cannot establish where or why the terminal event was lost. Diagnostics classify
the exact sentinel separately without returning its text or payload.
An earlier [CLIProxyAPI issue](https://github.com/router-for-me/CLIProxyAPI/issues/3055)
reports the same class of incomplete Codex streams through Claude's Messages
route. It is a separate report against an older version, not proof of this
installation's underlying cause or of a released fix.

For several long compactions, an origin-service request in shared access logs
ended at nearly the same time and ran for nearly the entire measured compact
duration. This points to waiting inside the origin request path, subject to the
absence of a cross-layer request identity; it cannot distinguish model work
from network or other upstream waiting. Main-request effort is not proof of
the maintenance request's effort.

A later shared-log sample contained four HTTP 500 `/v1/messages` completions,
each near 30 seconds, and four origin `/v1/responses` 500 completions near the
same times. The origin recorded 14 TLS-handshake EOF mentions across five
same-log local tags, each mentioning the fixed ChatGPT host; the managed layer
recorded four such mentions. These counts and timestamps are not cross-layer
request identities, and the tracked session recorded no API-error entry in the
same window. After the last recorded origin failure, six origin Responses
access records reported HTTP 200. Three later credential-free direct TLS
handshakes to the official host also verified successfully. These checks do not
establish the origin process's proxy route, the cause of the earlier EOF, or
complete SSE responses. No proxy, credential, account or retry setting changed.

Diagnostics classify URL host mentions in handshake-error lines into a fixed
allowlist (`chatgpt`, `openai_api`, `loopback`, `other`, `mixed` or `none`)
without exporting the URL, credentials, private hostname, path or local tag.
A host mentioned in a forwarded error does not establish which process opened
the connection. The classification is not a cross-layer request identifier.

In a subsequent one-hour sample, the shared origin log recorded one more TLS
handshake EOF mentioning the ChatGPT host. A unique same-log tag matched a
Responses HTTP 200 completion 12 seconds later, with a recorded request
duration of 22.445 seconds. The tracked session and its children had no API
error records in that sample. A matching short tag is not a cross-layer ID,
and the HTTP 200 does not prove the SSE stream completed; the root cause
remains unknown. No account, proxy, or retry settings were changed.

For future observations, `recent_preconnection_http` pairs up to 12 recent
handshake errors with same-log POST completions occurring within 30 minutes.
It reports only a unique match, marks reused tags as ambiguous, and emits no
tag, raw error, URL, or body. The wider window helps observe a later completion
but increases the chance of an unrelated tag reuse. Treat it as correlation,
not proof of retry behavior or complete response delivery.
After the [three-platform source gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41409393314)
passed, the updated diagnostic module was installed atomically and its
read-only command passed a local smoke check. The original and managed service
states were unchanged; no running process was restarted.

A later one-hour sample contained an automatic compaction recorded as 574.868
seconds, with client metadata reporting 753,797 tokens before and 51,271 after.
A shared origin `/v1/responses` request ended within ten seconds and lasted
574 seconds with an HTTP 200 access status. The timestamp and duration are a
correlation only: the logs have no cross-layer identity tying the two records,
and HTTP 200 alone does not establish a complete stream or compaction quality.

Another read-only sample on 2026-11-02 found an incomplete-stream record in
the tracked main conversation at 16:06 China time. The shared managed log had
three incomplete-stream sentinel records near 16:03, 16:04, and 16:06; their
unique same-log HTTP matches reported 200, 502, and 200, with durations of
41.034, 60.000, and 58.864 seconds respectively. Shared origin Responses
access records completed at the same recorded seconds with HTTP 200 and
durations of 40.449, 60.000, and 58.242 seconds. This timing is correlation,
not a cross-layer identity or proof that an origin stream completed. After the
tracked error, the main session recorded 11 deduplicated usage responses and
no further API-error records in the sampled main or child sessions. Both
listeners still matched their recorded processes; no restart or retry policy
change was made. The source of the missing terminal events remains unknown.

At 17:30 China time on 2026-11-02, the tracked main session recorded an
automatic compaction lasting 624.744 seconds, with reported context changing
from 766,494 to 55,373 tokens. Shared origin and managed logs each recorded
a 624-second HTTP 200 request ending in the same second. These are consistent
with the compaction duration, but shared logs do not provide cross-layer
request identity or first-byte timing; they do not identify whether model
computation, upstream waiting, or another component dominated. In the same
two-hour audit window the main session's deduplicated reported cache-read
share was 94.8% of reported input tokens, which is not request hit rate and
does not establish the compaction request's cache behavior. The main session
continued afterward; one later HTTP 499 was recorded by both shared layers,
without enough evidence here to identify the initiating client or cause.
Within the recorded compaction interval the main log contained no assistant
usage record that could be attributed to the summary request. An `xhigh`
request was recorded immediately after the boundary; that later request does
not establish the compaction's effort or cache usage.

As a separate local-only control on 2026-11-02, a disposable TLS ingress,
candidate translator and fake origin relayed synthetic messages of 100,000,
1,000,000 and 4,000,000 bytes in 0.058, 0.130 and 0.407 seconds respectively;
all three preserved the synthetic content. These are single samples of highly
repetitive text on one machine, measured in bytes rather than tokens. They do
not include real Claude compaction, the native Codex origin, network waiting or
provider inference, and cannot establish the cause of a real slow compaction.
The reproducible `tests/probe_large_payload_latency.py` checks preservation
without setting a hardware-dependent latency threshold.

An [open report in the official Codex repository](https://github.com/openai/codex/issues/51886)
describes similar long-stream cuts, including near 900 seconds. This is an
independent user report, not proof that every local failure has that cause.
The [official Codex configuration reference](https://developers.openai.com/codex/config-reference)
documents separate request and stream retry controls; their existence does
not establish safe replay through a different harness.

The next transport gate must distinguish cancellation, an upstream error event,
missing terminal completion and preconnection failures, using metadata only.
Any recovery design must preserve tool identity and prove that partially
delivered tool calls cannot execute twice. Do not manufacture a successful
terminal event, lower the user's effort, add an arbitrary execution deadline
or replay committed tool actions merely to hide a disconnect.

A subsequent two-hour read-only sample on 2026-11-02 included one tracked main
log and 67 discovered child logs; two children contained request metadata in
the window. The sampled main and children had no classified API-error records.
Across 154 deduplicated responses reporting usage, cached input represented
97.47% of reported input tokens; that is not a request hit rate. The shared
origin log recorded 181 inference requests and 562 HTTP 200 completions across
all its routes, with no parsed transport or stream-error mentions. Shared
counts are not limited to that session, HTTP 200 does not prove a complete
stream, and missing or unclassified records constrain these observations.

## First tool-safety gate

`tests/probe_claude_stream_safety.py` uses the actual locked Claude client, a
disposable Write target and a PostToolUse hook that counts effects. It needs no
model account. The optional `--proxy-binary PATH` runs the reviewed translator
between that client and a synthetic Responses origin.

The initial local check on Claude Code 2.1.316 passed both paths. The translator
path covers a healthy baseline, half-written arguments, complete arguments,
a completed tool item without a response terminal, and a late error after
`response.completed`. Every successful case performed the write exactly once
and sent its tool result on continuation. No case duplicated the write.

The direct Messages check also showed that a completed tool block can execute
before the entire response is terminated. Consequently, absence of a response
terminal is insufficient evidence that replay is safe. Partial arguments can
instead abort without an effect; the gate permits that safe outcome and rejects
false success. It always requires the healthy control to succeed.

The current fixture uses `dontAsk` and a configured PreToolUse handler that
permits only its exact owned target and synthetic content. A separate target
must be denied with no write and no PostToolUse effect. Approval does not
deduplicate calls; the independent hook counter still detects repeated effects.
This handler exists only in the disposable fixture configuration.

The original Windows healthy control produced permission denials before any
stream fault was injected; a scoped CLI-rule variant also failed locally.
Those CLI-rule/configuration differences remain a separate permission-matrix
investigation. They are not diagnosed as a model or translator fault. The
[official permission documentation](https://code.claude.com/docs/en/permissions)
defines file-edit rules and PreToolUse authorization; production permissions
were not changed to make the fixture pass.

Both checks are CI gates on Linux, macOS and Windows. These fixtures cover the
specified error events and Write/hook interaction; other socket failure types,
parallel-tool combinations and the exact upstream failure in the historical
session remain separately scoped.
Provider cause and general safe stream recovery remain open work.

The expanded gate also covers clean socket EOF and `response.failed` at several
single-tool boundaries. `tests/probe_parallel_stream.py` uses two interleaved
tool calls, unique hook markers and tool-result IDs to exercise EOF, error and
terminal-event boundaries. It also terminates its own isolated client before
the first completed item and after both tool effects are acknowledged. Those
cases check the observed effects and follow-up history. On POSIX, four further
cases run the locked Claude client in `-p` mode with a private controlling
pseudoterminal, then send the terminal's Ctrl-C character before either tool
item completes, after the synthetic origin emits the first item, between the
two effects, or after both effects and results are acknowledged. A scoped
synthetic PreToolUse gate holds the second effect during the between-effects
case. Locally, neither early case wrote a target; the gated case retained one
effect and did not execute the other; the final case retained one per target.
The case that pauses after the first item synchronizes with the origin sending
it, not with proof that Claude has consumed it. The fixture controls only its
own process session and temporary targets.

An additional POSIX `--resume-interrupted` fixture resumes that same isolated
session after the between-effects interruption. The history contained a
successful result for the first tool and a rejected result for the gated tool.
A synthetic model that distinguishes those outcomes retried only the rejected
tool; both effects occurred exactly once and successful results appeared on the
continuation. The initial harness fixture had incorrectly treated a rejected
result as a success, so its synthetic model stopped too early; the revised
fixture retains that distinction. This does not guarantee that an arbitrary
real model will choose the same recovery action.

The candidate gate runs across Linux, macOS and Windows; terminal Ctrl-C is
exercised only on POSIX. Windows console events, interactive TUI behavior,
arbitrary tools, real-model recovery choices and unobserved provider failures
remain separate checks.
