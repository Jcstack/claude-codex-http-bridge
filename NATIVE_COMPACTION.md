# Optional native Codex compaction

Claude Code retains the conversation, permissions, hooks and tools. This
extension uses Codex's native encrypted context state for Claude's compact
request. It requires the reviewed managed subscription runtime containing
`native-compaction-v2.patch`.

```text
claude-codex --native-compaction
claude-codex --native-compaction --resume SESSION_UUID
claude-codex --no-native-compaction
```

Use `/compact` normally in the opted-in session. Automatic compaction uses the
same documented request-class hint. `CLAUDE_CODEX_NATIVE_COMPACTION=1` opts in
future launches; the explicit disable flag wins. Missing gateway hints,
unsupported runtimes and the project-API route are rejected. Default launches
retain ordinary Claude summaries. Already-running clients retain their options.
The selected header is also saved in private managed settings so background
tasks keep the same choice, including an explicit disable.

## Protocol and fallback

The implementation follows Codex 0.166's
[V2 request](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/compact_remote_v2_attempt.rs)
and [collection path](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/compact_remote_v2.rs):
a typed `compaction_trigger` on an ordinary Responses stream, followed by one
canonical compaction item in a completed response. The legacy standalone
endpoint's 404 did not establish that native compaction was unavailable.
The [OpenAI guide](https://developers.openai.com/api/docs/guides/compaction)
describes opaque state continuation; Codex's V2 trigger is a source-verified
subscription protocol, not a public API promise.

Only this dedicated maintenance operation leaves its own effort unset, matching
native compaction. An explicit max trial returned 500; native default completed.
Ordinary turns and fallback summaries retain the user's effort, including max.
Operator payload rules apply once at the final barrier and retain the original
Claude source matching context. The adapter executes no tool action.

On native failure, no partial native summary has reached Claude. The original
summary request is used, with a fixed metadata warning and no lowered effort.
Partial responses are not replayed. Account access and upstream failures can
still make either path fail.

## State and privacy

Claude Code 2.1.314 drops a structured compaction block across `/compact`, so the
bridge uses a self-contained signed text carrier. On continuation it verifies
the signature, replaces only the carrier with native state, and preserves all
current instructions, images, tools, results and later turns. Claude owns
pruning; the bridge does not discard preceding input items itself.

HMAC-SHA256 uses the private local origin-guard key with a separate domain
prefix, never OAuth or project credentials. Only guarded numeric-loopback
upstreams enable the extension. Invalid, truncated, duplicate and foreign
carriers fail closed; tool outputs cannot create a context boundary. The
envelope has a 1 MiB defensive bound; larger output falls back to the summary.

The carrier is private conversation data even though native state is encrypted.
Never publish it, enable body logging, or add transcripts to Git. Resume/fork,
translator replacement and account/model changes preserve it under the same
signing root. Another installation needs that original root; a fresh key cannot
authenticate it. No key export or sharing is performed automatically.

Plugins expecting readable summaries need separate validation. Historical
image pixels are not certified as reconstructible; fresh images survive replay.
Local token-count estimates strip the carrier but cannot inspect native state.
Claude's post-compaction estimate can differ from subsequent provider usage;
the HUD continues using actual reported usage.

## Verification, 2026-10-31

- Direct V2: 13,105 synthetic input tokens compacted in 7.854 s; a max-effort
  continuation recovered all 12 latest configuration fields.
- Real Claude: about 14k input tokens, `/compact` in 35.029 s, 12/12 fields.
- Larger real Claude: 64,413 provider input tokens; the update read 64,256 cached
  tokens. Two compactions took 39.492 s and 46.765 s, each followed by a Terra
  max-effort continuation retaining 12/12 fields from the Sol conversation.
- Separate native capsule recall passed on Sol, Terra, Astra and the two locally
  authorized accounts. This does not certify arbitrary future models/items.
- Fake upstream tests cover SSE, repeated compaction, process/model replacement,
  instructions, fresh images, tool results, cache accounting, max effort,
  malformed carriers and fallback. Codex executor tests, shared helpers and
  Claude translation tests pass. The full upstream executor suite has a Devin
  diagnostic failure reproduced on untouched pinned source; it is not reported
  as passing.

Credential-free gate:

```text
python tests/smoke_native_compaction.py /path/to/reviewed-proxy
```

Synthetic live gate (consumes quota):

```text
python tests/live_compaction.py /path/to/reviewed-proxy --root /private/installation --native --efforts max --samples 3000 --repeat 2 --recall-model terra
```

These tests do not establish equivalent quality on a 367k-token investigation
or bounded upstream latency. No current user session was compacted or restarted.
