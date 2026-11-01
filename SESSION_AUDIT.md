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
- Lines are bounded at 16 MiB. Usage/tool identities and compaction details
  have explicit limits; up to 256 child logs per main log are inspected.
  Omitted records/files and parser limits are reported. Symlinked paths below
  a configured Claude root are skipped.
- Cache usage is deduplicated by response identity, including across children.
  Missing counters are reported separately. The fraction describes reported
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
HTTP access records say 200. The associated durations vary substantially;
they do not establish one fixed local timeout. The original response body and
terminal event sequence were not retained, so those historical logs cannot
prove whether the provider emitted an error, ended without a terminal event,
or suffered a transport interruption.

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

Both checks are CI gates on Linux, macOS and Windows. These fixtures cover the
specified error events and Write/hook interaction, not all socket failures,
parallel tools or the exact upstream failure in the user's historical session.
Provider cause and general safe stream recovery remain open work.
