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
- Proxy logs are shared across conversations. Origin and relay observations
  are not additive. An HTTP 200 can precede an SSE failure; both stream-failure
  mentions in proxy logs and API-error categories in transcripts are retained.
  Neither is automatically a count of distinct failed inference requests.

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

For several long compactions, an origin-service request in shared access logs
ended at nearly the same time and ran for nearly the entire measured compact
duration. This points to waiting inside the origin request path, subject to the
absence of a cross-layer request identity; it cannot distinguish model work
from network or other upstream waiting. Main-request effort is not proof of
the maintenance request's effort.

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
cases check the observed effects and follow-up history, while leaving actual
Ctrl-C behavior, arbitrary tools and unobserved provider failures for further
verification. The parallel test is part of the three-platform candidate gate.
